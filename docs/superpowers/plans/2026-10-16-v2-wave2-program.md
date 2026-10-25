# v2 Wave 2 — Surfaces: Program Plan

**Spec it implements:** `docs/superpowers/specs/2026-10-14-v2-trust-layer-design.md` §2 (Wave 2 row), §3.1, §5, §6. PRD FR-24, FR-29, FR-30 (part), §8, §9.
**Builds on:** `main` @ `cab0506` (Wave 0, Wave 1, follow-ups all merged). Alembic head `e2f3a4b5c6d7`.

## Context

Waves 0–1 built the trust layer with no user surface: findings, graph, lineage and four engines exist, but nothing lists a finding, explains it, or runs the engines over a tenant's history on demand. Wave 2 adds the surfaces that make the money visible: a findings queue with decisions, a cited "Why?" explanation with an auditor evidence pack, and the Trust Audit workspace (engagement → batch run → sample → report → recovery ledger → contingency fee). V2-A exits when this merges with its gates green.

Same shape as Wave 1: a **sequential scaffold** that owns every shared file, then **three parallel branches** (audit backend, explain backend, frontend) with disjoint ownership, each with its own plan, fable review, one fix pass, PR; merge on the user's word with `--match-head-commit`. Never `--auto`.

## Decisions (taken with the user, 2026-10-16)

| # | Decision | Why | Cost if wrong |
|---|---|---|---|
| 1 | **Core cut.** IN: findings queue (+ hold-only "Payment Trust queue"), finding detail with Why + evidence viewer + transitions, lineage view, Trust Audit workspace (engagement, batch run, sampling, report, ledger + fee), Why engine (counterfactual, auditor pack, ratings), minimal clause editor. OUT → PENDING: partner mode, mobile, notifications, trust strip, `/v2` public API + webhooks. | Partner console/mobile/channels have no v1 foundation; trust strip is Wave 3's score. | Partners wait for Wave 3 for API/webhooks. |
| 2 | **PDF = print-styled HTML + JSON.** Server renders HTML (`trust/html.py`, no Jinja — only a transitive dep); the browser prints to PDF. | Zero new dependency; one assembler feeds both formats; curl-able by an auditor. | Emailed PDFs need WeasyPrint later. |
| 3 | **Why engine = deterministic templates**, no model call. Templates cite only what the engine cited; `check()` enforces it. | FR-24.2 by construction; 100% citations (AC-24) guaranteed; instant; free. | A model-phrased narrative (Wave 3) layers on via `decide()`. |
| 4 | **Contingency terms** per engagement: 25% recovered, 10% attested prevented, optional cap and minimum; all editable. Fee = computed record + CSV/JSON, not billing. | PRD Q1 default. | None; fields are data. |
| 5 | **No `trust_recoveries` table.** Attestation = the approver's `recovered`/`prevented` transition (event row: amount, actor, reason, at). Engagement scope is a filter (period/vendors/categories), not a FK. | Events already are the ledger. | Overlapping engagements double-count → add `audit_id` to events then. |
| 6 | **One migration** (scaffold): `trust_audits`, `trust_audit_runs`, `trust_audit_samples`, `trust_explanation_ratings`. Explanations/packs/reports are regenerated, never stored (versioned by `explain_version`, sha-stamped). | Deterministic outputs need no storage. | Add a pack blob table if an auditor demands server retention. |
| 7 | **A batch run lifts `off` engines to `advisory` for that run only**; never downgrades `enforcing`. | An audit must run engines the tenant hasn't enabled. | If objectionable, require explicit `engines[]`. |
| 8 | **Roles by target status:** clerk → `triaged`, `needs_vendor_input`, ratings, sample verdicts; approver → money statuses, engagements, runs, samples, clause edits. | One `_role_for(status)` table. | One table to change. |
| 9 | **Frontend is one branch.** The pages share the evidence viewer, finding rows, status vocabulary and the transition dialog; the long pole is the two backends. | Splitting forces the scaffold to pre-build nearly every component. | Cut at `app/findings/**` vs `app/trust-audit/** + app/contracts/**` if capacity demands. |
| 10 | Vendor filters use vendor **graph node ids** everywhere. Evidence highlight is `#page=N` only (no region). Clause PATCH = review (`reviewed=True`). A sample verdict is validation data; it does not transition the finding. `amount` on recovered/prevented/written_off defaults to the finding's impact and is always stored. | Smallest designs. | Each is one field/toggle to extend; region highlight and clause impact assessment → PENDING. |

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

