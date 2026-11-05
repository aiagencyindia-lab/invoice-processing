# Ledgerly — invoice processing

Ledgerly is a multi-tenant web app that processes accounts-payable invoices for
Indian companies (GST). A PDF arrives by upload, email or webhook; a worker
extracts it with Gemini, validates it, matches it against purchase orders, the
vendor master and work authorisations (2-way and 3-way), and decides **ACCEPT**,
**REJECT** or **route for approval**. Every step is kept in an audit trail.

The v2 **trust layer** sits on top: an evidence graph, findings and holds, GST/ITC
and payment-trust checks, leakage, provenance and reality checks, a trust score,
a "why" explanation for each decision, trust audits, API keys with a `/v2` API
and webhooks, and control of automated agents that act on invoices.

Production runs on Railway; see [deployment/README.md](deployment/README.md).
Open and paused work is in [docs/PENDING.md](docs/PENDING.md).

## How a document flows

1. **Ingest.** `POST /api/documents` (upload) or `POST /webhooks/email`
   (inbound email) stores the PDF, de-duplicates it by SHA-256 and queues a job
   (`invoice_processing/api/ingest.py`).
2. **Process.** The worker (`python -m invoice_processing.worker`) runs the
   pipeline in `shared_libraries/acting/general_invoice_agent.py`: classify,
   extract (Gemini Pro reads the PDF), four validation phases, transform,
   output, audit log.
3. **Correct.** Approved correction rules (the ALF engine,
   `invoice_processing/alf_stage.py`) apply exceptions the rules book does not
   cover. A reviewer who overrules a decision and records it as a policy
   exception creates a rule proposal; an approver sees its conditions and which
   past cases it would change before approving it. Approved rules run in shadow
   (recording what they would change) until the tenant sets `rules_binding`.
4. **Decide.** The case lands as accepted, rejected or waiting in an approval
   chain (below). Each processed document also queues trust-layer follow-up
   jobs (sync, evaluate, audit, re-match, agent-injection scan).
5. **Export.** Approved invoices export as CSV, XLSX or JSON
   (`invoice_processing/exports.py`) or to Xero (`connectors/xero.py`).

## Stack

- **Backend:** Python ≥ 3.11 (CI uses 3.12) with `uv`; FastAPI + uvicorn;
  SQLAlchemy 2 + Alembic; Postgres in production (forced row-level security
  per tenant), SQLite locally and in tests; google-adk / google-genai on Vertex
  Gemini.
- **Frontend:** Next.js 16, React 19, TypeScript, Playwright end-to-end tests
  (`frontend/`).
- **Sign-in:** Firebase / Identity Platform (Google), Microsoft Entra via OIDC.
- **Models in production:** `gemini-3.5-flash` and `gemini-3.1-pro-preview`
  (`GEMINI_FLASH_MODEL`, `GEMINI_PRO_MODEL`; the code defaults to 2.5).

## Repository layout

```
invoice_processing/
  api/            FastAPI app (service.py), ingest, admin, approvals, POs,
                  vendors, KPIs, trust_* routes, /v2 routes
  worker/         job runner and queue (local directory queue or Pub/Sub)
  db/             models, tenant sessions, RLS policies, Alembic migrations
  shared_libraries/acting/   the processing pipeline
  trust/          v2 engines: graph, findings, holds, gate, gst_itc, payment,
                  leakage, provenance, reality, score, webhooks, apikeys,
                  explain, audit, agents
  connectors/     Xero export
  admin/          provisioning CLI (suspend-tenant, purge-tenant, ...)
frontend/         Next.js app
benchmark/        300-invoice benchmark harness, profiles, production runs
deployment/       Railway deployment guide
docs/             PENDING.md, API_V2.md, design specs and plans
scripts/          start-railway.sh, audit_gate.py (CI dependency-CVE gate)
tests/            pytest suite
eval/             older per-case evaluation scripts (the benchmark replaces them)
```

## Running locally

```bash
uv sync
cp .env.example .env    # set PROJECT_ID, LOCATION, GOOGLE_APPLICATION_CREDENTIALS
```

API and worker against a scratch SQLite database, signed in as a dev tenant.
Blank the cloud variables rather than unsetting them: `load_dotenv()` refills
absent ones from `.env`.

