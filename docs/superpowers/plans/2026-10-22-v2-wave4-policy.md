# Wave 4 branch B — Structural rules and the guard: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (native, chosen by the program plan's process: implement, then one fable review, one fix pass). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Fill `rules.RULES` with the six structural controls and `guard.check` with the inline interceptor, and call it from the six governed write paths (FR-28.2, FR-28.3).

**Architecture:** Each rule is a pure function over the action, the agent, its mandate and the ledger (`AgentStore`), returning `Reason | None`. The guard reads the `agents` mode first (off → returns with no agent query), finds the agent behind the principal, takes the agent lock, evaluates, records, and refuses a BLOCK with a 403 in enforcing. Each call site passes the guard a zero-argument builder of its `Action`, so it builds nothing (and reads nothing extra) while the engine is off.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2, pytest; SQLite and Postgres (`pg-w1`, port 55433).

**Spec:** `docs/superpowers/specs/2026-10-22-v2-wave4-policy-design.md`; program plan `docs/superpowers/plans/2026-10-22-v2-wave4-program.md` (branch B, U9, U10, B13–B19).

## Global Constraints

- Own only: `trust/agents/rules.py`, `trust/agents/guard.py`, the call-site edits in `api/vendor_routes.py` (`replace_vendors`, `patch_vendor`, `propose_change`, `decide_bank_change`) and `api/service.py` (`act`, `export_invoice`), `tests/test_trust_agents_rules.py`, `tests/test_trust_agents_guard.py`, this plan.
- Money is `Decimal`; mandate cap amounts are strings in `scope` JSON.
- No ids, emails or secrets in log `extra`.
- `lock_agent` stays `FOR NO KEY UPDATE`.
- The `text` loader is `None` for now: `# TODO(w4-behaviour): text = inputs.text_of(...)`.
- The governed routes' existing test modules stay green unchanged.

## Rulings (this branch's; ledgered in the main checkout)

- R1 `check(principal, action, ...)` takes an `Action`, or a zero-arg callable returning an `Action` or a list of them (the `replace_vendors` case). A builder runs only when the mode is not `off`, so engine-off call sites read nothing extra. A list returns a list of decisions.
- R2 A failure reading the mode is logged and the guard returns None: the settings and the governed write share one database, so the route meets that failure as it always did (an earlier 503 here changed 29 existing tests' answers).
- R3 Spend windows run from the period start to the period end (not to `now`): a concurrent approval recorded a moment "after" this guard's `now` still counts. With no `per_period`, `per_vendor`/`per_category` use the calendar month.
- R4 An approve with caps set and no readable amount is BLOCK `AGENT-OVER-LIMIT` ("amount unknown").
- R5 `initiated_by` for `act`/`export` = the uploader (`approval_flow.uploader_of`) only. An email-sourced document stores the `.eml` key, not a sender, and the inbound-email actor is never a registered agent.
- R6 `prior_deciders` = `decided_by` of every non-pending step of the chain.
- R7 The invoice action builder lives in `guard.py` (`invoice_action`), so the `service.py` edits stay inside `act` and `export_invoice`.
- R8 `decider=None`: `service.py` has no decider (told the parent).
- R9 `replace_vendors`: a record whose normalised name is already a vendor is not a create. Earlier ALLOW rows from a replace that a later record refuses stay in the ledger (decision ledger, B3).

## Review Focus

1. A human approver with no agents while the engine is enforcing: untouched (guard test `test_a_person_with_no_agents_is_untouched`).
2. Revoked agent: found, and blocked by `mandate_active` (rules test).
3. An invoice with no total or a non-numeric total under caps: BLOCK, never a crash (R4; rules test).
4. Currency case (`inr` vs `INR`): compare upper-cased (rules test).
5. Engine off with the agent store missing (no DB, file vendors): no error (the existing vendor file tests).

---

### Task 1: `rules.RULES`

**Files:** Modify `invoice_processing/trust/agents/rules.py`; Test `tests/test_trust_agents_rules.py`.

**Interfaces:** Produces `RULES = (currency, mandate_active, in_scope, self_approval, same_owner_chain, over_limit, unverified_vendor)`, each `fn(action, agent, mandate, store, settings, now, decider) -> Reason | None`; tenant from `agent["tenant_id"]`. Consumes `AgentStore.spent/by_emails/creators_of`.

- [ ] Write the tests: one positive and one negative per rule against a real SQLite `AgentStore` ledger (rows written with `store.record_action`); caps at the boundary (equal passes, `0.0001` over blocks); a BLOCK row does not count toward spend; a revoked agent; per-vendor and per-category caps; currency mismatch and case; counterparty skip when `vendor_key` is None; B11.
- [ ] Run, see them fail; implement; run, see them pass; commit.

### Task 2: `guard.check` and `guard.invoice_action`

**Files:** Modify `invoice_processing/trust/agents/guard.py`; Test `tests/test_trust_agents_guard.py`.

**Interfaces:** `check(principal, action, *, settings_store=None, now=None, store=None) -> Decision | list[Decision] | None`; `invoice_action(ctx, artifacts, kind) -> Action`. 403 detail = `{"detail": explanation, "action_id", "reasons": [{type, message}]}`; 503 detail `"Agent control is unavailable; the action was not taken."`.

- [ ] Write the tests: off → `AgentStore.by_principal` monkeypatched to raise is never called; unregistered principal → None in enforcing; owner check (U9) both ways; enforcing BLOCK → 403 with the body; advisory/shadow → decision returned, ledger row + finding; failure → 503 in enforcing, None in advisory with no ids in the log.
- [ ] Fail, implement, pass, commit.

### Task 3: Call sites

**Files:** Modify `api/vendor_routes.py`, `api/service.py`; Test `tests/test_trust_agents_guard.py` (route section).

- [ ] Write the route tests: per call site, enforcing BLOCK → 403 and the governed table is unchanged; advisory → write + ledger row + finding; two agents of one owner on one chain → 403 on the second; owner then agent and agent then owner → 403; two concurrent approvals that each fit the cap but not together → exactly one passes (Postgres only).
- [ ] Fail, implement, pass; existing governed-route modules unchanged and green; commit.

### Task 4: Verify, review, PR

- [ ] `uv run ruff check .`, `uv run ruff format --check .`, the full SQLite suite, Postgres `tests/test_trust_*.py tests/test_tenant_isolation_api.py` plus the governed modules on `trust_w4_policy`.
- [ ] Fable review, one fix pass (each fix test-first), push, PR.
