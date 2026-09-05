# Repo gates before any benchmark change

Captured by `bash benchmark/harness/gates.sh benchmark/runs/gates-before` on
2026-09-04 at `260d7e9`, immediately after branching `benchmark/india-gst` and
before a single line of the pipeline changed. Raw output is under
`benchmark/runs/gates-before/` (untracked; regenerate with the same command).

| Gate | Result | Evidence |
|---|---|---|
| `pytest tests/ -q` | PASS | `1434 passed, 5 warnings in 40.85s` |
| `ruff check .` | PASS after excluding the vendored dataset generator | see note |
| `ruff format --check .` | PASS after the same exclusion | see note |
| `scripts/verify_phase0.py` | PASS | `21/21 passed` |
| `scripts/verify_phase1.py` | PASS | `27 passed, 0 failed, 0 skipped` |
| `eval/eval.py … --require-baseline` | PASS | `GATE: PASSED` |
| frontend `npm run lint` | PASS | |
| frontend `npx tsc --noEmit` | PASS | |
| frontend `npm run test:e2e` | PASS | `18 passed (10.2s)` |

**The ruff note.** The first run failed on
`benchmark/dataset/generator/*.py` — four modules copied verbatim from the
dataset drop. They are evidence of how the corpus was produced, are never
imported and are never edited, so they are excluded in `pyproject.toml`
(`extend-exclude`) rather than reformatted: a reformatted copy could no longer
be diffed against the original. Nothing under `invoice_processing/` was touched.

`benchmark/harness/*.py` carries one lint exemption (`PLC0415`, imports inside a
function). The stack module must fix its environment before importing the
product, because `paths` and the store resolvers bind at import time; the score
module imports the product lazily so a saved run can be re-scored without them.

**This is the regression floor.** Every product change made for the AFTER run
re-runs this script, and the same table appears in the final report with the
after-values beside these.
