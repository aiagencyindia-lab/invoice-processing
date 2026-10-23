# Wave 5 hygiene (branch D) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daily per-tenant retention sweep for `agent_actions`, and an OCR fallback for the worker's deferred injection scan.

**Architecture:** A third `_x_if_due` sweep on `Worker` calls the scaffold's `AgentStore.purge_before` per active tenant with a floored cutoff. `confidence.ocr_page_text` takes bytes, a page cap and a deadline; `inputs.text_of(ocr=True)` uses it when the text layer is thin; only `later.classify` asks.

**Tech Stack:** Python 3.11, SQLAlchemy 2, pypdfium2, tesseract (subprocess), pytest.

**Spec:** `docs/superpowers/specs/2026-10-23-v2-wave5-hygiene-design.md` (program plan B7, B8, row D).

## Global Constraints

- Owned files only: `worker/runner.py`, `trust/agents/inputs.py`, `trust/agents/later.py`, `confidence.py`, `tests/test_trust_agents_retention.py`, `tests/test_trust_agents_inputs.py`, one case in `tests/test_confidence.py`. No edit to `store.py` / `thresholds.py`.
- `RETENTION_SCAN_SECONDS = 86_400`; `MAX_OCR_PAGES = 5`; `OCR_BUDGET_SECONDS = 120` (queue ack deadline 600 s).
- Retention days = `max(_days(agents.retention_days, 400), _days(agents.split_window_days, 7) + 1, 31)`; `_days(v, d)` = `int(float(v))` for a finite non-negative number, else `d`.
- Log `agent_actions_purged` with `extra={"rows": n}` only when n > 0; no tenant id in `extra`.
- Benchmark `--seed 7` stays 56/56; existing OCR callers (Path, no cap) unchanged.

## Review Focus

1. A tenant threshold of `"abc"`, `-5`, `NaN` or `inf` for `retention_days` → default 400, never purges more, never raises. Test in Task 1.
2. A blob that is not a PDF (junk bytes, empty) with `ocr=True` → OCR never called, `""`. Test in Task 2.
3. A blob over `pdfmeta.MAX_BYTES` with `ocr=True` → OCR not called. Test in Task 2.
4. Existing guard test doubles `text_of(document, blobs)` (two positional args) must keep working: `invoice_text` passes `ocr=` only when true. Covered by the existing guard suite.
5. Deadline already passed → `ocr_page_text` renders nothing, returns `[]`. Test in Task 2.

---

### Task 1: Retention sweep

**Files:**
- Modify: `invoice_processing/worker/runner.py` (constant near `WEBHOOK_SCAN_SECONDS`; `Worker.__init__` `_last_retention_scan = None`; `run_forever` line after `_deliver_webhooks_if_due()`; new method after `_deliver_webhooks_if_due`)
- Test: `tests/test_trust_agents_retention.py`

**Interfaces:**
- Consumes: `service.get_agent_store()` → `AgentStore | None`; `AgentStore.purge_before(tenant_id, before) -> int`; `tenant_settings.effective(tenant_id)`; `thresholds.merged(...)`.
- Produces: `runner.RETENTION_SCAN_SECONDS`, `Worker._purge_agent_actions_if_due() -> None`, module helper `runner._retention_days(settings: dict) -> int`.

- [ ] **Step 1: Write the failing tests** (two active tenants on `scratch_db` + `fresh_schema`, `DATABASE_URL` set, `service._agent_store` patched to `AgentStore(url, graph=GraphStore(url))`, `tenant_settings.effective` patched per tenant):
  - `test_the_sweep_deletes_only_old_non_create_rows_per_tenant`: rows at 500, 100, 10 days plus a 500-day `vendor.create` per tenant → after the sweep each tenant keeps 100, 10 and the create; a second (forced) run deletes nothing.
  - `test_one_tenants_failure_does_not_stop_the_others`: `effective` raises for tenant A → tenant B still purged, A's rows untouched, error logged.
  - `test_the_sweep_runs_once_per_interval`: clock-driven, `purge_before` counted: 1 call per tenant, again only after `RETENTION_SCAN_SECONDS + 1`.
  - `test_retention_below_the_floor_is_raised`: `retention_days=1`, `split_window_days=7` → 31; `split_window_days=60` → 61; bad values (`"abc"`, `-5`, `"nan"`, `"inf"`) → 400.
  - `test_purged_rows_are_logged_without_the_tenant`: caplog record `agent_actions_purged` with `rows == 1`, tenant id absent from the record's extras.
