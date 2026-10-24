# Agent drift detector (G1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** FR-28.4 "drift from mandate": flag (review, never block) an agent action whose amount is far beyond anything that agent has handled before.

**Architecture:** A fifth behavioural detector `behaviour.drift` beside circular/split/velocity/injection, reading the agent's own ledger like `split` does; a new finding type `AGENT-DRIFT`; three thresholds; one benchmark scenario.

**Tech Stack:** Python 3.11, pytest; the Wave 4 fleet benchmark.

**Spec:** agreed in chat 2026-10-24 (the user said "go ahead" with reading 1: drift from the agent's own past). Builder's calls: amount outlier only — the "first-time action kind" signal was dropped because the fleet's compliant agents (approve, then export, then propose a bank change) trip it on ordinary work, and the mandate already decides which kinds are allowed.

## Global Constraints

- Detectors never block (program plan B10): severity `REVIEW`.
- Windows are measured at the action's own time (`_at`), never the wall clock; dry runs never count (`store.actions` sources inline/log only).
- Eval `--seed 7` must keep every gate (56 + no new failures); compliant false blocks stay 0.

## Review Focus

- An agent with fewer than `agents.drift_min_history` prior actions → never flagged (a new agent is not drift).
- A larger amount in a different currency → not compared (no FX).
- Blocked prior actions do not count as history (only ALLOW / ALLOW-WITH-REVIEW).
- An action exactly at the factor × max boundary → not flagged (strictly greater).
- A missing or non-positive amount → no flag.

---

### Task 1: The detector, type and thresholds

**Files:** `invoice_processing/trust/agents/behaviour.py` (`drift`, in `RULES` after `velocity`), `invoice_processing/trust/types.py` (`AGENT-DRIFT`), `invoice_processing/trust/explain/templates/agents.py` (NEXT/WOULD entries), `invoice_processing/trust/thresholds.py` (`agents.drift_window_days: 90`, `agents.drift_min_history: 10`, `agents.drift_amount_factor: 3`); tests `tests/test_trust_agents_behaviour.py`, `tests/test_trust_settings.py` snapshot.

`drift(action, agent, mandate, store, settings, now, decider)`: only `invoice.approve` / `invoice.export` with an amount > 0; history = `store.actions(tenant, agent_id, action in (approve, export) — two calls or one without `action=` filtered to those kinds, since = at − window, until = at (exclusive of this action), decisions ALLOW/ALLOW-WITH-REVIEW, sources inline/log)` in the same currency with amounts; fewer than min → None; flag when `amount > factor × max(history)`. Message: "{name} handled {amount} {currency}, more than {factor}× the largest ({max}) of its last {n} actions in {days} days."

- [ ] Failing tests for each Review Focus line plus the positive case and the explanation template (`explain.build` on an `AGENT-DRIFT` finding names what happened and the next move).
- [ ] Implement; run behaviour, explain, settings suites; commit.

### Task 2: Benchmark scenario

**Files:** `benchmark/history/agents.py` (`EXPECTED["drift"] = "AGENT-DRIFT"`, `BEHAVIOURAL += drift`, a `rogue-drift` agent with ten in-scope approvals at 10 000 then five at 50 000 marked `drift`); `tests/test_trust_agents_sim.py` if it pins the scenario count.

- [ ] Failing: eval reports `by_kind["drift"]` with n=5, caught=5; gates unchanged.
- [ ] Implement; eval 56/56 (or the gate count it reports) with compliant false blocks 0; commit.

### Task 3: Close-out

- [ ] PENDING: tick G1. Full suite; Postgres agent suites; fable review; one fix pass; PR; merge on the user's word.