## Part 2 — Sub-project specs (approved together with this plan)

### A. Trust Audit backend (FR-29) — `trust/audit/`, `api/trust_audit_routes.py`

**Routes** (approver unless noted): `POST /api/trust/audits` `{name, period_from, period_to, vendors[], categories[], terms, approver}` → 201; `GET /api/trust/audits` (viewer); `GET/PATCH /api/trust/audits/{id}` (GET viewer: audit + `runs[]` + `samples[]` with precision); `POST .../runs` `{engines?}` → 202 run `queued`, enqueues `trust.audit.run`, 409 while one is queued/running; `GET .../runs/{run_id}` (viewer; the UI polls it and `/findings?since=started_at`); `POST .../samples` `{size 1..200, seed?}` (400 if scope smaller); `PATCH .../samples/{sid}` `{verdicts}` (clerk) → sample + `{validated, true, false, precision}`; `GET .../report?format=json|html` (viewer); `GET .../ledger?format=json|csv` (viewer; CSV via `exports.write_csv`, formula-safe).

**Batch** (`audit/batch.py`): `run_batch(ctx, run_id, *, graph, links, url=None, store=None, findings=None, now=None)`: `queued|failed → running` (`done` → no-op; `running` → re-run, idempotent by `dedupe_key`); settings wrapper lifts requested `off` engines to `advisory`; `evaluate(ctx, None, ..., on_progress=store.progress(run_id))`; success → `done` with `per_engine = report`; exception → `failed` + `error`, re-raise (worker retries then dead-letters). Per §3.1.4 an engine failing inside `evaluate` is contained (`"failed"` in the report; the UI shows it red with "re-run"). A run never deletes findings.

**Sampling** (`audit/sampling.py`): `scope_findings(audit, findings, graph)` (period by transaction date, vendor by `issued_by`, categories by line keyword; empty = no filter), `draw(scope, size, seed) -> ids` (`random.Random(seed).sample`), `precision(verdicts) -> {validated, true, false, precision|None}`.

**Report** (`audit/report.py`): pure `assemble(audit, scope, events, *, graph, run, now) -> dict` with `report_version: 1`, `summary {findings, detected_impact, accepted_impact, recovered, prevented, written_off, at_risk_holds, currency}`, `by_type[]`, `by_vendor[]`, `top_recoveries[≤20 with citations + source]`, `prevention[]` (static table: LEAK/DUP/STMT/RADAR → leakage advisory; PAY → payment enforcing; PROV-TAMPER → provenance enforcing; GST → gst_itc enforcing; with the `thresholds.DEFAULTS` names involved), `itc_exposure`, `ledger`. HTML via `audit/render.py` → `trust/html.py`.

**Ledger + fee** (`audit/ledger.py`, Decimal, `ROUND_HALF_UP`, `quantize("0.01")`): `rows(scope_ids, events)` `{finding_id, type, vendor, to_status, amount, attested_by, at, reason}`; `totals(rows)`; `fee(terms, recovered, prevented)` = `recovered×pct + prevented×pct`, then `min(·, cap)` if cap, then `max(·, minimum)` if minimum; every intermediate returned as str.

**Tests:** `test_trust_audit.py` (fee table-driven incl. rounding/zero/cap-only/min-only; scope; draw determinism; precision; assemble reconciles with ledger, top-20 order, prevention rows; run_batch states), `test_trust_audit_routes.py` with `PROBED` + two-tenant probes (404 across tenants; CSV has no foreign vendor).

### B. Causal Why engine (FR-24) — `trust/explain/`, `api/trust_explain_routes.py`

