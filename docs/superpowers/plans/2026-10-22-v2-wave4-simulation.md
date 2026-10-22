# v2 Wave 4 — Simulation harness, `/v2` agents, docs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A simulated agent fleet that gates AC-28 in `python -m benchmark.history.eval`, four `/v2/agents*` routes over the registry's handlers, and the "Agents" section of `docs/API_V2.md`.

**Architecture:** `benchmark/history/agents.py` is a `measure` plug-in (`eval._measure_modules()`): it registers two admins' fleet on the bench tenant, builds a pure, timestamped script of labelled `Action`s, drives each through `policy.evaluate_action` + `record.record(source="inline", mode="advisory")` on a simulated clock, and tallies decisions against labels. `/v2` routes are import-call-return wrappers over `trust_agent_routes`.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2, pytest (SQLite + Postgres).

**Spec:** `docs/superpowers/specs/2026-10-22-v2-wave4-simulation-design.md`; program plan `docs/superpowers/plans/2026-10-22-v2-wave4-program.md` branch D, B10, B15, B17, B18.

## Global Constraints

- Owns only: `benchmark/history/agents.py`, `invoice_processing/api/v2_routes.py`, `docs/API_V2.md`, `tests/test_trust_agents_sim.py`, `tests/test_trust_api_routes.py`, this plan.
- Money is `Decimal`; no ids/emails/secrets in log `extra`.
- Gates: `agents_structural_blocked == 1.0`, `agents_behavioural_flagged >= 0.9`, `agents_false_blocks == 0`, `agents_latency_60s` (`elapsed_s <= 60`). `false_reviews` reported, not gated.
- Merged last: the gates pass only on a `main` with B (`rules.RULES`) and C (`behaviour.RULES`); the façade needs A's handlers.

## Rulings (this branch)

- **D1 Simulated clock.** Every scripted action has an `occurred_at` on a fixed clock (`T0 = 2026-09-01T09:00Z`, one action every 15 min across the fleet; the velocity burst is 25 actions 2 s apart). `evaluate_action(now=occurred_at)` and `record(now=occurred_at)`. Deterministic, and the 15-min spacing keeps every agent below the velocity window (10 min) except the burst.
- **D2 "Caught" = right decision for the right reason.** A structural action counts as blocked only when the decision is `BLOCK` *and* its expected `AGENT-*` type is among the reasons; behavioural likewise with non-ALLOW. A self-approval blocked only by some other rule would hide a dead rule. Gate ratios use this count.
- **D3 Advisory, inline, never refused.** The fleet records `mode="advisory"`, `refused=False`: AC-28 measures the decision (B15: no `agent.action.blocked` from the bench).
- **D4 Vendors and invoices from the bench graph**, padded with synthetic `Fleet Vendor NN` names if the graph has fewer than needed; invoice nodes cycled for `transaction_node`/`subject_ref` (none → `fleet-{i}` refs, no node).

## Fleet (agents emails `{name}@agents.bench`, `kind="user"`, `principal_id="agent:{name}"`; people `admin-a@bench.test` proposes every mandate, `admin-b@bench.test` approves; uploads by `clerk@bench.test`)

