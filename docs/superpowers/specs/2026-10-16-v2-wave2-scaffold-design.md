# Wave 2 scaffold design

**Program plan:** `docs/superpowers/plans/2026-10-16-v2-wave2-program.md` (decisions, ownership, execution). **Contracts:** `docs/superpowers/specs/2026-10-14-v2-trust-layer-design.md` §3.1.

## Part 1 — Scaffold (sequential, branch `v2/w2-scaffold` from `main`)

Executed by me, TDD, with its own plan (`docs/superpowers/plans/2026-10-16-v2-wave2-scaffold.md`). Deliverables:

**S1 Migration + models.** `invoice_processing/db/migrations/versions/f4a5b6c7d8e9_trust_wave2.py` (revises `e2f3a4b5c6d7`; own `RLS_TABLES` literal; `enable_rls_statements`). `db/models.py` + `TENANT_TABLES`:
- `trust_audits`: `name(128)`, `status(16)` draft|active|closed, `period_from/period_to` Date, `vendors` JSON (vendor node ids; empty = all), `categories` JSON (keywords matched on line `gl_account`/description; empty = all), `terms` JSON `{recovered_pct, prevented_pct, cap, minimum}` (Decimal strings), `approver(320)`, `created_by(320)`.
- `trust_audit_runs`: `audit_id` FK, `status(16)` queued|running|done|failed, `engines` JSON, `progress` JSON `{engines_done, engines_total, engine, findings_so_far, per_engine}`, `error(500)`, `requested_by`, `started_at`, `finished_at`.
- `trust_audit_samples`: `audit_id` FK, `size` int, `seed(32)`, `finding_ids` JSON, `verdicts` JSON `{finding_id: bool|null}`, `created_by`.
- `trust_explanation_ratings`: `finding_id` FK, `rater(320)`, `correct` bool, `useful` bool, `note(500)`, unique `(tenant_id, finding_id, rater)`.
- Tests: `test_trust_schema.py`, `test_gcs_and_migrations.py` param `("f4a5b6c7d8e9", 4)`, `test_restore_check.py` head pin.

**S2 FindingStore extensions** (`trust/findings/store.py`): `list(..., statuses=None, types=None, vendor_node=None, since=None, until=None, limit=None, offset=0)` (vendor via `issued_by` edge join; order `created_at desc, id`), `count(...)`, `summary(tenant_id) -> {by_status, by_type, vendors[top 50]}`, `events(tenant_id, *, to_status=(), since=None, until=None)` (event row + finding type/node/impact/currency = the raw ledger). Engine/hold filters are type sets computed by the route from `TYPE_ENGINE`/`HOLD_TYPES` (no JSON-path filtering).

**S3 Accessors, stubs, shared helpers.** `service.get_finding_store()` (None without the table). Empty packages `trust/audit/`, `trust/explain/`. Stub routers `api/trust_audit_routes.py`, `api/trust_explain_routes.py` (just `router = APIRouter()`), mounted in `service.py`. `trust_routes.source_of(graph, tenant_id, node) -> {document_url, page}|None` (invoice/`document_version` → `/api/invoices/{case_id}/document`; `contract`/`contract_clause` → `/api/trust/contracts/{document_id}/file` + `attrs.page`), `trust_routes.vendor_of(graph, tenant_id, node_id) -> {node, name}|None`. `trust/html.py`: `page(title, body)` with inline `@media print` CSS, `table(columns, rows)`, `kv(pairs)`, all escaped (~40 lines).

**S4 Routes + lineage fix + isolation.** In `trust_routes.py`:

| Method | Path | Role | Shape |
|---|---|---|---|
| GET | `/api/trust/findings` | viewer | `?status&open=true&type&engine&vendor=<node>&hold=true&since&until&limit≤200&offset` → `{findings:[row + vendor + engine + hold], total, offset, limit}` |
| GET | `/api/trust/findings/summary` | viewer | `{by_status, by_type, hold_open, vendors:[{node,name,count}]}` |
| GET | `/api/trust/findings/{id}` | viewer | store dict (citations, events) + `vendor` + `citations[i].node` (masked) + `citations[i].source` + `transaction{node,source}` + `lineage[]` + `allowed_transitions[]` |
| POST | `/api/trust/findings/{id}/transition` | clerk / approver by status | `{to_status, reason?, amount?, expect?}` → finding; 400 missing reason/bad amount; 403; 409 `IllegalTransition`/`StatusChanged` with `detail.current` |
| GET | `/api/trust/nodes/{node_id}` | viewer | `{node, source, edges_out≤50, edges_in≤50}` |
| GET | `/api/trust/lineage/{node_id}` | viewer | `?as_of` → `{node_id, links:[{stage,status,node_id,note}]}` |
| GET | `/api/trust/contracts/{document_id}/file` | viewer | PDF bytes (kind contract, this tenant, else 404) |
| GET | `/api/trust/contracts/{document_id}/clauses` | viewer | `[{node_id,type,params,page,excerpt,confidence,reviewed,since}]` |
| PATCH | `/api/trust/clauses/{node_id}` | approver | `{type?, params?}` → clause; new node version with `reviewed: true`; audit `trust_clause_reviewed` |

