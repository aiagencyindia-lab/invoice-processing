# Drift baseline aggregate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The drift detector reads its baseline as one `count + max(amount)` query over the whole window instead of the newest 500 rows.

**Architecture:** New `AgentStore.baseline(...) -> tuple[int, Decimal | None]` with the same filters `behaviour.drift` applies in Python today; the "not a row drift flagged" filter is a text match on the stored reason type `"AGENT-DRIFT"` (portable across SQLite and Postgres, no migration). `drift` calls it; message and thresholds unchanged.

**Tech Stack:** Python, SQLAlchemy, pytest.

**Spec:** design agreed in chat 2026-10-25 (PENDING "Drift limits … the baseline reads the newest 500 money actions"). Not built (user agreed): "accepting a drift finding raises the baseline".

## Global Constraints

- Same history as today: decisions ALLOW / ALLOW-WITH-REVIEW, sources inline / log, kinds `invoice.approve` / `invoice.export`, non-null amount, same currency, `since <= occurred_at < until`, rows whose reasons carry `AGENT-DRIFT` excluded.
- No migration, no new column.

## Review Focus

- A reason message from another rule that quotes the text `AGENT-DRIFT` — JSON escapes inner quotes, so `"AGENT-DRIFT"` (with quotes) matches only a reason's `type` value.
- Postgres JSONB renders `"type": "AGENT-DRIFT"`; SQLite JSON text the same — the match is on `"AGENT-DRIFT"` alone, independent of separators.
- Currency `None` on the action: today `r["currency"] == None` matches rows with null currency; keep that (`IS NULL`).

---

### Task 1: Aggregate baseline

**Files:** Modify `invoice_processing/trust/agents/store.py` (`baseline`), `invoice_processing/trust/agents/behaviour.py` (`drift`), `docs/PENDING.md`; Test `tests/test_trust_agents_behaviour.py`.

- [ ] Failing test `test_drift_sees_past_the_newest_500_actions`: 500 rows at 10000 in the last day plus one at 100000 ten days ago; `approve("150000")` → None (today: flagged, the 100000 row is past the cap).
- [ ] Run, expect FAIL; implement `baseline(tenant_id, agent_id, *, since, until, currency, kinds) -> tuple[int, Decimal | None]` and use it in `drift`; run the drift tests, expect PASS (all existing drift tests unchanged).
- [ ] PENDING: drop the 500-row clause; full suite, ruff (unpiped), Postgres trust suites on a fresh DB, `benchmark.history.eval --seed 7`; commit.
