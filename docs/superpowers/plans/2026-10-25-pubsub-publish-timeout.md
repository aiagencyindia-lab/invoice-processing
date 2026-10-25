# Pub/Sub publish timeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A slow Pub/Sub holds a request for at most 5 s, not 30 s.

**Architecture:** `PubSubQueue.publish` keeps waiting for the broker's
message id (so `queued` stays true and a failed publish stays an error in the
request's log), only shorter. Every caller already catches a failed enqueue.

**Tech Stack:** Python, google-cloud-pubsub, pytest.

**Spec:** design agreed in chat 2026-10-25 (PENDING Wave 5 item "`PubSubQueue.publish` waits up to 30 s").

## Global Constraints

- Timeout is 5 seconds, a module constant `PUBLISH_TIMEOUT_SECONDS` in `invoice_processing/worker/queue.py`; no env knob.
- No fire-and-forget: `publish` still returns the broker's message id.

## Review Focus

- A publish that times out raises (`TimeoutError` from the future) and the
  upload is still accepted with `queued` empty — already the behaviour of
  `ingest.py`'s `except Exception`; the task's test pins the timeout value.

---

### Task 1: Shorten the publish wait

**Files:**
- Modify: `invoice_processing/worker/queue.py` (`PubSubQueue.publish`)
- Test: `tests/test_production_paths.py` (`TestPubSubQueue`)
- Modify: `docs/PENDING.md` (tick the item)

- [ ] **Step 1: Failing test** `test_publish_waits_at_most_five_seconds_for_the_broker`: `FakeFuture.result` records the `timeout` it was given; after `queue.publish({...})`, assert it equals `5` and equals `queue_module.PUBLISH_TIMEOUT_SECONDS`.
- [ ] **Step 2: Run** `./.venv/bin/python -m pytest tests/test_production_paths.py -k five_seconds -q -p no:cacheprovider` — expect FAIL (30 ≠ 5 / no constant).
- [ ] **Step 3: Implement** `PUBLISH_TIMEOUT_SECONDS = 5` with a comment, used in `future.result(timeout=PUBLISH_TIMEOUT_SECONDS)`.
- [ ] **Step 4: Run** the `TestPubSubQueue` class — expect PASS.
- [ ] **Step 5: Tick PENDING**, run full suite + `uv run ruff check .` and `uv run ruff format --check .` (unpiped), commit.
