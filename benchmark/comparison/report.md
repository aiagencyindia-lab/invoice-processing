# BEFORE (`baseline`) vs AFTER (`after`)

`baseline` at `dcf0ba3`, 300 submissions; `after` at `18843b3`, 300 submissions. Same documents, same order, same ground truth, same scoring code.

## Per check (cascade-aware, full population)

| Check | n before→after | Accuracy before | after | change | Recall b→a | Precision b→a | FP b→a | FN b→a |
|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 270→273 | 82.2% | 99.3% | +17.0pt | 68.0%→100.0% | 51.5%→96.2% | 32→2 | 16→0 |
| 1.2 customer_verification | 223→226 | 86.1% | 96.5% | +10.4pt | 89.4%→87.2% | 61.8%→95.3% | 26→2 | 5→6 |
| 1.3 tax_compliance | 220→223 | 81.4% | 97.3% | +15.9pt | 0.0%→66.7% | 0.0%→66.7% | 32→3 | 9→3 |
| 1.4 work_authorization | 41→130 | 0.0% | 92.3% | +92.3pt | 0.0%→78.0% | -→97.0% | 0→1 | 41→9 |
| 1.5 single_document | 203→206 | 96.5% | 98.5% | +2.0pt | 70.8%→87.5% | 100.0%→100.0% | 0→0 | 7→3 |
| 2.1 line_items_present | 134→117 | 89.5% | 91.5% | +1.9pt | 12.5%→37.5% | 100.0%→100.0% | 0→0 | 14→10 |
| 2.2 po_validation | 94→92 | 35.1% | 95.7% | +60.5pt | 80.8%→92.3% | 27.3%→92.3% | 56→2 | 5→2 |
| 3.1 duplicate_detection | 34→92 | 70.6% | 100.0% | +29.4pt | 55.6%→100.0% | 83.3%→100.0% | 2→0 | 8→0 |
| 3.2 tax_id_validation | 19→80 | 5.3% | 96.2% | +91.0pt | 16.7%→100.0% | 7.1%→66.7% | 13→3 | 5→0 |
| 3.3 future_date_check | 19→78 | 73.7% | 100.0% | +26.3pt | 0.0%→100.0% | -→100.0% | 0→0 | 5→0 |
| 4.1 total_verification | 0→56 | - | 100.0% | - | -→100.0% | -→100.0% | 0→0 | 0→0 |
| 4.2 line_sum_validation | 5→54 | 0.0% | 59.3% | +59.3pt | 0.0%→60.0% | -→13.0% | 0→20 | 5→2 |
| 4.3 authorization_hours_check | 7→31 | 0.0% | 96.8% | +96.8pt | 0.0%→85.7% | -→100.0% | 0→0 | 7→1 |
| 4.4 tax_rate_verification | 1→50 | 0.0% | 76.0% | +76.0pt | 0.0%→100.0% | -→7.7% | 0→12 | 1→0 |

A check whose `n` moved was measured on a different number of documents, usually because an earlier check stopped rejecting everything. Read the accuracy change together with `n`.

## Decisions

| Measure | Before | After | Change |
|---|---|---|---|
| touchless rate | 0.0% | 8.7% | +8.7pt |
| correct touchless rate | 0.0% | 7.7% | +7.7pt |
| touchless accuracy | - | 88.5% | - |
| decision accuracy | 85.0% | 91.7% | +6.7pt |
| correctly auto-approved | 0 | 23 | +23 |
| incorrectly auto-approved | 0 | 3 | +3 |
| correctly rejected | 255 | 252 | -3 |
| incorrectly rejected | 45 | 22 | -23 |
| rejection reason exact | 120 | 216 | +96 |
| rejection reason another real fault | 33 | 28 | -5 |
| rejection reason wrong | 102 | 8 | -94 |
| errors / no decision | 0 | 0 | +0 |

## Extraction

| Group | Before | After | Change |
|---|---|---|---|
| header | 92.0% | 95.4% | +3.4pt |
| totals | 90.6% | 91.5% | +0.9pt |
| lines | 89.5% | 88.3% | -1.2pt |
| tax | 96.1% | 95.9% | -0.2pt |
| documents scored | 188 | 221 | +33 |

## Cost and time

| Measure | Before | After |
|---|---|---|
| processing seconds, median | 44.025 | 51.44 |
| LLM calls, mean | 1.963 | 2.234 |
| cost USD, total | 2.116 | 3.458 |
| errors | 0 | 0 |

## Flips

945 of 4200 scored rows changed verdict: 530 newly-scored, 251 fixed, 90 newly-failing, 66 no-longer-scored, 8 regressed. Every one is listed with its before and after evidence in `flips.csv`.

### Regressions

| Test | Check | Expected | Before | After |
|---|---|---|---|---|
| MFG-INV-040 | 1.1 | PASS | PASS | FAIL |
| SVC-INV-042 | 1.1 | PASS | PASS | FAIL |
| MFG-INV-040 | 1.2 | PASS | PASS | FAIL |
| SVC-INV-042 | 1.2 | PASS | PASS | FAIL |
| TECH-INV-024 | 1.2 | FAIL | FAIL | PASS |
| MFG-INV-040 | 1.3 | PASS | PASS | FAIL |
| SVC-INV-042 | 1.3 | PASS | PASS | FAIL |
| TECH-INV-087 | 1.3 | FAIL | FAIL | PASS |
