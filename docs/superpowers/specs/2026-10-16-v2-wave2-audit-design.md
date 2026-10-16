# Wave 2 — Trust Audit backend (FR-29) design

**Program plan:** `docs/superpowers/plans/2026-10-16-v2-wave2-program.md` (decisions, ownership, execution). **Contracts:** `docs/superpowers/specs/2026-10-14-v2-trust-layer-design.md` §3.1.

### A. Trust Audit backend (FR-29) — `trust/audit/`, `api/trust_audit_routes.py`

**Routes** (approver unless noted): `POST /api/trust/audits` `{name, period_from, period_to, vendors[], categories[], terms, approver}` → 201; `GET /api/trust/audits` (viewer); `GET/PATCH /api/trust/audits/{id}` (GET viewer: audit + `runs[]` + `samples[]` with precision); `POST .../runs` `{engines?}` → 202 run `queued`, enqueues `trust.audit.run`, 409 while one is queued/running; `GET .../runs/{run_id}` (viewer; the UI polls it and `/findings?since=started_at`); `POST .../samples` `{size 1..200, seed?}` (400 if scope smaller); `PATCH .../samples/{sid}` `{verdicts}` (clerk) → sample + `{validated, true, false, precision}`; `GET .../report?format=json|html` (viewer); `GET .../ledger?format=json|csv` (viewer; CSV via `exports.write_csv`, formula-safe).

**Batch** (`audit/batch.py`): `run_batch(ctx, run_id, *, graph, links, url=None, store=None, findings=None, now=None)`: `queued|failed → running` (`done` → no-op; `running` → re-run, idempotent by `dedupe_key`); settings wrapper lifts requested `off` engines to `advisory`; `evaluate(ctx, None, ..., on_progress=store.progress(run_id))`; success → `done` with `per_engine = report`; exception → `failed` + `error`, re-raise (worker retries then dead-letters). Per §3.1.4 an engine failing inside `evaluate` is contained (`"failed"` in the report; the UI shows it red with "re-run"). A run never deletes findings.

**Sampling** (`audit/sampling.py`): `scope_findings(audit, findings, graph)` (period by transaction date, vendor by `issued_by`, categories by line keyword; empty = no filter), `draw(scope, size, seed) -> ids` (`random.Random(seed).sample`), `precision(verdicts) -> {validated, true, false, precision|None}`.

**Report** (`audit/report.py`): pure `assemble(audit, scope, events, *, graph, run, now) -> dict` with `report_version: 1`, `summary {findings, detected_impact, accepted_impact, recovered, prevented, written_off, at_risk_holds, currency}`, `by_type[]`, `by_vendor[]`, `top_recoveries[≤20 with citations + source]`, `prevention[]` (static table: LEAK/DUP/STMT/RADAR → leakage advisory; PAY → payment enforcing; PROV-TAMPER → provenance enforcing; GST → gst_itc enforcing; with the `thresholds.DEFAULTS` names involved), `itc_exposure`, `ledger`. HTML via `audit/render.py` → `trust/html.py`.

**Ledger + fee** (`audit/ledger.py`, Decimal, `ROUND_HALF_UP`, `quantize("0.01")`): `rows(scope_ids, events)` `{finding_id, type, vendor, to_status, amount, attested_by, at, reason}`; `totals(rows)`; `fee(terms, recovered, prevented)` = `recovered×pct + prevented×pct`, then `min(·, cap)` if cap, then `max(·, minimum)` if minimum; every intermediate returned as str.

**Tests:** `test_trust_audit.py` (fee table-driven incl. rounding/zero/cap-only/min-only; scope; draw determinism; precision; assemble reconciles with ledger, top-20 order, prevention rows; run_batch states), `test_trust_audit_routes.py` with `PROBED` + two-tenant probes (404 across tenants; CSV has no foreign vendor).
