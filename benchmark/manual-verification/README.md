# Check it yourself

Everything in `benchmark/comparison/` was produced by a script that I wrote,
scoring runs that I ran. This package exists so you can disagree with it.

## What is here

| File | What it is |
|---|---|
| `test-instructions.md` | How to start the application and put an invoice through it, by hand |
| `test-matrix.csv` | ~45 invoices, one per scenario per company, with what the dataset expects and what each run recorded — and an empty **Manual result** column |
| `ground-truth.csv` | Every check's expectation for the sampled invoices, straight from the dataset |
| `before-results.csv` | All 4,200 scored rows from the BEFORE run |
| `after-results.csv` | The same 4,200 rows from the AFTER run |
| `before-vs-after.csv` | Per-check accuracy, both runs, and the change |

## The short version

1. Follow `test-instructions.md` to start a stack for one company (about five
   minutes; three terminals).
2. Open `test-matrix.csv`. Each row names a PDF and what should happen to it.
3. Upload it, wait for Done, open the invoice, read the decision and the
   findings.
4. Write `agree` or `disagree` in **Manual result**.

Twenty minutes gets you the five rows that matter most; ninety gets you all 45.

## What the columns mean

- **Expected behavior / Expected rejection** — the dataset's answer, computed
  by its generator from the faults it deliberately planted. Not my opinion.
- **Expected first failing check** — where a pipeline that stops at the first
  problem should stop. Several of these invoices carry three to five faults;
  catching a different one of them is still catching a real fault, and the
  benchmark counts that separately rather than as a miss.
- **Before result / After result** — the decision and reason each run recorded.
- **Before / After check verdict** — what that row's own check said:
  `PASS`, `FAIL`, `NOT_RUN` (the pipeline stopped earlier), `SKIPPED`.
- **Manual result** — yours. Left empty on purpose.

## Two things worth knowing before you start

**`NOT_RUN` is not a pass.** The pipeline stops at the first failing phase, so
most checks on a rejected invoice never ran. Where the dataset expected that
check to be the one that caught the invoice, the benchmark counts `NOT_RUN` as
a miss.

**Duplicates are caught in two places.** An identical file is refused at upload
before any processing — you will see it rejected without a new invoice
appearing. A re-rendered copy goes through the pipeline and is rejected at
check 3.1. Both are correct; the reports count them separately.

## If your answer differs from the benchmark's

Write it in **Notes** and change nothing else. That disagreement is the most
useful thing this package can produce, and it is worth more unedited.
`benchmark/baseline/current-validation-map.md` says what each check actually
reads, which is usually enough to tell a wrong expectation from a wrong
implementation.
