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