| Agent | Owner | Mandate | Script (label: kind × n) |
|---|---|---|---|
| `ap-bot-1..3` | `ops1..3@bench.test` | active; approve/export/bank_change.propose; caps 100 000 / month 10 000 000 / vendor 2 000 000; INR | compliant: 10 approvals (distinct vendors, ≤ 50 000), 5 exports, 2 bank-change proposals each (51) |
| `rogue-unapproved` | `rogue1@` | left `proposed` | structural `unapproved_mandate` × 5 |
| `rogue-expired` | `rogue2@` | active, valid 2026-07-01 → 08-15 | structural `expired_mandate` × 5 |
| `rogue-approver` | `rogue3@` | approve; counterparties = 5 pool vendors + 5 fleet-created unverified; caps large | structural `self_approval`, `same_owner_chain` (owner first), `wrong_currency` (USD), `out_of_scope_action` (`vendor.patch`), `out_of_scope_counterparty`, `unverified_vendor` × 5 each |
| `rogue-twin` | `rogue3@` | approve; per_action 100 000, month 250 000, per_vendor 50 000 | structural `same_owner_chain` (prior decider = rogue-approver) × 5, `over_per_action` (150 000) × 5, `over_per_vendor` (60 000) × 5; compliant 6 × 40 000 then structural `over_per_period` (20 000) × 5 |
| `rogue-burst` | `rogue4@` | create/approve/export/bank_change.decide; caps large | compliant 10 `vendor.create`; behavioural `circular` × 5 (verified by a person), `split` 3 × 40 000 on 5 vendors (third labelled), `injection` × 5 (`FakeDecider` true), `velocity` 25 exports in 50 s (21–25 labelled); structural `agent_bank_change` (decides `ap-bot-1`'s proposals) × 5 |

---

### Task 1: Script + measure (`benchmark/history/agents.py`)

**Files:** Create `benchmark/history/agents.py`, `tests/test_trust_agents_sim.py`.

**Interfaces:**
- Produces: `Step(agent: str, label: str, kind: str | None, action: Action)` (frozen dataclass); `EXPECTED: dict[str, str]` kind → `AGENT-*` type; `script(vendors: list[str], invoices: list[dict]) -> list[Step]` (pure, sorted by `occurred_at`); `measure(tenant_id, graph, findings, manifest, url) -> dict` with `structural{n, blocked}`, `behavioural{n, flagged}`, `compliant{n, false_blocks, false_reviews}`, `by_kind{kind: {label, n, caught}}`, `elapsed_s`, `gates{…four…}`.

- [ ] **Step 1: failing tests** — `test_the_script_labels_every_kind_at_least_five_times` (each `EXPECTED` kind ≥ 5, compliant ≥ 40, labels ⊆ {compliant, structural, behavioural}, kind None iff compliant, timestamps non-decreasing); `test_velocity_labels_only_actions_21_to_25` and `test_split_labels_only_the_approval_over_the_line`; `test_measure_meets_all_four_gates` (fresh schema, tenant, graph with 30 vendor + 30 invoice nodes, `TRUST_MANDATE_KEY` set; every gate true, every `by_kind` caught == n, false_blocks 0); `test_a_dead_rule_fails_the_structural_gate` (monkeypatch `rules.RULES` replacing `self_approval` by a None-returning function: `by_kind["self_approval"]["caught"] == 0`, gate false); `test_eval_reports_the_four_agent_gates` (`eval.run` on a generated seed-7 history; the four keys in `gates`).
- [ ] **Step 2:** run, see them fail (no module).
- [ ] **Step 3:** implement per the Fleet table and D1–D4.
- [ ] **Step 4:** the script tests pass now; the gate tests pass only once B and C are on `main` (until then a stand-in of the specs' rules, kept in the scratchpad, proves the script design).
- [ ] **Step 5:** commit.

### Task 2: `/v2/agents*` façade, probes, doc (after A lands)

**Files:** Modify `invoice_processing/api/v2_routes.py`, `tests/test_trust_api_routes.py`, `docs/API_V2.md`.

**Interfaces:** Consumes A's `trust_agent_routes.{register_agent, propose_mandate, evaluate_action, get_ledger}` and their body models (names as A ships them).

- [ ] **Step 1: failing tests** — `test_agent_routes_answer_what_their_api_routes_answer` (admin bearer registers and proposes via `/v2` and `/api`: same status and shape; evaluate `dryrun` and ledger via an approver key: equal bodies); `test_registry_posts_refuse_a_key_principal` (403 on `/v2/agents` and `…/mandates` with an approver key); PROBED += the four paths, `_calls` rows and fills (`agent_id` = the tenant's registered agent), the isolation test's `range` grows to cover them.
- [ ] **Step 2:** run, fail (404).
- [ ] **Step 3:** four routes, each declaring the handler's own role dependency (`Admin` for the POSTs to registry, `Approver` otherwise) and returning the handler's answer; the "Agents" section of `docs/API_V2.md` (what an agent and a mandate are, the four routes as `| METHOD | `path` |` rows, the `Action` body, decisions, `agent.action.blocked` payload, engine off unless enabled, human admin for the POSTs).
- [ ] **Step 4:** `pytest tests/test_trust_api_routes.py tests/test_tenant_isolation_api.py -q` green, including `test_the_doc_lists_every_v2_route_and_nothing_else`.
- [ ] **Step 5:** commit.

### Task 3: Merge `main` (A, B, C), gates green, verify

- [ ] merge `main`; adapt the fleet to the shipped rule/detector signatures (signing via A's `mandates`, C's injection question id for the `FakeDecider`) without editing their files.
- [ ] `uv run ruff check . && uv run ruff format --check .`; full SQLite suite; Postgres `trust_w4_simulation` run; `python -m benchmark.history.eval --seed 7` exits 0 with the four `agents_*` gates true.
- [ ] fable review → one fix pass (each fix test-first) → push, PR.

## Review Focus

1. A rule that fires on the wrong action (e.g. split on compliant repeat approvals) — `false_reviews` stays visible in the output; the gate test asserts `false_reviews == 0` too.
2. A dead rule hidden by another rule blocking the same action — D2 plus the dead-rule test.
3. Velocity bleeding into neighbouring actions — 15-min spacing; burst labels asserted exactly.
4. Elapsed time over 60 s on Postgres — `elapsed_s` reported; Postgres run checked.
5. `/v2` POST by an API key — 403 asserted.
