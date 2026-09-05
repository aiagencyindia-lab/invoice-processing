# BEFORE (`baseline`) vs AFTER (`after`)

`baseline` at `dcf0ba3`, 300 submissions; `after` at `6db9408`, 300 submissions. Same documents, same order, same ground truth, same scoring code.

## Per check (cascade-aware, full population)

| Check | n before→after | Accuracy before | after | change | Recall b→a | Precision b→a | FP b→a | FN b→a |
|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 270→273 | 82.2% | 99.3% | +17.0pt | 68.0%→98.0% | 51.5%→98.0% | 32→1 | 16→1 |
| 1.2 customer_verification | 223→226 | 86.1% | 96.9% | +10.8pt | 89.4%→85.1% | 61.8%→100.0% | 26→0 | 5→7 |
| 1.3 tax_compliance | 220→223 | 81.4% | 97.8% | +16.4pt | 0.0%→66.7% | 0.0%→75.0% | 32→2 | 9→3 |
| 1.4 work_authorization | 41→130 | 0.0% | 93.1% | +93.1pt | 0.0%→78.0% | -→100.0% | 0→0 | 41→9 |
| 1.5 single_document | 203→206 | 96.5% | 98.5% | +2.0pt | 70.8%→87.5% | 100.0%→100.0% | 0→0 | 7→3 |
| 2.1 line_items_present | 134→118 | 89.5% | 91.5% | +2.0pt | 12.5%→37.5% | 100.0%→100.0% | 0→0 | 14→10 |
| 2.2 po_validation | 94→93 | 35.1% | 73.1% | +38.0pt | 80.8%→92.3% | 27.3%→51.1% | 56→23 | 5→2 |
| 3.1 duplicate_detection | 34→74 | 70.6% | 96.0% | +25.4pt | 55.6%→83.3% | 83.3%→100.0% | 2→0 | 8→3 |
| 3.2 tax_id_validation | 19→62 | 5.3% | 96.8% | +91.5pt | 16.7%→83.3% | 7.1%→83.3% | 13→1 | 5→1 |
| 3.3 future_date_check | 19→60 | 73.7% | 98.3% | +24.6pt | 0.0%→80.0% | -→100.0% | 0→0 | 5→1 |
| 4.1 total_verification | 0→43 | - | 97.7% | - | -→100.0% | -→66.7% | 0→1 | 0→0 |
| 4.2 line_sum_validation | 5→41 | 0.0% | 41.5% | +41.5pt | 0.0%→40.0% | -→8.7% | 0→21 | 5→3 |
| 4.3 authorization_hours_check | 7→19 | 0.0% | 89.5% | +89.5pt | 0.0%→71.4% | -→100.0% | 0→0 | 7→2 |
| 4.4 tax_rate_verification | 1→39 | 0.0% | 69.2% | +69.2pt | 0.0%→0.0% | -→0.0% | 0→11 | 1→1 |

A check whose `n` moved was measured on a different number of documents, usually because an earlier check stopped rejecting everything. Read the accuracy change together with `n`.

## Decisions

| Measure | Before | After | Change |
|---|---|---|---|
| touchless rate | 0.0% | 4.7% | +4.7pt |
| correct touchless rate | 0.0% | 3.7% | +3.7pt |
| touchless accuracy | - | 78.6% | - |
| decision accuracy | 85.0% | 87.7% | +2.7pt |
| correctly auto-approved | 0 | 11 | +11 |
| incorrectly auto-approved | 0 | 3 | +3 |
| correctly rejected | 255 | 252 | -3 |
| incorrectly rejected | 45 | 34 | -11 |
| rejection reason exact | 120 | 207 | +87 |
| rejection reason another real fault | 33 | 29 | -4 |
| rejection reason wrong | 102 | 16 | -86 |
| errors / no decision | 0 | 0 | +0 |

## Extraction

| Group | Before | After | Change |
|---|---|---|---|
| header | 92.0% | 94.7% | +2.7pt |
| totals | 90.6% | 90.8% | +0.2pt |
| lines | 89.5% | 87.9% | -1.6pt |
| tax | 96.1% | 94.4% | -1.7pt |
| documents scored | 188 | 222 | +34 |

## Cost and time

| Measure | Before | After |
|---|---|---|
| processing seconds, median | 44.025 | 50.3 |
| LLM calls, mean | 1.963 | 2.238 |
| cost USD, total | 2.116 | 3.529 |
| errors | 0 | 0 |

## Flips

864 of 4200 scored rows changed verdict: 463 newly-scored, 226 fixed, 89 newly-failing, 71 no-longer-scored, 15 regressed. Every one is listed with its before and after evidence in `flips.csv`.

### Regressions

| Test | Check | Expected | Before | After |
|---|---|---|---|---|
| SVC-INV-025 | 1.1 | PASS | PASS | FAIL |
| MFG-INV-002 | 1.2 | FAIL | FAIL | PASS |
| TECH-INV-024 | 1.2 | FAIL | FAIL | PASS |
| SVC-INV-025 | 1.3 | PASS | PASS | FAIL |
| TECH-INV-087 | 1.3 | FAIL | FAIL | PASS |
| MFG-INV-006 | 2.2 | PASS | PASS | FAIL |
| SVC-INV-008 | 2.2 | PASS | PASS | FAIL |
| SVC-INV-038 | 2.2 | PASS | PASS | FAIL |
| SVC-INV-063 | 2.2 | PASS | PASS | FAIL |
| SVC-INV-072 | 2.2 | PASS | PASS | FAIL |
| TECH-INV-016 | 2.2 | PASS | PASS | FAIL |
| TECH-INV-039 | 2.2 | PASS | PASS | FAIL |
| TECH-INV-047 | 2.2 | PASS | PASS | FAIL |
| SVC-INV-072 | 3.1 | FAIL | FAIL | NOT_RUN |
| SVC-INV-008 | 3.2 | FAIL | FAIL | NOT_RUN |
