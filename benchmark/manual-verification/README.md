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

## A second opinion, and where to look first

The three `Claude review` columns are **not** the manual result, which is still
empty. They were written on 2026-10-07 by reading each PDF directly (the scans
as images) against the dataset's planted faults and reference masters, not from
the scorer's output. That makes them independent of the scripts but not of the
model that wrote the scripts, which is the gap your column closes.

- `expectation`: does the page actually show what the dataset says? (`holds`,
  `doubtful`, `wrong`)
- `after result`: was the after-run's decision and reason right for the page?
  (`correct`, `correct-other-fault`, `wrong-reason`, `wrong`)

The rows most worth your time, in order:

| Row | Claude's view | Why look |
|---|---|---|
| MV-036 | dataset wrong, app right | The planted discount (1,184.15) is not on the page: line 2 prints `19,683.00 5% 18,698.85`, which is correct. The generator altered the discount *amount* on a layout that prints the *percentage*. SVC-INV-019 (MV-029) carries the same unprinted fault. |
| MV-002 | app wrong | A clean invoice rejected for its GSTIN. The text layer prints `24OXMPF9713X1Z2` (the letter O), which matches the vendor master. The app read a zero. |
| MV-018 | app wrong | One lump-sum line ("Professional services, 1 lot") accepted. 2.1 counted it as itemized. |
| MV-041 | app wrong, known | Tax is 4.20 over on 2,50,751.58. Recorded in CHANGELOG as given up deliberately: smaller than legitimate per-line drift. |
| MV-027, MV-035 | right decision, wrong reason | Rejected for "Purchase order not recognised", but the "PO" is the printed *Quotation Ref.* Neither invoice prints a PO. The real faults (GSTIN, line total) are visible. |
| MV-010, MV-015, MV-016 | right, other real fault | Rejected at ingest as duplicates, which they are, before the row's own check could be the reason. |

Smaller things the review noticed, which change no decision: the 2.2 evidence
on service invoices says "matched on quantity and unit price" when the PO
differs (MV-001, MV-003, MV-020); "billed without a tax registration" is shown
on non-tax invoices that do print a GSTIN (MV-011, MV-012); and
`extra_invoice_numbers` is empty in the dataset for MV-015 and MV-016 although
the second invoice is printed.
