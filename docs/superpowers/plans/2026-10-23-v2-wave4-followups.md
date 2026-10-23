# Wave 4 follow-ups (PENDING items 2–6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close five Wave 4 deferrals listed in `docs/PENDING.md`: revocation judged at the action's time, no agent judgements while the engine is off, a person's U9 refusal kept off the agent's ledger, rule crashes failing like infrastructure, and one query for the vendor-load create check.

**Architecture:** Small, independent fixes inside `invoice_processing/trust/agents/` and two routes. No migration, no new table, no new dependency.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2, pytest (SQLite; Postgres container for the agent suites).

**Spec:** the design agreed in chat on 2026-10-23 (summarised under each task); program context `docs/superpowers/plans/2026-10-22-v2-wave4-program.md` (B8 failure rule, B15 log judgement, U9 owner rule).

## Global Constraints

- Spec §3.1: fail closed for controls, open for insights.
- Engine mode `off` means the engine does nothing: no ledger rows, no findings.
- A refused BLOCK in enforcing stays a 403 whose body explains it; a guard failure in enforcing stays a 503 `guard.UNAVAILABLE`.
- No ids/emails/secrets in log `extra`.
- Run ruff from the repo root exactly as CI: `uv run ruff check . && uv run ruff format --check .`

## Review Focus

- A log row timed exactly at the agent's `revoked_at` → revoked (the boundary is `revoked_at <= at`). Pinned in Task 1.
- An agent revoked with a NULL `revoked_at` (legacy/manual row) → still revoked. Pinned in Task 1.
- `/v2/agents/{id}/actions/evaluate` while off → the same 409 (it delegates). Pinned in Task 2.
- A person refused twice on the same invoice → one finding, not two (dedupe). Pinned in Task 3.
- A behavioural detector that raises inside the guard in enforcing → the write goes through (no 503). Pinned in Task 4.

---

### Task 1: Revocation is judged at the action's time (item 2)

**Files:**
- Modify: `invoice_processing/trust/agents/rules.py` (`mandate_active`)
- Test: `tests/test_trust_agents_rules.py`, `tests/test_trust_agent_routes.py`

`mandate_active` blocks as revoked only when `agent["status"] == "revoked"` and (`agent["revoked_at"]` is None or `revoked_at <= (action.occurred_at or now)`), comparing in UTC (naive = UTC). Otherwise it falls through to the mandate checks, where `mandates.in_force` already ends mandates at their `revoked_at`.

- [ ] **Step 1: Failing tests** — in `test_trust_agents_rules.py`: `test_an_action_before_the_revocation_is_not_revoked` (agent revoked at T, action `occurred_at` T−1h with a verified mandate in force → `mandate_active` returns None); `test_an_action_at_or_after_the_revocation_is_revoked` (at T and T+1h → reason message endswith "is revoked."); `test_a_revoked_agent_without_a_time_is_revoked` (revoked_at None → revoked). In `test_trust_agent_routes.py`: `test_an_import_before_the_revocation_is_judged_then` (mandated_earlier, revoke via route at NOW, log row at NOW−2d → decision not BLOCK; row at NOW+1m… use occurred_at after revoke → BLOCK "is revoked").
- [ ] **Step 2:** Run them; expect the before-revocation cases to FAIL ("is revoked").
- [ ] **Step 3:** Implement as above.
- [ ] **Step 4:** Run `tests/test_trust_agents_rules.py tests/test_trust_agent_routes.py`; PASS.
- [ ] **Step 5:** Commit.

### Task 2: Evaluate and import refuse while the engine is off (item 3)

**Files:**
- Modify: `invoice_processing/api/trust_agent_routes.py` (`import_actions`, `evaluate_action`, `_judge`)
- Test: `tests/test_trust_agent_routes.py` (fixture `env` sets `agents` to `shadow` for both tenants via `modes.set_engine_mode(ctx, "agents", "shadow", store=<the env's JsonTenantSettings>)`; `test_a_proposed_mandate_evaluates_to_block` now expects `mode == "shadow"`)

Add `_mode(tenant_id) -> str`: reads `engine_mode(tenant_id, "agents")`; `"off"` → `HTTPException(409, OFF)` with `OFF = "Agent control is off for this tenant; turn it on (shadow or higher) first."`. Both routes call it before reading the body/file further; `_judge` takes `mode` as a parameter instead of reading it per row.

- [ ] **Step 1: Failing tests** — `test_evaluate_and_import_are_409_while_off` (set mode off; evaluate (log and dryrun), `/v2/agents/{id}/actions/evaluate`, and import each → 409 with `OFF`; `store.actions(...) == []`; no findings).
- [ ] **Step 2:** Run; FAIL (200).
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `tests/test_trust_agent_routes.py tests/test_v2_routes.py tests/test_tenant_isolation_api.py`; PASS.
- [ ] **Step 5:** Commit; update `docs/API_V2.md` Agents section with the 409.