```bash
ROOT=$PWD/benchmark/runs/manual/stacks/A
export DATABASE_URL=sqlite:///$ROOT/app.db INVOICE_DATA_DIR=$ROOT/data \
  CLOUD_SQL_INSTANCE= GCS_BUCKET= PUBSUB_TOPIC= PUBSUB_SUBSCRIPTION=
./.venv/bin/python -u -m invoice_processing.worker &
AUTH_DISABLED=1 ENV=local AUTH_DEV_TENANT=bench-a \
  ./.venv/bin/python -u -m uvicorn invoice_processing.api.service:app --port 8000
```

Frontend on port 3000:

```bash
cd frontend && npm ci && API_ORIGIN=http://127.0.0.1:8000 npm run dev
```

## Checks (as CI runs them)

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest tests/ -q
python3 scripts/audit_gate.py
cd frontend && npm ci && npm run lint && npm run build
```

CI (`.github/workflows/ci.yml`) runs five jobs: `test`, `postgres`, `frontend`,
`e2e` and `audit`.

## Benchmark

```bash
uv run python -m benchmark.harness.stack_main --company A --out benchmark/runs/X \
  --profile benchmark/harness/profiles/after.json [--subset-file benchmark/harness/smoke.txt]
uv run python -m benchmark.harness.score benchmark/runs/X \
  --profile benchmark/harness/profiles/after.json
```

History is in [benchmark/CHANGELOG.md](benchmark/CHANGELOG.md); production runs
and their procedure in [benchmark/production/README.md](benchmark/production/README.md).

---

## Vendor Bank Details and Bank-Change Approval

Bank details reach a vendor only through a change proposed by a clerk or above and decided by a different approver or above. Self-decision is refused, and only one change per vendor can be pending. The supplier import (`PUT /api/vendors`, admin-only) never writes bank fields or the email domain; admins set the email domain with `PATCH`.

Pipeline step 3.4 compares each invoice against the vendor record and holds it for review, never rejecting it, on any of these signals: `bank_mismatch`, `bank_unverified`, `payee_name_differs`, `recent_bank_change` (window set by `bank_change_recent_days`, default 30) and `sender_domain_mismatch` (emailed invoices; Reply-To beats From). Comparison is literal, with no O/0 folding.

Without a database, the vendor list works from the per-tenant file and the bank-change routes answer 503.

Routes: `GET/PUT /api/vendors`, `PATCH /api/vendors/{vendor_id}`, `POST /api/vendors/{vendor_id}/bank-changes`, `GET /api/vendors/bank-changes`, `POST /api/vendors/bank-changes/{change_id}/decide`, `POST /api/invoices/{case_id}/bank-change`.

---

## Approval Workflows

Routing applies only to invoices a person must touch: those the pipeline routes to REVIEW, or whose total reaches an amount tier's minimum (optionally restricted to a currency). An AUTO invoice below every tier gets no approval steps and stays touchless.

The chain order is fixed: vendor rules (by normalised vendor name), then entity rules (organisation name, matched the way step 1.2 matches the customer), then the single highest matching amount tier. If nothing matches, the chain is one step for anyone with the approver role. A duplicate assignee collapses to its first position.

Steps are sequential and the lowest pending one is current. An assignee is a user, a group (any member may decide) or a role. An active delegate of the assignee may decide, and the decision is recorded as acting for them. Each step has a due date (`approval_sla_days`, default 3); the worker escalates an overdue step to the assignee's manager or, failing that, to admins. Nothing is ever auto-approved.

Roles: viewer, clerk, approver, **controller** and admin. A controller is a finance role that ranks with approver for every approval above (it can be a step, a delegate or a manager), is never an admin, and alone approves or rejects the registration of an automated agent (agent control): an agent an admin registers does nothing until a controller approves it. API keys never hold the controller role, and a `role:controller` chain step is decided only by a controller (never an approver or admin, even when it is overdue). Such a rule needs an active controller, and the last one cannot be demoted or deactivated while a rule or a waiting invoice needs one.

Segregation: the uploader is refused at every step; whoever decided an earlier step cannot decide the next; someone who corrected the invoice's fields cannot be the sole approver, so a second step is added. Admins may override, with a recorded reason.

Completing the chain writes the same workflow decision as before, so export, the approved sheet and the learning loop are unchanged. A rejection at any step ends the chain. Editing the total, vendor, customer or currency re-routes the pending steps; decided steps stay, and a decided invoice is never re-opened.

Without a database the approval routes answer 503 and approval behaves as before (a single approver).

Routes: `GET /api/approvals/mine`, `GET /api/invoices/{case_id}/approvals`, `POST /api/invoices/{case_id}/actions` (decides one step; admins may add `override_reason`), groups, rules and delegations under `/api/approvals/...`, and `PATCH /api/admin/users/{user_id}` to set a user's manager.