**Layout:** `model.py` (frozen `Step(text, node_id)`, `Actor(kind, label, node_id|None)`, `Evidence(node_id, kind, label, excerpt, source)`, `Counterfactual(currency, payable_now, payable_correct, delta, formula, note)`, `Explanation(finding_id, type, version, what_happened, why, caused_by, evidence, confidence, confidence_note, next_action, would_change, counterfactual, versions, transaction_node)`, `EXPLAIN_VERSION="explain-1"`, `check()`); `engine.py` `build(tenant_id, finding, *, graph, links=None) -> Explanation`; `counterfactual.py`; `templates/{__init__,leakage,payment,provenance,gst_itc,generic}.py` (`register(code)` decorator; `TEMPLATES` dict); `pack.py` `evidence_pack(...) -> dict` (`pack_version: 1`: generated_at, finding, explanation, lineage, masked nodes + edges, versions incl. `explain_version`, decision {status, owner_id, decided_at, events}, ratings, `sha256` over canonical JSON); `render.py` `pack_html(pack)`; `ratings.py` `RatingStore(url).rate(...)` (upsert per rater; audit `trust_explanation_rated`), `.aggregate(tenant_id) -> {type: {rated, correct, useful}}`.

**Citation enforcement:** `check()` raises `Uncited` unless `evidence` non-empty, every `Step.node_id` ∈ citation ids ∪ {transaction_node}, every `Actor.node_id` likewise or None, no empty step text. Templates read `details` defensively and cite only cited nodes → FR-24.2 by construction. **Generic fallback** for any type without a template: `what_happened = summary`; steps from `TYPES` description + each citation excerpt; `caused_by` vendor + engine; next action "Review the cited evidence and accept or reject." Satisfies `check()` because the DB guarantees ≥ 1 citation.

