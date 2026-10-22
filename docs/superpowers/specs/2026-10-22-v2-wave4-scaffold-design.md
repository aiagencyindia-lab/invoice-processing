# Wave 4 — Scaffold design (`v2/w4-scaffold`)

**Program plan:** `docs/superpowers/plans/2026-10-22-v2-wave4-program.md` (decisions U1–U8, builder's calls B1–B12). PRD FR-28, AC-28, §9, §10; spec §3.1; ADR-16.

**Owns every shared file** (program plan, File ownership, scaffold row). Merged before any branch starts. Behaviour change with the `agents` engine `off`: none.

## S1 Schema

Migration `b2c3d4e5f6a7_trust_wave4.py` (revises `a1b2c3d4e5f6`; own `RLS_TABLES = ("agents", "agent_mandates", "agent_actions")`; helpers copied from the Wave 3 file). Models `Agent`, `AgentMandate`, `AgentAction` in `db/models.py`, all three in `TENANT_TABLES`. Columns exactly as the program plan's S1 table. Constraints: unique `(tenant_id, principal_id)` on `agents` (NULLs allowed — `kind=external`), unique `(agent_id, version)` on `agent_mandates`, index `(tenant_id, agent_id, occurred_at)` and `(tenant_id, vendor_key)` on `agent_actions`. Money `Numeric(18,4)`; mandate cap amounts are strings in `scope` JSON, parsed to `Decimal`.

## S2 Registries

- `trust/types.py`: the nine `AGENT-*` codes (engine `agents`, `hold=False`, dimension `authorization`), descriptions as in the program plan.
- `trust/modes.py`: `ENGINES += ("agents",)`; `BLOCKING_ENGINES = frozenset({"agents"})`; `HOLD_ENGINES` unchanged. `set_engine_mode` works unchanged (it reconciles holds only for hold engines; `agents` has none).
- `trust/score/readiness.py`: an engine in `BLOCKING_ENGINES` is not insight-only; its advisory→enforcing recommendation reason reads "Enforcing refuses blocked agent actions."
- `trust/audit/report.py PREVENTION`: no agents row — Trust Audit reports are customer-facing and the engine is dark (changed while building).
- `trust/score/dimensions.py FEEDS` leaves out `BLOCKING_ENGINES`: authorization is measured from approvals whether or not agent control runs (changed while building; without it authorization read as unmeasured while the engine is off).
- `trust/webhooks/__init__.py EVENTS += ("agent.action.blocked",)`; payload `{agent_id, agent_name, owner, action, subject_ref, decision, reasons, explanation, action_id, finding_id}` (no amounts beyond the action's own, no bank numbers).
- `trust/thresholds.py DEFAULTS`: `agents.velocity_window_minutes 10`, `agents.velocity_max 20`, `agents.split_window_days 7`, `agents.split_min_count 2`, `agents.injection_min_hits 1`.
- Frontend: `ENGINE_LABELS.agents = "Agent control"`; `/trust-engines` lists an engine only if its mode is not `off` or its readiness row has any findings (U6). No other UI.

## S3 Package and store (`trust/agents/`)

- `__init__.py`: `ENGINE_VERSION = "agents-1"`, `ACTIONS` (seven, program plan S3), `DECISIONS = ("ALLOW", "ALLOW-WITH-REVIEW", "BLOCK")`, `run(ectx) -> []`, `register_type` for the nine codes from `types.TABLE`.
- `store.py` `AgentStore(url=None)`: every method takes `tenant_id` (or `ctx`) and uses `tenant_session`. Signatures in the program plan S3. `approve_mandate` raises `SelfApproval` (reuse the class from `db/repositories.py`) when `approved_by.casefold() == proposed_by.casefold()`; on success the previous active version of that agent becomes `revoked` with reason `superseded`. `current_mandate` = the `active` row with `valid_from ≤ now < valid_to`, else `None`. `spent(tenant, agent_id, *, since, vendor_key=None, category=None)` sums `amount` of `invoice.approve` rows whose decision is not `BLOCK` (a blocked approval spent nothing). `creators_of(tenant, vendor_key)` = agents with a non-BLOCK `vendor.create` row for that key. Every count/sum excludes `source='dryrun'`; windows take an explicit `at` (the action's `occurred_at`). `spent` filters by the mandate's `currency` (U10). `propose_mandate` refuses (409) while a version is `proposed`; `approve_mandate` refuses a version that is not the latest (B16). `canonical(row)`/`digest(row)` live here. `lock_agent(s, agent_id)` = `SELECT … FOR UPDATE` on the agent row (B17).
- Graph: `register` upserts node `kind="agent"`, `natural_key=f"agents:{id}"`, `ref_table="agents"`, attrs `{name, owner, kind, status}` and stores its id in `agents.graph_node`; `revoke` re-versions it with `status=revoked`.

## S4 Policy seam

`policy.py` dataclasses and `evaluate_action` as in the program plan S4. Composition: `RULES = rules.RULES + behaviour.RULES`, evaluated in order, every rule run (reasons are complete), decision by severity. A rule that raises is a `BLOCK` reason `rule-failed` in enforcing and a `REVIEW` reason otherwise (fail closed only where it bites, B8). Explanation: one sentence per reason, joined, escaped through `trust/html.py`.

`record.py::record(...)` (ordered, not atomic — program plan B13): writes the `agent_actions` row; for a non-ALLOW decision from a non-`dryrun` source, upserts the finding (`FindingStore.upsert`; dedupe `agent_action:{action_id}`; `transaction_node` = the invoice node when known, else the agent node (B14); citations: agent node, plus subject node when known; `details = {engine: "agents", mode, reasons, action, agent_id, decision}`), then in one session stamps `finding_id` on the row and emits `agent.action.blocked` only when the action was refused (enforcing, inline; B15). An ALLOW/REVIEW inline action with a known subject node gets an `acted_on` edge (agent node → subject node). Returns the action id.

`guard.py::check(principal, action, *, settings_store=None, now=None) -> Decision | None`: stub returning `None` (branch B).

## S5 Routes, benchmark, isolation

- `api/trust_agent_routes.py`: twelve handlers (names and paths as the program plan's branch A table) each raising `HTTPException(503, "Agent control is not built yet.")`; mounted in `service.py`; `service.get_agent_store()` lazy accessor beside `get_hold_store`.
- `benchmark/history/eval.py`: `_measure_modules()` also looks for `benchmark/history/agents.py` (the Wave 3 `measure` plug-in). Absent → nothing changes.
- `tests/test_tenant_isolation_api.py`: the twelve paths join a stub-probe set asserting 503 for both tenants (the Wave 3 hand-over mechanism, deleted at Wave 3 close in 5da399c — restore it from 4f836b3: a path leaves the stub set once `tests.test_trust_agent_routes.PROBED` lists it); `EXTRA_PROBE_MODULES += ("tests.test_trust_agent_routes",)`.
- `explain/templates/agents.py` empty module, imported from `templates/__init__.py`; the explain coverage test skips `AGENT-*` until branch C fills them (marked, removed at wave close).

## Tests

`test_trust_schema` (three tables, constraints, RLS list); migration param `("b2c3d4e5f6a7", 3)`; restore pin; settings snapshot; findings/types agreement; readiness for a blocking engine; `test_trust_agents_store.py` (register/lookup, mandate versions + self-approval + supersede, current_mandate window, spent excludes BLOCK and non-approve rows, creators_of, record writes row, finding and outbox in order; dryrun writes no finding; blocked webhook only for a refused action, ALLOW writes no finding); eval hook with a fake family; frontend lint/tsc/build.

**Done:** ruff; SQLite suite; Postgres trust glob; alembic up/down; eval 52/52 unchanged; frontend checks; e2e 47.