`trust/graph/lineage.py` contract stage: `out("governed_by", invoice)` → none: `no_evidence "No contract governs this invoice."`; contract's `governs` vendor ≠ invoice's `issued_by` vendor (via `same()`): `contradicts`; else `found`. GL stays `no_evidence`. `tests/test_tenant_isolation_api.py`: `TestTrustFindingsIsolation` + PROBED; `EXTRA_PROBE_MODULES = ("tests.test_trust_audit_routes", "tests.test_trust_explain_routes")` unioned into coverage (ImportError → empty), so the branches never edit this file; branch route tests must define `PROBED` and real two-tenant probes (review checks them).

**S5 Batch job type.** `worker/runner.py`: `TRUST_AUDIT_RUN = "trust.audit.run"`, `enqueue_trust_audit_run(queue, tenant_id, run_id, request_id="")`, `_handle_trust_audit_run` via `_run_claimless(key=f"audit:{run_id}", ...)`; the closure imports `trust.audit.batch.run_batch` (ImportError → dead-letter). `trust/evaluate.py` gains exactly one keyword `on_progress: Callable[[dict], None] | None = None`, called after each engine with `{engine, done, total, findings, report}`. Tests `tests/test_trust_worker.py` (monkeypatched `run_batch`), `test_trust_engine.py` progress order.

**S6 Frontend shell.** `frontend/src/lib/api.ts`: every TS type in the API tables (TrustFinding, TrustFindingPage, TrustFindingDetail, TrustCitation, GraphNodeRecord, LineageLink, Explanation, EvidencePack, TrustAudit, AuditRun, AuditSample, AuditReport, LedgerExport, ContractClause, RatingSummary) + `findingsQuery(filters)`. `frontend/src/lib/trust.ts`: `STATUS_LABELS`, `TRANSITIONS` (mirror of `model.TRANSITIONS`), `REASON_REQUIRED`, `money()`, `ENGINE_LABELS`. `AppShell.tsx` NAV += Findings (`/findings`, viewer), Trust audit (`/trust-audit`, approver). Placeholder pages `app/findings/page.tsx`, `app/trust-audit/page.tsx` (h1 only). `components/trust/EvidenceViewer.tsx` (left: explanation/steps; right: `objectUrl(document_url)` → `<iframe src=url#page=N>` or a `.kv` node record; j/k + arrows). `globals.css`: `.evidence-split`, `.chain`, `@media print` hides shell. e2e nav list += both labels; `tests/test_frontend_reaches_the_flow.py` VIEWS += `findings`, `trust-audit`. `scripts/e2e_api.py`: after projecting cases, `sync_tenant` + upsert one `LEAK-RATE` finding citing the `case_005` invoice node with realistic `details` (so e2e can triage/explain without a worker).

**S7 Docs + benchmark hook.** Specs committed: `docs/superpowers/specs/2026-10-16-v2-wave2-{scaffold,audit,explain,frontend}-design.md` (Part 2 text). `benchmark/history/eval.py`: `try: from . import explain as _ex` → `out["explain"] = _ex.measure(tenant_id, graph, findings, manifest, url)`, gates merged into `out["gates"]`; absent → no-op. `docs/PENDING.md` Wave 2 bullets + deferrals. CI unchanged (the `tests/test_trust_*.py` glob covers new files).

### File ownership (in every brief; checked at review)

| Branch | MAY touch | MUST NOT touch |
|---|---|---|
| `v2/w2-audit` | `trust/audit/**`, `api/trust_audit_routes.py` (fills the stub), `tests/test_trust_audit*.py`, `tests/fixtures/trust_audit/**`, its plan doc | everything else: `trust_routes.py, service.py, runner.py, evaluate.py, findings/*, graph/*, models.py, migrations/**, html.py, explain/**, frontend/**, test_tenant_isolation_api.py, eval.py` |
| `v2/w2-explain` | `trust/explain/**`, `api/trust_explain_routes.py`, `benchmark/history/explain.py`, `tests/test_trust_explain*.py`, `tests/fixtures/trust_explain/**`, its plan doc | everything else incl. `audit/**` and every engine package (templates live in `explain/templates/`) |
| `v2/w2-frontend` | `frontend/src/app/findings/**`, `app/trust-audit/**`, `app/contracts/**`, `components/trust/**` (new files only; not `EvidenceViewer.tsx`), `frontend/e2e/trust.spec.ts`, its plan doc | `api.ts, trust.ts, AppShell.tsx, globals.css, EvidenceViewer.tsx, golden-path.spec.ts`, all Python |

A needed shared change is a scaffold follow-up commit on `main`, then rebase.
