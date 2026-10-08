# Vendor bank details + bank-change dual control + BEC signals — design

Phase 3 ("Parity") sub-project 1. Design approved section by section with the user on
2026-10-08 (superpowers brainstorming, architectural path). The implementation plan is written
from this document; nothing here is built before that plan exists.

## Context

Payment-redirection fraud (BEC) works by changing a vendor's bank account. The product plan calls
vendor master + bank-change dual control + BEC signals a buying criterion. Today:

- Vendors are a per-tenant JSON file (`paths.vendor_master_file`) with only `name`, `tax_id`,
  `kind`; API is replace-the-whole-list (`GET`/`PUT /api/vendors`, config_routes.py:308-392);
  no frontend page.
- The extractor already pulls `bank_account_name/number/code/iban` (general_invoice_agent.py:707-713,
  prompt :1438-1443, :1504) and `confidence.py` grounds them (GROUNDED_FIELDS :68-71). Nothing
  compares them to anything.
- Inbound email keeps only recipient + message id (ingest.py:364-417). Sender is not stored.
- **Prod gap:** API and worker are separate containers (runner.py comment ~:224); a JSON master
  written by the API is invisible to the worker. Dual control on a file is theatre → DB tables.
- Baseline measured on `benchmark/runs/after-ecb9bbd` vs `benchmark/dataset/vendor_master.csv`
  (has bank/account/ifsc/email columns): 247/300 print bank details; 245 match exactly;
  2 near-misses (MFG-INV-011 IFSC `DTBKO117671` OCR O/0; MFG-INV-003 `1 974810581534` stray
  prefix); 53 none. No bank faults planted in the dataset.

Decisions taken with the user: mismatch → **hold for REVIEW, never reject**; change proposed by
clerk+, approved by a *different* approver+ (self-approval refused, new behaviour — rules.py only
records `self_approved`); all five BEC signals in v1; vendors page + workbench action; storage
approach **A** (Postgres tables).

## Section 1 — Data model and storage

One Alembic migration, `down_revision = "d5b81a6c39f4"`, template
`db/migrations/versions/a7c4e91f0b32_record_locks.py` (RLS via `enable_rls_statements`, skipped
on non-Postgres). Add both tables to `TENANT_TABLES` (models.py:504-518).

**`vendors`** (TenantMixin + TimestampMixin): `name`, `tax_id`, `kind`, `email_domain` (lowercase,
nullable), `bank_account_name`, `bank_account_number`, `bank_code`, `bank_iban` (verified payee
details, nullable), `bank_verified_at`, `bank_verified_by`, `normalized_name`
(`po_matching.normalize_vendor`), unique `(tenant_id, normalized_name)`.

**`vendor_bank_changes`**: `vendor_id` FK, `proposed_account_name/number/code/iban`,
`previous_*` (four, snapshot), `reason` (required), `source_case_id` (nullable), `status`
`pending|approved|rejected`, `proposed_by`, `decided_by`, `decided_at`, `decision_note`. One
pending change per vendor: partial unique index on Postgres, checked in code for SQLite.

Model-enforced rules: only the approve action writes bank fields onto `vendors`;
`decided_by != proposed_by`; a pending change blocks a second.

Unchanged: `VendorMaster(records=...)` constructor (vendor_master.py:57). `Vendor` dataclass
gains the bank fields + `email_domain`. `tenant_settings.apply_to_pipeline` (:345-375) loads
records from the table when a DB is configured and sets `config["vendor_master_records"]`;
`_load_vendors()` (agent :2030) prefers records over the file path. Without a DB (benchmark,
replay, SQLite dev without seed) the file path is used as today. `PUT /api/vendors` stays as an
admin bulk upsert of name/tax_id/kind only — never bank fields. No data migration.

Document gains `sender`, `reply_to` columns (same migration).

## Section 2 — Pipeline check 3.4 "Bank details"

