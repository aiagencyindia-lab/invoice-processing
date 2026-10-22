# v2 Wave 4 Behavioural Detectors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill `behaviour.RULES` with the four FR-28.4 detectors (circular, split, velocity, injection), the lazy text loader they read, and cited explanations for all nine `AGENT-*` finding types.

**Architecture:** Each detector is a pure `fn(action, agent, mandate, store, settings, now, decider) -> Reason | None` over the `AgentStore` ledger, the `Action` and the merged thresholds; every one returns `REVIEW` (B10). Injection is deterministic patterns first, then one `decide()` yes/no with `fallback=True`. Templates read only the finding's details and cited nodes (agent node + subject node).

**Tech Stack:** Python 3.11, SQLAlchemy 2 (via `AgentStore`), pdfplumber, pytest.

**Spec:** `docs/superpowers/specs/2026-10-22-v2-wave4-behaviour-design.md`; program plan `docs/superpowers/plans/2026-10-22-v2-wave4-program.md` (branch C, B10, B12, B15).

## Global Constraints

- Every detector severity `REVIEW`; rule names `circular`, `split`, `velocity`, `injection`; types `AGENT-CIRCULAR`, `AGENT-SPLIT`, `AGENT-VELOCITY`, `AGENT-INJECTION`.
- Thresholds read from `settings` (merged `trust.thresholds`): `agents.velocity_window_minutes` 10, `agents.velocity_max` 20, `agents.split_window_days` 7, `agents.split_min_count` 2, `agents.injection_min_hits` 1.
- Windows end at `action.occurred_at or now`, never wall-clock; `dryrun` rows never count (the store already excludes them).
- Money is `Decimal`. Excerpts ≤ 120 chars, at most three, each through `masking.mask_text`. No ids/emails in log `extra`.
- `inputs.text_of(document, blobs) -> Callable[[], str]`; PDF text ≤ 20 pages, ≤ 200 000 chars; email body when `source == "email"`; no OCR.
- Only owned files change (program plan "File ownership", branch C).

## Review Focus

1. A clean Indian e-invoice carries a 64-hex IRN: the base64 pattern must not hit it (requires upper, lower and a digit) — corpus sample in Task 2.
2. `decide` raising or returning garbage → review, never silence — Task 2 test with `FakeDecider(fail=True)`.
3. Split with a prior approval in another currency or with no amount → ignored, not summed — Task 1.
4. Velocity/split rows at exactly the action's instant count (window is `[at − w, at]`) — Task 1 edge tests.
5. A text loader that raises → no signal, no crash — Task 2 (`inputs`) and Task 2 (`injection` with a raising `text`).

---

### Task 1: circular, split, velocity

**Files:** Modify `invoice_processing/trust/agents/behaviour.py`; Test `tests/test_trust_agents_behaviour.py`.

**Interfaces:** Consumes `AgentStore.creators_of/actions/count_since`, `policy.Action/Reason`. Produces `behaviour.circular/split/velocity` and `behaviour.RULES` (Task 2 appends `injection`).

- [ ] Tests (SQLite `AgentStore` with `GraphStore`, ledger rows via `record_action`):
  - `circular`: approve to a vendor this agent created → `Reason("circular","AGENT-CIRCULAR","REVIEW",…)`; created by a same-owner agent (owner differs only in case) → reason; another owner's agent → None; non-approve action or no `vendor_key` → None; a dryrun or BLOCKed `vendor.create` → None.
  - `split`: cap `scope.caps.per_action = "100000"`, two priors 40 000 + this 40 000 → reason; sum exactly 100 000 → None; one prior ≥ cap → None; prior outside 7 days → None; prior BLOCK / dryrun / other currency / other vendor → not counted; no cap and no `high_value_threshold` → None; `settings["high_value_threshold"]` used when no cap.
  - `velocity`: 20 counted rows in `[at−10m, at]` → reason; 19 → None; a row at `at−10m` counts, one at `at−10m−1s` does not; a row at `at` counts; rows measured at `occurred_at` of a log action, not `now`.
- [ ] Run → fail; implement; run → pass; commit.

### Task 2: injection patterns, text input, detector

**Files:** Create `invoice_processing/trust/agents/{injection,inputs}.py`, `tests/fixtures/trust_agents/{injected,clean}.json`; Modify `provenance/pdfmeta.py` (add `page_text(data: bytes, max_pages: int, max_chars: int) -> str`), `behaviour.py`.

**Interfaces:** `injection.normalise(text) -> str` (zero-width removed, spaces collapsed, newlines kept); `injection.scan(text) -> list[Hit]`, `Hit(pattern: str, start: int, end: int)` with offsets into `normalise(text)`; `injection.excerpts(text, hits, n=3, width=120) -> list[str]` (masked). `inputs.text_of(document: dict, blobs) -> Callable[[], str]` (memoised).

- [ ] Tests: every injected sample (10) has ≥ 1 hit; every clean sample (9 hand-written + 5 exemplary PDFs read through `text_of` + 6 `trust_payment` emails) has 0; each pattern named in the spec hits its own one-liner; excerpts masked and ≤ 120; `text_of` reads a made PDF and an email body, is lazy (blobs untouched until called), returns "" on a missing blob; detector: hit + decider true → reason with excerpts; decider false → None; decider fails → reason; no hit → `FakeDecider` never called; `text=None` → None.
- [ ] Run → fail; implement; run → pass; commit.

### Task 3: explanations for the nine `AGENT-*` codes

**Files:** Modify `invoice_processing/trust/explain/templates/agents.py`, `tests/test_trust_explain.py` (drop `WAVE4_PENDING`, add `"agents"` to `ENGINE_TYPES`, allow actor kind `agent`); Create `tests/fixtures/trust_explain/AGENT-*.json` (nine).

- [ ] Fixtures mirror `record.record`'s finding: citations `[agent node, invoice node]`, `details.reasons`, `agent_id`, `action`, no impact (counterfactual note). Template: `what_happened` = first reason's message; `why` one step per reason on the agent node, plus a step on the subject node; `caused_by = Actor("agent", "{name} (owner {owner})", agent node)`; per-type `next_action` and `would_change`.
- [ ] Run the explain suite → nine codes explain, `check()` passes; commit.

### Task 4: verification

- [ ] `uv run ruff check .`, `uv run ruff format --check .`; full SQLite suite; Postgres `tests/test_trust_*.py` on `trust_w4_behaviour`; `python -m benchmark.history.eval --seed 7` all green.
