# Wave 4 branch A — Registry, mandates, ledger, routes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the agent registry, two-person signed mandates, the spend ledger and the twelve `/api/trust/agents*` routes (FR-28.1, FR-28.5, FR-28.6), replacing the scaffold's 503 stubs.

**Architecture:** Three small modules over the scaffold's `AgentStore`: `registry.py` (bind a principal, audit), `mandates.py` (validate, sign, verify, approve), `ledger.py` (sums by currency/vendor/category/period). `api/trust_agent_routes.py` is the HTTP layer; `actions/evaluate` and `actions/import` share one helper that builds a `policy.Action`, calls `policy.evaluate_action` and `record.record`.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2, stdlib `hmac`/`hashlib`, pytest on SQLite and Postgres.

**Spec:** `docs/superpowers/specs/2026-10-22-v2-wave4-registry-design.md`; program plan `docs/superpowers/plans/2026-10-22-v2-wave4-program.md` branch A, U2/U4/U8/U10, B1/B3/B4/B9/B13–B19.

## Global Constraints

- Own only: `invoice_processing/trust/agents/{registry,mandates,ledger}.py`, `invoice_processing/api/trust_agent_routes.py`, `tests/test_trust_agent_routes.py`, `tests/test_trust_agents_mandates.py`, this plan. Handler names and paths stay as the stubs (D imports them).
- Money is `Decimal`; JSON carries amounts as strings. No ids/emails/secrets in log `extra`.
- `Admin` = `integration_routes.Admin` (API-key principals 403; 503 without a DB).
- Signature key: `hmac_sha256(TRUST_MANDATE_KEY, tenant_id)`; signature = `hmac_sha256(key, digest)` hex. Key unset → approve 503 `"Mandate signing is not configured."`, nothing changes.
- `dryrun` never refuses/counts/raises a finding/fires a webhook; `log` rows count and raise findings at their `occurred_at` and never fire `agent.action.blocked` (`refused=False` always on these routes).
- Import: CSV/XLSX via `history/readers.read_table`, ≤ 5 000 rows, one bad row never stops the rest.
- Every route in `PROBED`, probed with two tenants: bearer and the other tenant's API key.

## Review Focus