New pure module `invoice_processing/bank_check.py` (pattern: `vendor_master.py`, `duplicates.py`).
Phase3ValidatorAgent.run (agent :2101) calls it after 3.2, reusing the 3.2 `VendorMatch`.
YAML step `3.4` in `invoice_master_data.yaml` phase3 (~:254) with `informational: true` so it
never rejects and shows in Settings / `disabled_checks`. `_settle` (:1596) already skips
informational failures.

Signals (each its own evidence line, most severe first):

| id | fires when |
|---|---|
| `bank_mismatch` | vendor verified; invoice account number or code differs |
| `bank_unverified` | vendor known, no verified details, invoice prints some |
| `payee_name_differs` | invoice `bank_account_name` doesn't normalise to vendor name |
| `recent_bank_change` | pending change exists, or approved within `bank_change_recent_days` (default 30) |
| `sender_domain_mismatch` | document came by email, vendor has `email_domain`, sender domain differs |

Comparison: account digits-only; code uppercased, spaces removed; **no O/0 folding** (per
confidence.py:85-91 reasoning). Silent on: unknown vendor (3.2 covers), master not configured,
no bank details printed, exact match (passed, evidence "Bank details match vendor record").

Routing: on any signal the step sets `invoice["_routing"]="REVIEW"`, appends `bank: <signal>` to
`_evidence._summary.fields_needing_review`, and re-saves `02_extraction.json` (so
`service._summarize` :582, `case_projection` :166 and the sheet read it unchanged). Decision stays
ACCEPT when Phase 4 passes.

Sender capture: `_parse_mime`/`_parse_postmark`/`_parse_sendgrid` read From + Reply-To
(Postmark `FromFull`/`ReplyTo`, SendGrid `from`); `InboundMessage` gains `sender`, `reply_to`;
`ingest_document` stores them on the Document; worker (`process_document`, runner.py:192) sets
`config["document_sender"]` (Reply-To wins over From) before `process_invoice`.

Setting: `bank_change_recent_days` (int, 30) in `tenant_settings.SETTABLE`, default in
`agent.DEFAULT_CONFIG`, `DESCRIPTIONS` entry.

## Section 3 — API and dual control

New `invoice_processing/api/vendor_routes.py`, mirroring config_routes.py (principal via
`require_role`, `service.get_audit().emit(principal.context(), ...)`, `HTTPException(400, detail)`).
Every route → probe + `PROBED` entry in `tests/test_tenant_isolation_api.py` (:785-846 guard).

| Route | Role | Behaviour |
|---|---|---|
| `GET /api/vendors` | clerk | from table; masked account (last 4), `bank_verified_at`, `pending_change_id` |
| `PUT /api/vendors` | admin | bulk upsert name/tax_id/kind by normalized name; never bank/email; audit `vendors_replaced` |
| `PATCH /api/vendors/{id}` | admin | `email_domain` only; audit `vendor_updated` with before |
| `POST /api/vendors/{id}/bank-changes` | clerk | 4 bank fields (account number required), `reason`, optional `source_case_id`; 409 if pending exists; audit `vendor_bank_change_proposed` |
| `GET /api/vendors/bank-changes?status=` | clerk | queue, newest first, previous vs proposed |
| `POST /api/vendors/bank-changes/{id}/decide` | approver | `{action: approve\|reject, note}`; **403 if `principal.email == proposed_by`** ("The person who proposed a bank change cannot approve it"); approve copies values + `bank_verified_*`; 409 if not pending; audits `..._approved` / `..._rejected` |
| `POST /api/invoices/{case_id}/bank-change` | clerk | thin wrapper: proposal from the case's extracted bank fields + vendor, `source_case_id=case_id` |

`PgVendorRepository` in `db/repositories.py` (load_records, list, upsert_many, patch, propose,
list_changes, decide) using `tenant_session` + explicit `tenant_id` filters (SQLite has no RLS).
Add `"vendor": "vendor"` to `_ENTITY_TYPES` (:567). Masking only at the API boundary. Approving
an invoice never approves a bank change.