**Per-family templates** (implementer re-reads each engine's `details` keys first): LEAK-RATE/ESCALATOR/TIER (clause → line billed → over by impact; would_change: reviewed clause with another rate, statement credit; next: short-pay/claim), LEAK-REBATE/DISCOUNT/TERM + RADAR-* (clause → timing/spend → missed ₹), DUP-*/STMT-* (earlier → this, same amount/number; next: hold the later), PAY-* (provider evidence → verdict; would_change: verified result, call-back), PROV-SUSPECT/TAMPER (score components / approved twins), GST-* (lookup → status → ITC). `caused_by` is heuristic, worded "likely", persons only as the approver on record.

**Counterfactual** (Decimal): LEAK-RATE/ESCALATOR/TIER/REBATE/DISCOUNT → `payable_correct = amount − impact`, formula from details; DUP-*/STMT-DOUBLE-PAID/UNAPPLIED-CREDIT → pay once (`0` for the later copy); PAY-*/PROV-*/GST holds → unchanged, delta = exposure "amount at risk if paid now"; GST-ITC-RISK → "ITC claimable if the vendor is active/files"; impact None → `note="no money is computed for this finding"`.

**Routes:** `GET /api/trust/findings/{id}/explanation` (viewer); `GET .../evidence-pack?format=json|html` (viewer; HTML `Content-Disposition: inline`); `POST .../rating` `{correct, useful, note?}` (clerk); `GET /api/trust/explanations/ratings` (viewer).

**Benchmark** (`benchmark/history/explain.py` `measure(...)`): `explanation_citation_coverage` (share of eval-DB findings whose `build()` passes `check()` with ≥1 evidence) gate `== 1.0`; `counterfactual_impact_error` (Σ|delta − expected_impact| / Σexpected over priced manifest faults, same matching as `_score`) gate `≤ 0.01`.

**Tests:** `test_trust_explain.py` parametrized over **every** `TYPES` code with a fixture per type (`tests/fixtures/trust_explain/<TYPE>.json`) asserting `check()`, ≥1 step, counterfactual math; unknown type → generic; HTML escapes `<script>`; pack sha stable; ratings; `test_trust_explain_routes.py` with `PROBED` + probes.

### C. Frontend (FR-30 part, §8) — `frontend/src/app/{findings,trust-audit,contracts}/**`

| Route | Data | Actions |
|---|---|---|
| `/findings` | `usePoll(/api/trust/findings?…)` + `/summary`; URL filters; hold toggle labelled "Payment Trust queue" | row → detail; quick "Triage" |
| `/findings/[id]` | detail, `/explanation`, pack on demand | `TransitionDialog` (reason required for rejected/written_off; amount prefilled with impact for recovered/prevented; 409 → "moved to X, reload"); tabs Why (sections + correct/useful rating), Evidence (`EvidenceViewer`: steps left, source right), Lineage (`.chain`, seven stages, badges), History (events); "Evidence pack" HTML/JSON |
| `/trust-audit` | `/api/trust/audits` | create form (`<input type="date">`, vendor multi-select from `/api/vendors` mapped to nodes, categories, terms, approver) |
| `/trust-audit/[id]` | audit; `usePoll(run, 3000)` while active | tabs Overview (scope, terms, progress bar, per-engine chips, Run batch, interim count → `/findings?since=`), Sampling (draw N, ✓/✗ rows, precision tile), Report (tables + `Tile`s, Print view, Download JSON), Ledger (rows, fee card, CSV/JSON), Contracts |
| `/contracts/[document_id]` | clauses + `objectUrl(file)` | type select, params inputs, Save (PATCH), iframe `#page=`, reviewed badge |

e2e `frontend/e2e/trust.spec.ts`: seeded `LEAK-RATE` row → Why shows what_happened + ≥1 evidence → triage → create engagement → Run batch shows queued → sample of 1 ✓ → precision 100% → Report renders "Executive summary" → Ledger has CSV. Pytest adds `test_findings_can_be_triaged`, `test_an_engagement_can_be_created`.

## Part 3 — Execution mechanics

1. **Scaffold** on `v2/w2-scaffold`: plan → TDD → full suite + Postgres glob + `alembic upgrade head && downgrade base` + eval → fable review → one fix pass → PR → user's "merge".
2. **Worktrees** from the merged scaffold: `git worktree add .worktrees/<name> -b v2/w2-<name> main` ×3 (`audit`, `explain`, `frontend`); `uv sync --frozen` in the two Python ones; `npm ci` in the frontend one; Postgres DBs `trust_w2_audit`, `trust_w2_explain` on `pg-w1` (55433).
3. **Implementers:** three subagents in parallel, each writing its plan first (`docs/superpowers/plans/2026-10-16-v2-wave2-<name>.md`), strict TDD, the ownership row, the repo rules (no ids in log extra, masked attrs, every route a probe, Decimal money). Each ends with: ruff, full suite, Postgres (own DB, pg8000), eval (explain branch), `npm run lint && npm run build` + Playwright (frontend), push, report. Then fable review → one fix pass → PR.
4. **Merge order:** scaffold → explain, audit (any order) → frontend last (its e2e drives the other two's routes), each rebased onto `main` once the others land; merge pinned to the reviewed head, on the user's word, after CI green (wait for the run; the repo has no required checks).
5. **Wave close:** all PRs merged, gates green on `main` together, worktrees removed, PENDING + memory updated, post-wave cleanup PR for any duplicated helper.

## Verification (end of Wave 2)

- `main`: full suite; Postgres glob; `alembic upgrade head && downgrade base` on Postgres; `restore_check.repo_head() == "f4a5b6c7d8e9"`; `python -m benchmark.history.eval --seed 7` green incl. `explanation_citation_coverage == 1.0`, `counterfactual_impact_error ≤ 0.01`; `npm run lint && npm run build`; Playwright incl. `trust.spec.ts`.
- Isolation: every new path in a `PROBED` set backed by a real two-tenant probe.
- Manual smoke (local, `AUTH_DISABLED=1`, worker running): import history + upload a contract → engagement → Run batch (progress reaches total, interim findings appear) → sample 10, verdicts, precision → open a `LEAK-RATE` finding: Why cites the clause, Evidence shows the contract page beside it, Lineage `contract: found` → accept → claimed → recovered with amount → Ledger row with the approver + fee (cap/min honoured) → Report prints cleanly → pack JSON sha stable across downloads → edit a clause, re-evaluate, reviewed clause untouched.

## Risks carried into execution

- Templates depend on Wave 1 engines' `details` shapes (internal, untyped): the explain implementer reads each engine first; the per-type fixture tests pin what is read. A shape change later breaks a test, not production (generic fallback still cites).
- `categories` scoping is keyword-weak (v1 has no category master) → PENDING: GL-rule-derived categories.
- Overlapping engagements double-count recoveries (ruling 5).
- Evidence highlight is page-level only; region highlight and clause impact assessment → PENDING.
- Deferred to PENDING: partner mode, notifications, trust strip, mobile, `/v2` API + webhooks, server-side PDF, finding auto-release for insight engines (still open from Wave 1).