### Task 3: A person refused under U9 is not booked on the agent (item 4)

**Files:**
- Modify: `invoice_processing/trust/agents/record.py` (add `person`), `invoice_processing/trust/agents/guard.py` (`_owner_check`)
- Test: `tests/test_trust_agents_guard.py`

**Interfaces:**
- Produces: `record.person(ctx, decision, action, *, agent: dict, principal, findings, mode: str) -> str` — upserts one finding and returns its id. No `agent_actions` row, no webhook, no graph edge. FindingSpec: type `AGENT-SAME-OWNER-CHAIN`, `transaction_node = action.transaction_node or agent["graph_node"]`, citations as in `record` (agent node, plus the invoice node when known), `dedupe_key = f"owner_chain:{action.subject_ref}:{principal.user_id}"`, summary `decision.explanation[:500]`, details `{engine, mode, source: "inline", hold: False, action, agent_id, decision, reasons, subject_ref, person: principal.email}`.

`_owner_check` calls `person(...)` instead of `_record(...)`; the returned Decision has `action_id=None` (so `settling` stamps nothing); enforcing still answers 403 via `_answer`.

- [ ] **Step 1: Update/failing tests** — change `test_agent_then_its_owner_is_refused_for_the_owner`: `ledger(env)` has no BLOCK row; one open `AGENT-SAME-OWNER-CHAIN` finding whose details `person` is the owner's email and `agent_id` the bot's; no `agent.action.blocked` outbox row. Change `test_the_owner_check_only_records_below_enforcing`: no BLOCK ledger row, one finding. Add `test_a_person_refused_twice_raises_one_finding`.
- [ ] **Step 2:** Run; FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `tests/test_trust_agents_guard.py tests/test_trust_agents_policy.py tests/test_trust_explain*.py`; PASS.
- [ ] **Step 5:** Commit.

### Task 4: A crashing rule fails like infrastructure (item 5)

**Files:**
- Modify: `invoice_processing/trust/agents/policy.py` (`evaluate_action`: drop the `mode` parameter and the `rule-failed` reason), `guard.py`, `trust_agent_routes.py`, `benchmark/history/agents.py` (callers stop passing `mode`)
- Test: `tests/test_trust_agents_policy.py`, `tests/test_trust_agents_guard.py`, `tests/test_trust_agent_routes.py`

A structural rule (`rules.RULES`) that raises propagates out of `evaluate_action`; the guard's existing handler makes it 503 in enforcing, a logged no-op below. A behavioural detector (`behaviour.RULES`) that raises is logged (`agent_rule_failed`, error type only) and skipped. In `_judge`, an exception from `evaluate` becomes `CheckFailed(RuntimeError)`; `evaluate_action` route → 503 `"A check could not run; try again."`, import → that row's error with the same message, other rows continue.

- [ ] **Step 1: Failing tests** — policy: replace `test_a_rule_that_raises_blocks_only_in_enforcing` with `test_a_structural_rule_that_raises_propagates` (`pytest.raises(RuntimeError)`) and `test_a_detector_that_raises_is_skipped` (behaviour rule raises, another returns REVIEW → `ALLOW-WITH-REVIEW` with one reason). Guard: `test_a_crashing_rule_is_503_in_enforcing_and_writes_nothing` (no ledger row, no finding, no outbox row, invoice undecided); `test_a_crashing_detector_does_not_stop_the_write_in_enforcing`. Routes: `test_a_crashing_rule_is_503_on_evaluate_and_a_row_error_on_import`.
- [ ] **Step 2:** Run; FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the agent suites and `./.venv/bin/python -m benchmark.history.eval --seed 7` (56/56).
- [ ] **Step 5:** Commit.

### Task 5: One query for the vendor-load create check (item 6)

**Files:**
- Modify: `invoice_processing/db/repositories.py` (`PgVendorRepository.normalized_names(tenant_id) -> set[str]`: one `select(Vendor.normalized_name)` for the tenant), `invoice_processing/api/vendor_routes.py` (`replace_vendors.creates` uses it; drop the `ponytail:` note)
- Test: `tests/test_trust_agents_guard.py`

- [ ] **Step 1: Failing test** — `test_a_vendor_load_reads_the_vendor_names_once` (advisory, agent with `vendor.create`; patch `PgVendorRepository.list` to raise; load 3 vendors → 200, one ledger row per new vendor). Existing `test_only_new_vendors_are_creates` still passes.
- [ ] **Step 2:** Run; FAIL (list called).
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `tests/test_trust_agents_guard.py tests/test_vendor_routes.py`; PASS.
- [ ] **Step 5:** Commit.

### Task 6: Close-out

- [ ] Full suite, ruff (CI form), Postgres agent/trust/isolation suites on a fresh `pg-w1` DB, benchmark `--seed 7`.
- [ ] Tick items 2–6 in `docs/PENDING.md` with one line each; keep item 1 open.
- [ ] Fable review of the branch diff; one fix pass (each fix test-first); PR.
