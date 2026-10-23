# Wave 5 scaffold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land every shared change Wave 5's four branches need, with zero behaviour change while `agents` is off.

**Architecture:** Registries and types first; then the one-session `record()` under the guard's lock; then store counts, routes, the intake stub and isolation hand-over; then the frontend shell. No migration.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2, pytest; Next.js 16 / TypeScript; Playwright.

**Spec:** `docs/superpowers/specs/2026-10-23-v2-wave5-scaffold-design.md` (program plan `docs/superpowers/plans/2026-10-23-v2-wave5-program.md`).

## Global Constraints

- Alembic head stays `c3d4e5f6a7b8`; no new revision.
- With `agents` off, no query beyond the one tenant-settings read on any governed path.
- No ids/emails in log `extra`. Every new route in a `PROBED` set.
- Ruff as CI: `uv run ruff check . && uv run ruff format --check .`

## Review Focus

- A record failure inside the guard's lock in enforcing → 503, no ledger row, no finding, no outbox row. Pinned in Task 2.
- The plain `FOR UPDATE` lock: two concurrent approvals still cannot both fit one cap (existing test, Postgres too). Pinned in Task 2.
- A vendor load whose later create is refused leaves no "allowed" row unstamped. Pinned in Task 3.
- `count_blocked` on a tenant with no rows → zeros, not an error. Pinned in Task 4.
- The stub `/v2/agents/actions` answers 503 to both tenants and the doc test stays green. Pinned in Task 5.

---

### Task 1: Registries, `Action.vendor_id`, `_judge` lookups

**Files:** `trust/agents/__init__.py` (ACTIONS), `api/trust_agent_routes.py` (SUBJECT_TABLES, `_judge`), `trust/thresholds.py`, `trust/agents/policy.py`, `trust/agents/guard.py` (`invoice_action`), `api/vendor_routes.py` (patch / propose / decide builders); tests `tests/test_trust_settings.py`, `tests/test_trust_agents_policy.py`, `tests/test_trust_agent_routes.py`, `tests/test_trust_agents_guard.py`.

- [ ] Failing tests: settings snapshot has `agents.banner_window_hours: 24`, `agents.retention_days: 400`; `"po.create" in ACTIONS and "document.upload" in ACTIONS`; `Action(...).vendor_id is None`; `_judge` log row naming a stored vendor → ledger row's action carried `vendor_id` (assert via a monkeypatched `evaluate` capturing the Action) and an `invoices:<known case>` subject gets `transaction_node`; guard invoice approve passes `vendor_id` of the stored vendor.
- [ ] Implement; run the four test files; commit.

### Task 2: One-session `record()`, plain lock

**Files:** `trust/findings/store.py` (`_upsert`), `trust/agents/record.py`, `trust/agents/guard.py` (`_one` passes `s`), `trust/agents/store.py` (`_lock_query`); tests `tests/test_trust_agents_policy.py`, `tests/test_trust_agents_guard.py`, `tests/test_trust_findings*.py` unchanged.

- [ ] Failing tests: `test_a_failing_finding_write_leaves_no_ledger_row` (patch `FindingStore._upsert` to raise → `record` raises; no `agent_actions`, no outbox rows); `test_record_refuses_two_databases` (`findings.url != store.url` → `ValueError`); guard: `test_a_record_failure_in_enforcing_is_503_and_writes_nothing`.
- [ ] Implement; existing guard concurrency test and the whole agent suite pass on SQLite; commit.

### Task 3: List refusal stamps earlier rows; velocity counts requests

**Files:** `trust/agents/guard.py` (`_check`), `trust/agents/store.py` (`count_since`); tests `tests/test_trust_agents_guard.py`, `tests/test_trust_agents_store.py`.

- [ ] Failing tests: vendor load of three new vendors where the mandate's counterparties allow only the first two (enforcing) → 403, rows 1–2 `outcome == "failed"`; `count_since` over five rows sharing one `request_id` plus two without → 3.
- [ ] Implement; run; commit.

### Task 4: `count_blocked`, `purge_before`

**Files:** `trust/agents/store.py`; test `tests/test_trust_agents_store.py`.

- [ ] Failing tests per the spec's Tests bullet (blocked vs reviews classes, window, dryrun excluded, empty tenant zeros; purge keeps `vendor.create`, returns count, per tenant).
- [ ] Implement; run; commit.

### Task 5: Routes, list mandate, intake stub, isolation, doc, e2e key

**Files:** `api/trust_agent_routes.py` (`blocks_count`, `list_agents` rows + `mandate`), `api/v2_routes.py` (`intake_actions` stub), `tests/test_tenant_isolation_api.py` (`TestWave5StubsIsolation`), `docs/API_V2.md`, `scripts/e2e_api.py`; tests `tests/test_trust_agent_routes.py` (PROBED += `/api/trust/agents/blocks/count`), `tests/test_trust_api_routes.py`.

- [ ] Failing tests: count route two tenants, approver-only, window from threshold; list rows carry `mandate` (null without one, `verified` with one); stub 503 both tenants; doc test green.
- [ ] Implement; run route + isolation + api-route suites; commit.

### Task 6: Frontend shell, PENDING

**Files:** `frontend/src/lib/api.ts`, `frontend/src/components/AppShell.tsx`, `frontend/src/app/agents/page.tsx`, `frontend/e2e/golden-path.spec.ts`, `tests/test_frontend_reaches_the_flow.py`, `docs/PENDING.md`.

- [ ] `tests/test_frontend_reaches_the_flow.py` fails first with `agents` in VIEWS; add page + NAV; lint/tsc/build; e2e 47; commit.
- [ ] PENDING Wave 5 block; commit.

### Task 7: Close-out

- [ ] Full SQLite suite; Postgres `tests/test_trust_*.py tests/test_tenant_isolation_api.py`; eval `--seed 7` 56/56; ruff; frontend lint/tsc/build/e2e.
- [ ] Fable review; one fix pass; PR; merge on the user's word.
