# BEFORE (`baseline`) vs AFTER (`prod300`)

`baseline` at `dcf0ba3`, 300 submissions; `prod300` at `e538061`, 300 submissions. Same documents, same order, same ground truth, same scoring code.

## Per check (cascade-aware, full population)

| Check | n before→after | Accuracy before | after | change | Recall b→a | Precision b→a | FP b→a | FN b→a |
|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 270→273 | 82.2% | 100.0% | +17.8pt | 68.0%→100.0% | 51.5%→100.0% | 32→0 | 16→0 |
| 1.2 customer_verification | 223→226 | 86.1% | 96.5% | +10.4pt | 89.4%→83.0% | 61.8%→100.0% | 26→0 | 5→8 |
| 1.3 tax_compliance | 220→223 | 81.4% | 98.7% | +17.3pt | 0.0%→66.7% | 0.0%→100.0% | 32→0 | 9→3 |
| 1.4 work_authorization | 41→130 | 0.0% | 93.1% | +93.1pt | 0.0%→78.0% | -→100.0% | 0→0 | 41→9 |
| 1.5 single_document | 203→206 | 96.5% | 98.5% | +2.0pt | 70.8%→87.5% | 100.0%→100.0% | 0→0 | 7→3 |
| 2.1 line_items_present | 134→118 | 89.5% | 91.5% | +2.0pt | 12.5%→37.5% | 100.0%→100.0% | 0→0 | 14→10 |
| 2.2 po_validation | 94→93 | 35.1% | 94.6% | +59.5pt | 80.8%→92.3% | 27.3%→88.9% | 56→3 | 5→2 |
| 3.1 duplicate_detection | 34→92 | 70.6% | 100.0% | +29.4pt | 55.6%→100.0% | 83.3%→100.0% | 2→0 | 8→0 |
| 3.2 tax_id_validation | 19→81 | 5.3% | 92.6% | +87.3pt | 16.7%→83.3% | 7.1%→50.0% | 13→5 | 5→1 |
| 3.3 future_date_check | 19→78 | 73.7% | 100.0% | +26.3pt | 0.0%→100.0% | -→100.0% | 0→0 | 5→0 |
| 4.1 total_verification | 0→56 | - | 98.2% | - | -→100.0% | -→75.0% | 0→1 | 0→0 |
| 4.2 line_sum_validation | 5→55 | 0.0% | 92.7% | +92.7pt | 0.0%→40.0% | -→66.7% | 0→1 | 5→3 |
| 4.3 authorization_hours_check | 7→31 | 0.0% | 100.0% | +100.0pt | 0.0%→100.0% | -→100.0% | 0→0 | 7→0 |
| 4.4 tax_rate_verification | 1→50 | 0.0% | 96.0% | +96.0pt | 0.0%→0.0% | -→0.0% | 0→1 | 1→1 |

A check whose `n` moved was measured on a different number of documents, usually because an earlier check stopped rejecting everything. Read the accuracy change together with `n`.

## Decisions

| Measure | Before | After | Change |
|---|---|---|---|
| touchless rate | 0.0% | 14.7% | +14.7pt |
| correct touchless rate | 0.0% | 13.7% | +13.7pt |
| touchless accuracy | - | 93.2% | - |
| decision accuracy | 85.0% | 97.7% | +12.7pt |
| correctly auto-approved | 0 | 41 | +41 |
| incorrectly auto-approved | 0 | 3 | +3 |
| correctly rejected | 255 | 252 | -3 |
| incorrectly rejected | 45 | 4 | -41 |
| rejection reason exact | 120 | 214 | +94 |
| rejection reason another real fault | 33 | 30 | -3 |
| rejection reason wrong | 102 | 8 | -94 |
| errors / no decision | 0 | 0 | +0 |

## Extraction

| Group | Before | After | Change |
|---|---|---|---|
| header | 92.0% | 94.3% | +2.3pt |
| totals | 90.6% | 91.4% | +0.8pt |
| lines | 89.5% | 88.2% | -1.3pt |
| tax | 96.1% | 94.8% | -1.3pt |
| documents scored | 188 | 223 | +35 |

## Cost and time

| Measure | Before | After |
|---|---|---|
| processing seconds, median | 44.025 | 78.86 |
| LLM calls, mean | 1.963 | 2.231 |
| cost USD, total | 2.116 | 3.74 |
| errors | 0 | 0 |

## Flips

938 of 4200 scored rows changed verdict: 558 newly-scored, 251 fixed, 63 newly-failing, 63 no-longer-scored, 3 regressed. Every one is listed with its before and after evidence in `flips.csv`.

### Regressions

| Test | Check | Expected | Before | After |
|---|---|---|---|---|
| MFG-INV-002 | 1.2 | FAIL | FAIL | PASS |
| MFG-INV-021 | 1.2 | FAIL | FAIL | PASS |
| TECH-INV-024 | 1.2 | FAIL | FAIL | PASS |
