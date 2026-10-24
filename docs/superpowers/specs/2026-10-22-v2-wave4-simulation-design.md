# Wave 4 — Simulation harness, `/v2` agents, docs design (`v2/w4-simulation`)

**Program plan:** `docs/superpowers/plans/2026-10-22-v2-wave4-program.md`, branch D. AC-28, PRD §9. Decision U7; calls B10, B12. Merged last.

**Owns:** `benchmark/history/agents.py`, `api/v2_routes.py` (agents routes), `docs/API_V2.md` (agents section), `tests/test_trust_agents_sim.py`, `tests/test_trust_api_routes.py` (`PROBED` += the four paths), its plan doc.

## Fleet (`benchmark/history/agents.py`)

A `measure(tenant_id, graph, findings, manifest, url) -> dict` module, picked up by `eval._measure_modules()` (the Wave 3 plug-in) after the engines ran; it acts on the base history's vendors and invoices:
- Registers on the bench tenant: two admins (`admin-a`, `admin-b` emails), three compliant agents (owners distinct) and five rogue agents, each with a mandate proposed by `admin-a` and approved by `admin-b` (one rogue's mandate left `proposed`, one expired). `TRUST_MANDATE_KEY` set for the run.
- Drives scripted `Action`s through `policy.evaluate_action` + `record(source="inline")` in timestamp order (no HTTP, no guard — the decision is what AC-28 measures).
- **Compliant set** (≥ 40 actions): in-scope approvals under every cap, exports of approved invoices, human-created vendors, bank changes proposed by agents and decided by people.
- **Structural violations** (each ≥ 5 instances): self-approval, same-owner chain (two agents; owner then agent), over per-action / per-period / per-vendor cap, wrong currency, unapproved mandate, expired mandate, out-of-scope action, out-of-scope counterparty, approving an agent-created unverified vendor, agent deciding an agent-proposed bank change.
- **Behavioural violations** (each ≥ 5): circular (agent creates vendor, approves its invoice), split (3 × 40 000 under a 100 000 per-action cap in 7 days), velocity (25 actions in one minute), injection (invoice text carrying an instruction; `FakeDecider` answers true).
- Every scripted action carries a label (`compliant`, `structural`, `behavioural`); only the actions that should be blocked/flagged carry a violation label (velocity: actions 21–25; split: the approval that crosses the line) (B18).
- Returns `{structural: {n, blocked}, behavioural: {n, flagged}, compliant: {n, false_blocks, false_reviews}, elapsed_s, gates: {agents_structural_blocked: blocked/n == 1.0, agents_behavioural_flagged: flagged/n ≥ 0.9, agents_false_blocks: false_blocks == 0, agents_latency_60s: elapsed_s ≤ 60}}`. `false_reviews` is reported, not gated.

## `/v2` (`v2_routes.py`)

| v2 | calls |
|---|---|
| `POST /v2/agents` | `trust_agent_routes.register_agent` |
| `POST /v2/agents/{agent_id}/mandates` | `trust_agent_routes.propose_mandate` |
| `POST /v2/agents/{agent_id}/actions/evaluate` | `trust_agent_routes.evaluate_action` |
| `GET /v2/agents/{agent_id}/ledger` | `trust_agent_routes.get_ledger` |

Import + call + return, as the Wave 3 façade. The two registry POSTs need a human admin (a key principal gets 403), documented. Mandate approval stays on `/api` only (a person's act).

## Docs

`docs/API_V2.md`: an "Agents" section — what an agent and a mandate are, the four routes, the `Action` body, decisions, the `agent.action.blocked` event payload, and that the engine is off unless enabled. The doc test keeps documented == routed.

## Tests

`measure` on SQLite meets all four gates and reports counts per violation kind; a broken rule (monkeypatched to return `None`) fails the structural gate (the harness can fail); `/v2` parity with `/api` for the same principal; probes with bearer and foreign key; `eval --seed 7` output contains the four gates.

**Done:** ruff; suite both dialects; `python -m benchmark.history.eval --seed 7` all gates green including the four `agents_*`.