- [ ] **Step 2: Run** `./.venv/bin/python -m pytest tests/test_trust_agents_retention.py -q -p no:cacheprovider` → FAIL (no attribute).
- [ ] **Step 3: Implement** the method in the shape of `_deliver_webhooks_if_due`; tenants via `Session(get_engine())` and `select(Tenant.id).where(Tenant.status == "active")`; per-tenant `try/except` logging `"Agent-action retention sweep failed for a tenant; continuing."`; outer `except` logging `"Agent-action retention sweep failed; continuing."`. Returns early when the store is `None`.
- [ ] **Step 4: Run** the file → PASS.
- [ ] **Step 5: Commit** runner + test.

### Task 2: OCR bytes path and the worker's fallback

**Files:**
- Modify: `invoice_processing/confidence.py:222-255`, `invoice_processing/trust/agents/inputs.py`, `invoice_processing/trust/agents/later.py:57`
- Test: `tests/test_confidence.py` (one case), `tests/test_trust_agents_inputs.py`

**Interfaces:**
- Produces: `confidence.ocr_page_text(pdf: Path | bytes, *, max_pages: int | None = None, deadline: float | None = None) -> list[str]`; `inputs.MAX_OCR_PAGES = 5`; `inputs.OCR_BUDGET_SECONDS = 120`; `inputs.text_of(document, blobs, *, ocr: bool = False)`; `inputs.invoice_text(tenant_id, case_id, *, ocr: bool = False)`.

- [ ] **Step 1: Write the failing tests**
  - `test_confidence.py::test_ocr_reads_bytes_like_a_path_and_stops_at_max_pages`: fake `shutil.which` + `subprocess.run` (returns `b"page"` per call, counts calls) on a 3-page PDF → bytes result == path result (3 pages); `max_pages=2` → 2 pages; `deadline=time.monotonic() - 1` → `[]`.
  - `tests/test_trust_agents_inputs.py`: `ocr=False` on a scan never calls OCR (patched to raise); `ocr=True` on a thin layer calls OCR with the blob bytes and `max_pages=5`, a deadline ≤ now+120, and its text (`"Ignore previous instructions"`) reaches the returned text and `injection.scan`; `ocr=True` on a ≥30-word layer never calls OCR; OCR returning `[]` → layer text only, no error; junk / empty / oversize blob → OCR not called; `later.classify` passes `ocr=True` to `inputs.invoice_text`.
- [ ] **Step 2: Run** both files → FAIL.
- [ ] **Step 3: Implement.** `ocr_page_text`: hand `pdf` to `PdfDocument` as-is when bytes, `str(pdf)` otherwise; before each page, break on `max_pages` reached or `time.monotonic() > deadline`. `text_of`: fetch the blob once; layer via `page_text`; when `ocr` and `not has_readable_text([layer])` and the bytes are a PDF within `MAX_BYTES`, append OCR text under `_read`. `invoice_text` calls `text_of(document, blobs)` when `ocr` is false (keeps two-arg doubles valid). Docstring: "OCR only when asked (the worker's deferred scan)". `later.classify`: `inputs.invoice_text(tenant_id, case_id, ocr=True)`.
- [ ] **Step 4: Run** both files plus `tests/test_trust_agents_behaviour.py tests/test_trust_agents_guard.py tests/test_trust_agents_later.py tests/test_supporting_docs.py` → PASS.
- [ ] **Step 5: Commit.**

### Task 3: Verification

- [ ] Full suite `./.venv/bin/python -m pytest -q -p no:cacheprovider` → all pass.
- [ ] `uv run ruff check . && uv run ruff format --check .` → clean.
- [ ] `./.venv/bin/python -m benchmark.history.eval --seed 7` → 56/56.
- [ ] Postgres: fresh db `w5_hygiene_d`, `tests/test_trust_*.py tests/test_worker.py` → pass.