## Section 4 — Frontend

`/vendors` page (nav `min: "clerk"` in AppShell.tsx:33-41, between Purchase orders and
Settings), patterned on `purchase-orders/page.tsx` (`usePoll`, `can(me, ...)`, notice/problem
panels, `data-table`, badges):
- Vendors table: name, tax ID, kind, email domain, bank badge `verified|unverified|pending`;
  row action "Request bank change" → inline form (4 fields + reason). Admin inline edit of
  email domain. Admin "Import list" keeps the JSON bulk-load.
- Pending changes panel (only when non-empty): previous vs proposed, proposer, time, reason,
  link to source invoice; Approve/Reject for approvers, disabled with tooltip "You proposed this
  change" for the proposer; reject requires a note; `window.confirm` both.

Invoice page (`invoices/page.tsx`, Findings :643): when a 3.4 finding fired, clerk+ sees
"Propose these bank details for <vendor>" → workbench shortcut → notice with link to `/vendors`;
if a pending change exists, show "A bank change for this vendor is awaiting approval" + link.

Types in `lib/api.ts`: `Vendor`, `VendorList`, `BankChange`, `BankChangeList`. One Playwright
e2e: propose, self-approval refused, approve as second user, badge becomes verified.

## Section 5 — Testing, measurement, rollout

Tests (one focused class per behaviour, each verified to fail without the change via
`git stash push -- invoice_processing`):
- `tests/test_bank_check.py`: five signals fire on motivating cases; silent on exact match /
  none printed / unknown vendor / unconfigured; digits-only + uppercase; no O/0 folding
  (MFG-INV-011 stays a hold).
- `tests/test_vendor_master.py`: records with bank fields + email_domain.
- `tests/test_vendor_routes.py`: propose; 409 second pending; 403 self-approve; approve copies +
  stamps; reject terminal; PUT never alters bank fields; workbench shortcut uses extracted values.
- `tests/test_tenant_isolation_api.py`: probes for all new routes.
- `tests/test_ingestion.py`: From/Reply-To for MIME, Postmark, SendGrid; Reply-To wins; stored.
- `tests/test_worker.py`: sender reaches config; 3.4 hold projects as routing REVIEW.
- Migration test on SQLite like existing ones.

Benchmark: `benchmark/harness/dataset.vendor_records` gains bank/account/ifsc/email; replay
`--out` over `after-ecb9bbd`. Expect 245 pass, 2 hold, 53 silent. Safety: posted-untouched-wrongly
stays 1 (MFG-INV-039). Cost: clean posted untouched stays 40 unless a near-miss is among the 45
clean — then record in `benchmark/CHANGELOG.md` as the price of no O/0 folding, don't tune.
Then plant 2 manual bank-change faults (account, IFSC) and confirm both hold.

Rollout order: migration + repository → `bank_check` + tests → Phase 3 wiring + routing → email
sender capture → API routes + probes → frontend → replay + CHANGELOG. Suite green after each.

Out of scope: vendor portal, callback verification workflow, look-alike domain scoring, external
bank registries, migration of existing JSON vendor files.

## Verification (end-to-end, once implemented)

```bash
cd /Users/vineet/Downloads/ADK/invoice-processing
./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format --check .
bash benchmark/harness/gates.sh
./.venv/bin/python -m benchmark.harness.replay --run after-ecb9bbd --profile benchmark/harness/profiles/after.json --out replay-bank-check
./.venv/bin/python -m benchmark.harness.score benchmark/runs/replay-bank-check --profile benchmark/harness/profiles/after.json
```
Manual stack (`preview_start manual-a-api-worker`, `manual-frontend`): propose a change as clerk,
confirm 403 on self-approve, approve as approver, upload an invoice with a differing IFSC and
confirm it lands in REVIEW with the 3.4 evidence and the "Propose these bank details" button.