1. **Cross-tenant ids in paths** (acme's agent id under a globex principal, a mandate id under a different agent id) → 404, never data. Probe tests + `test_a_mandate_under_another_agent_is_404`.
2. **Naive / non-UTC datetimes** in `valid_from`, `occurred_at`, `since`/`until` → treated as UTC, never a 500. `test_naive_times_are_utc`.
3. **Bad money** (`"1,000"`, `"-5"`, `"abc"`, `"NaN"`) in evaluate/import/caps → 400 or a row error, never a 500 or a float. Mandate validation table + import bad rows.
4. **Approve crash gap**: approval commits before the signature is written (store sets `approved_at` itself) → the row reads `verified: false`, visible. `test_an_unsigned_active_mandate_does_not_verify`.
5. **An agent's own principal** evaluating another agent's id → 403; its own id → 200. `test_an_agent_key_may_evaluate_only_itself`.

---

### Task 1: Mandates — validate, sign, verify, approve

**Files:** Create `invoice_processing/trust/agents/mandates.py`; Test `tests/test_trust_agents_mandates.py`.

**Interfaces — Produces:**
- `class MandateKeyMissing(Exception)`
- `validate(body: dict, *, now) -> tuple[dict, datetime, datetime]` — body `{scope:{actions, counterparties?, caps?, currency}, valid_from, valid_to}`; returns normalised scope (counterparties via `po_matching.normalize_vendor`, deduped; caps amounts canonical `str(Decimal)`), UTC datetimes; `ValueError(first problem)`. Rules: actions non-empty ⊆ `ACTIONS`; caps keys ⊆ `{per_action, per_period, per_vendor, per_category}`; `per_period = {period: day|month, amount}`; `per_category = {kind: amount}`; amounts finite, ≥ 0; currency 3-letter alpha upper-cased; `valid_from < valid_to`, span ≤ 366 days.
- `sign(tenant_id: str, digest: str) -> str` (raises `MandateKeyMissing`).
- `verify(row: dict, prev: dict | None) -> bool` — prev_digest chain; `store.digest(row) == row["digest"]`; approved rows also need a signature equal under `hmac.compare_digest`; key unset → approved rows `False`.
- `annotate(rows: list[dict], now) -> list[dict]` — adds `verified`, `expired` (active and `valid_to ≤ now`).
- `approve(ctx, store, mandate_id: str) -> dict` — `MandateKeyMissing` before any write; `store.approve_mandate(..., signature=None)`, then stamp `signature = sign(tenant, digest)` in a second session (ruling: store sets `approved_at`, so the digest is only known after; the gap leaves `verified: false`).

- [ ] Tests: validation table (each rule's 400 message); `sign` deterministic per tenant, differs across tenants; propose → approve → `verify` true; editing `scope` or `valid_to` in the DB → false; wrong `prev_digest` → false; proposed row verifies on digest alone; key unset → `approve` raises and the row stays `proposed`; unsigned active row → false.
- [ ] Run, see fail; implement; run, pass; commit.

### Task 2: Registry

**Files:** Create `invoice_processing/trust/agents/registry.py`; tests in `tests/test_trust_agent_routes.py` (through the routes).

**Interfaces — Produces:**
- `class AgentConflict(Exception)`
- `register(ctx, body: dict, *, store, audit, url=None) -> dict` — body `{name, owner, description?, principal?}`; `{api_key_id}` → tenant's live key else `LookupError`, `principal_id=f"api-key:{id}"`, email `f"{name}@api-key.{tenant}"`, another live key with that name (casefold) → `AgentConflict`; `{user_id, email}` → `kind="user"`; none → `external`. Principal id or principal email already on an agent → `AgentConflict`; unique-constraint race → `AgentConflict`. Owner/email must match `^[^@\s]+@[^@\s]+\.[^@\s]+$` else `ValueError`. Audit `agent_registered {name, kind, owner}`.
- `revoke(ctx, agent_id, reason, *, store, audit) -> dict` — `LookupError` when unknown; already revoked → returned unchanged, no audit; else audit `agent_revoked {name, reason}`.

### Task 3: Ledger

**Files:** Create `invoice_processing/trust/agents/ledger.py`.

**Interfaces — Produces:**
- `window(since, until, now) -> (since, until)` — defaults start of the UTC month / now; naive → UTC; `since ≥ until` → `ValueError`.
- `agent_ledger(store, tenant_id, agent: dict, *, since, until, now) -> dict` → `{agent, mandate, spend: {CUR: {approved, exported, by_vendor, by_category}}, period: {period, currency, cap, used, remaining, since, until} | None, actions: [≤200 newest, any source], exceptions: [non-ALLOW, inline/log, ≤200]}`. Sums: one `GROUP BY action, vendor_key, category, currency` over non-BLOCK non-dryrun `invoice.approve`/`invoice.export` rows. Spend keyed by currency (ruling: ledgers can hold log rows in several currencies; the mandate's cap is in its one currency, U10). Period uses `store.spent` over the current day/month containing `now`.
- `tenant_ledger(store, tenant_id, *, since, until) -> {since, until, owners: [{owner, agents, approved:{CUR}, exported:{CUR}, blocked, reviewed}]}`.
- Amounts as strings.

### Task 4: Routes (all twelve) + probes

**Files:** Modify `invoice_processing/api/trust_agent_routes.py`; Test `tests/test_trust_agent_routes.py` (exports `PROBED` = the twelve templates).

Errors: `ValueError` 400, `LookupError` 404, `AgentConflict`/`MandateConflict` 409, `SelfApproval` 403, `MandateKeyMissing` 503. Approver routes 503 when `service.get_agent_store()` is `None`. `evaluate`: any authenticated principal; allowed when `principal.can("approver")` or `principal.user_id == agent.principal_id`, else 403. Shared helper `_judge(ctx, agent, row: dict, source, *, now) -> dict` builds the `Action` (subject_table from the action prefix: vendors / vendor_bank_changes / invoices; vendor → `normalize_vendor`; amount `Decimal`), mandate = `store.current_mandate(tenant, id, occurred_at or now)`, settings = `thresholds.merged(effective(...)["trust_thresholds"])`, mode = `engine_mode`, then `record(..., refused=False)`; returns `{action_id, decision, reasons, explanation, mode, source}`. `log` needs `occurred_at`.

- [ ] Tests (spec list): probes (12 paths × bearer of the other tenant × other tenant's API key); key principal 403 on every Admin route; registry 404/409 (foreign key id, revoked key, shared key name, principal reused); self-approval 403; key unset 503; proposed mandate → evaluate BLOCK `AGENT-OUT-OF-MANDATE` (rules are B's: the test installs a stand-in `mandate_active` rule on `rules.RULES`); expiry and supersede via `GET …/mandates`; ledger sums by vendor/category/period; import happy path + mixed bad rows; dryrun never 403s and writes no finding; log writes a finding and no outbox row; Review Focus tests.
- [ ] Run, fail; implement; pass; commit.

### Task 5: Verify, review, PR

- [ ] `uv run ruff check . && uv run ruff format --check invoice_processing tests benchmark scripts`
- [ ] `./.venv/bin/python -m pytest -q -p no:cacheprovider` → all pass (stub probes for the twelve paths skip).
- [ ] Postgres `trust_w4_registry` on `pg-w1` for `tests/test_trust_*.py tests/test_tenant_isolation_api.py`.
- [ ] Fable review → one fix pass (each fix test-first) → push, PR.
