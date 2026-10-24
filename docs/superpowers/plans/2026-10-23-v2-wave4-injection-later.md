# Injection classified after the write Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the injection detector (PDF text read + model call, default timeout 120 s) off the inline guard path and out of the agent row lock; run it in the worker after the write, so the inline p95 ≤ 2 s holds (PENDING Wave 4 item 1).

**Architecture:** The guard evaluates every rule except the deferred detector, records the decision, releases the lock, then queues `trust.agent.injection` keyed on the ledger row. The worker job reads the invoice text, scans it, asks the model only on a deterministic hit, and on "yes" raises its own `AGENT-INJECTION` finding and appends the reason to the ledger row (ALLOW → ALLOW-WITH-REVIEW; other decisions unchanged).

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2, the existing worker queue (`worker/runner.py` typed jobs), pytest.

**Spec:** design agreed in chat 2026-10-23 (option B); program plan `docs/superpowers/plans/2026-10-22-v2-wave4-program.md` (B10 detectors are review-only, B12 no OCR, B13 record ordered not atomic).

## Global Constraints

- Detectors never block; insights fail open. The deferred job never changes a BLOCK and never refuses anything.
- Only masked excerpts reach the model (`injection.excerpts`), never the whole document.
- No ids/emails/secrets in log `extra` (error type only).
- `policy.evaluate_action` without arguments keeps running all four detectors (the benchmark measures injection through it).
- Ruff exactly as CI: `uv run ruff check . && uv run ruff format --check .`

## Review Focus

- The job runs twice (redelivery) → one finding, the reason appended once. Pinned in Task 2.
- The model is down → `decide` falls back to `True` (fail toward review) → finding raised. Pinned in Task 2.
- The ledger row or agent is gone, or the subject is not an invoice → the job acks and does nothing. Pinned in Task 2.
- The queue cannot be reached when the guard enqueues → the governed write still answers 200; a warning is logged. Pinned in Task 3.
- A refused BLOCK in enforcing (nothing was written) → no job queued. Pinned in Task 3.

---

### Task 1: The guard evaluates without the deferred detector

**Files:**
- Modify: `invoice_processing/trust/agents/behaviour.py` (add `DEFERRED: tuple = (injection,)`), `invoice_processing/trust/agents/policy.py` (`evaluate_action(..., skip: tuple = ())`: detectors in `skip` are not run)
- Modify: `invoice_processing/trust/agents/inputs.py`: `invoice_text(tenant_id: str, case_id: str) -> Callable[[], str]` (the document + blobs lookup now inline in `guard.invoice_action.text`), reused by `guard.invoice_action`
- Test: `tests/test_trust_agents_policy.py`, `tests/test_trust_agents_guard.py` (`test_the_invoice_text_is_the_documents_loaded_only_when_asked` keeps passing)

- [ ] **Step 1: Failing test** `test_skipped_detectors_do_not_run` — `behaviour.RULES = (asked, _rule(REVIEW))` where `asked` records a call; `evaluate_action(..., skip=(asked,))` → `asked` never called, reasons `(REVIEW,)`.
- [ ] **Step 2:** Run; FAIL (unexpected keyword).
- [ ] **Step 3:** Implement; guard `_one` passes `skip=behaviour.DEFERRED`.
- [ ] **Step 4:** Run agent suites; PASS.
- [ ] **Step 5:** Commit.

### Task 2: The deferred injection job

**Files:**
- Create: `invoice_processing/trust/agents/later.py`
- Modify: `invoice_processing/trust/agents/record.py` (add `injection`)
- Test: `tests/test_trust_agents_later.py`

**Interfaces:**
- Produces: `later.classify(tenant_id: str, action_id: str, *, store, findings, text: Callable[[], str] | None = None, settings: dict | None = None, decider=None) -> bool` — True when it raised the finding. Defaults: `text` from `inputs.invoice_text` for an `invoices:{case_id}` subject (any other subject → returns False); `settings` merged thresholds as the guard reads them. Runs `behaviour.injection(action, agent, None, store, settings, now, decider)` on an `Action` rebuilt from the row (kind, subject, vendor, amount, currency, transaction_node, occurred_at, text).
- Produces: `record.injection(ctx, row: dict, agent: dict, reason: Reason, *, store, findings) -> str` — upserts the finding (type `AGENT-INJECTION`, `dedupe_key=f"agent_injection:{row['id']}"`, transaction node and citations as `record.record`, details as `record.record` with `source` = the row's and `"deferred": True`), then in one session on the ledger row: if no reason with `rule == "injection"` yet, append it to `reasons`, append its message to `explanation` (≤ 1000), set `decision` ALLOW → ALLOW-WITH-REVIEW, and set `finding_id` if empty. Returns the finding id.

- [ ] **Step 1: Failing tests** — `test_a_yes_raises_the_finding_and_marks_the_row` (ALLOW row + text with a hit + `FakeDecider({"injection": True})` → True; row ALLOW-WITH-REVIEW with the injection reason; one `AGENT-INJECTION` finding citing the agent node); `test_a_no_leaves_everything` ; `test_no_hit_never_asks_the_model` (decider records no prompt); `test_a_model_outage_asks_for_review` (`FakeDecider({}, fail=True)` → finding); `test_running_twice_raises_one_finding_and_one_reason`; `test_a_block_stays_a_block_with_its_own_finding`; `test_a_missing_row_or_non_invoice_subject_does_nothing`.
- [ ] **Step 2:** Run; FAIL (no module).
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; PASS.
- [ ] **Step 5:** Commit.

### Task 3: Queue it from the guard; run it in the worker

**Files:**
- Modify: `invoice_processing/worker/runner.py` (`TRUST_AGENT_INJECTION = "trust.agent.injection"`; `enqueue_agent_injection(queue, tenant_id, action_id, request_id="") -> str`; `_handle_agent_injection` registered in `_typed`, via `_run_claimless(key=f"agent_injection:{action_id}", event="trust_dead_lettered", done="agent-injection-classified")`; malformed (no tenant/action id) → dead-letter; `_dead_letter_event` maps it to `trust_dead_lettered`)
- Modify: `invoice_processing/trust/agents/guard.py` (`_one`: after the lock is released, when `action.text is not None` and the decision has an `action_id` and is not a refused BLOCK, call `enqueue_agent_injection(service.get_queue(), ...)`; any exception → `_log`, the decision stands)
- Test: `tests/test_trust_agents_guard.py`, `tests/test_trust_worker.py`

- [ ] **Step 1: Failing tests** — guard: `test_injection_is_queued_after_the_write_not_asked_inline` (advisory, text with a hit, a decider that would fail the test if called inline → 200, ledger ALLOW, no `AGENT-INJECTION` finding, one queued message `{type, tenant_id, action_id}` for the row); `test_a_refused_block_queues_nothing`; `test_a_vendor_action_queues_nothing`; `test_an_unreachable_queue_does_not_fail_the_write`. Worker: `test_the_agent_injection_job_classifies_and_acks`; `test_a_malformed_agent_injection_job_is_dead_lettered`.
- [ ] **Step 2:** Run; FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run guard + worker suites; PASS.
- [ ] **Step 5:** Commit.

### Task 4: Close-out

- [ ] Full suite, ruff (CI form), Postgres agent/trust/isolation/worker suites on a fresh `pg-w1` DB, benchmark `--seed 7` (56/56).
- [ ] PENDING: tick item 1 (classified after the write in the worker; the finding lands after the request). Program-plan note not needed.
- [ ] Fable review; one fix pass; PR; merge on the user's word.
