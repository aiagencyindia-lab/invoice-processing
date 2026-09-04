# Benchmark changelog

Every change made because the benchmark found something. One entry per change,
newest last. A change with no root cause named is a change that should not have
been made.

Format: date · problem · root cause · fix · files · tests · result · regression.

---

## 2026-09-04 — Baseline established

**Problem.** The 14 validation checks had never been measured against anything
but three exemplary Australian cases.

**What was done.** Branched `benchmark/india-gst` from `260d7e9`; captured every
repo gate first (`benchmark/baseline/current-test-results.md`); vendored the
300-invoice synthetic dataset under `benchmark/dataset/` read-only (the 33 MB of
PDFs stay outside git); wrote the harness (`benchmark/harness/`) and the two
descriptive baselines (`architecture.md`, `current-validation-map.md`).

**Product changed.** Nothing. `pyproject.toml` gained a ruff exclusion for the
vendored dataset generator and a `PLC0415` exemption for the harness.

**Regression status.** All gates green (1434 tests, 21/21, 27/27, eval GATE
PASSED, 18 e2e).

### Configuration decision recorded here because it shapes every BEFORE number

`require_work_authorization` is **off** in the baseline profile. Check 1.4 can
only be satisfied by a Work Authorization *form inside the submission PDF*
(`waf_count`), and the worker materialises only `invoice.pdf`; this dataset
carries WA *reference numbers* instead. With the flag on, 9 of the 14 invoices
in the smoke run (`benchmark/runs/smoke15-require-wa-on/`) were rejected at 1.4
before phases 2–4 ran, which would have produced a baseline measuring one broken
check and nothing else. With it off, every other check is measured and 1.4's 48
expected failures are scored as false negatives — the honest reading: the
application cannot validate a work-authorization reference today. Both values
are reachable through the settings API, so this stays within "configured as an
administrator could configure it".

---

## 2026-09-04 — P0: a Vertex call that cannot hang

**Problem.** One benchmark document took 294 seconds against a ~60 second norm,
and the repo already records two 15–25 minute stalls.

**Root cause.** `model.generate_content` takes no timeout and the RPC beneath it
has none either, so a stalled request blocked its caller indefinitely. In the
worker that is worse than slow: the queue lease expires, the document is
redelivered, stalls again, and after three attempts an invoice nobody rejected
is dead-lettered. The critic had already hit this and had already fixed it
(`guarded_generate`) — in its own file, shared with nobody.

**Fix.** Moved the guard to `invoice_processing/llm_guard.py`; the critic
re-exports it so every existing caller and test keeps its import. The acting
pipeline's three Vertex calls now go through one `_generate` helper with
`VERTEX_TIMEOUT_SECONDS` (default 180). A timeout raises, so the classifier
falls back to keywords, extraction is marked failed, and the worker's existing
retry and dead-letter path does its job.

**Files.** `invoice_processing/llm_guard.py` (new),
`shared_libraries/investigation/investigate_agent_reconst.py`,
`shared_libraries/acting/general_invoice_agent.py`.

**Tests.** `tests/test_vertex_deadline.py` — a stalled call is abandoned; an
infrastructure error is retried; a malformed request is not; an AST sweep fails
if any future code calls `generate_content` outside `_generate`; the critic and
the pipeline share one guard object.

**Result.** 1464 passed. Reverting the routing fails
`test_no_pipeline_code_calls_vertex_directly`. The critic's own 13 tests pass
unchanged, which is what makes this a move rather than a rewrite.

**Regression status.** Green.

---

## 2026-09-04 — P1: a reference date step 3.3 can be held to

**Problem.** "Is this invoice dated in the future?" was answered against
`date.today()`, read at validation time, so the dataset's 48 future-dated
invoices stop being future-dated as the calendar advances and the benchmark
cannot be reproduced. Separately, the corpus prints seven date formats and
`parse_date` knew five — and an unparseable date makes step 3.3 emit no verdict
at all, so a future-dated invoice passes because nobody looked.

**Root cause.** No injection point for the clock; a hardcoded format list.

**Fix.** `_reference_date()` reads `INVOICE_REFERENCE_DATE` (ISO) and falls back
to today, including when the value is unreadable — a typo must not stop the
queue. An environment variable rather than a tenant setting: a customer who can
move the clock can accept invoices dated into next year. `_DATE_FORMATS` gains
`09-Aug-2026`, `09.08.2026`, `09 August 2026`, `09 Aug 2026`, `Aug 09, 2026`.

**Files.** `shared_libraries/acting/general_invoice_agent.py`.

**Tests.** `tests/test_reference_date.py` — the date comes from the environment;
unset means today; an unreadable value falls back rather than failing; the eight
printed formats parse; nonsense is still `None`.

**Result.** 1464 passed. Reverting to `date.today()` fails
`test_it_comes_from_the_environment`.

**Regression status.** Green.
