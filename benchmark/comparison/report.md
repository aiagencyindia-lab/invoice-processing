# BEFORE (`baseline`) vs AFTER (`after`)

`baseline` at `dcf0ba3`, 300 submissions; `after` at `d3d07fc`, 300 submissions. Same documents, same order, same ground truth, same scoring code.

## Per check (cascade-aware, full population)

| Check | n before→after | Accuracy before | after | change | Recall b→a | Precision b→a | FP b→a | FN b→a |
|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 270→273 | 82.2% | 100.0% | +17.8pt | 68.0%→100.0% | 51.5%→100.0% | 32→0 | 16→0 |
| 1.2 customer_verification | 223→226 | 86.1% | 96.9% | +10.8pt | 89.4%→85.1% | 61.8%→100.0% | 26→0 | 5→7 |
| 1.3 tax_compliance | 220→223 | 81.4% | 98.7% | +17.3pt | 0.0%→66.7% | 0.0%→100.0% | 32→0 | 9→3 |
| 1.4 work_authorization | 41→130 | 0.0% | 92.3% | +92.3pt | 0.0%→75.6% | -→100.0% | 0→0 | 41→10 |
| 1.5 single_document | 203→206 | 96.5% | 98.5% | +2.0pt | 70.8%→87.5% | 100.0%→100.0% | 0→0 | 7→3 |
| 2.1 line_items_present | 134→118 | 89.5% | 91.5% | +2.0pt | 12.5%→37.5% | 100.0%→100.0% | 0→0 | 14→10 |
| 2.2 po_validation | 94→94 | 35.1% | 95.7% | +60.6pt | 80.8%→92.3% | 27.3%→92.3% | 56→2 | 5→2 |
| 3.1 duplicate_detection | 34→92 | 70.6% | 100.0% | +29.4pt | 55.6%→100.0% | 83.3%→100.0% | 2→0 | 8→0 |
| 3.2 tax_id_validation | 19→81 | 5.3% | 93.8% | +88.6pt | 16.7%→83.3% | 7.1%→55.6% | 13→4 | 5→1 |
| 3.3 future_date_check | 19→78 | 73.7% | 100.0% | +26.3pt | 0.0%→100.0% | -→100.0% | 0→0 | 5→0 |
| 4.1 total_verification | 0→56 | - | 100.0% | - | -→100.0% | -→100.0% | 0→0 | 0→0 |
| 4.2 line_sum_validation | 5→54 | 0.0% | 94.4% | +94.4pt | 0.0%→40.0% | -→100.0% | 0→0 | 5→3 |
| 4.3 authorization_hours_check | 7→31 | 0.0% | 96.8% | +96.8pt | 0.0%→85.7% | -→100.0% | 0→0 | 7→1 |
| 4.4 tax_rate_verification | 1→51 | 0.0% | 96.1% | +96.1pt | 0.0%→0.0% | -→0.0% | 0→1 | 1→1 |

A check whose `n` moved was measured on a different number of documents, usually because an earlier check stopped rejecting everything. Read the accuracy change together with `n`.

## Decisions

| Measure | Before | After | Change |
|---|---|---|---|
| touchless rate | 0.0% | 15.3% | +15.3pt |
| correct touchless rate | 0.0% | 14.0% | +14.0pt |
| touchless accuracy | - | 91.3% | - |
| decision accuracy | 85.0% | 97.7% | +12.7pt |
| correctly auto-approved | 0 | 42 | +42 |
| incorrectly auto-approved | 0 | 4 | +4 |
| correctly rejected | 255 | 251 | -4 |
| incorrectly rejected | 45 | 3 | -42 |
| rejection reason exact | 120 | 214 | +94 |
| rejection reason another real fault | 33 | 30 | -3 |
| rejection reason wrong | 102 | 7 | -95 |
| errors / no decision | 0 | 0 | +0 |

## Extraction

| Group | Before | After | Change |
|---|---|---|---|
| header | 92.0% | 94.5% | +2.5pt |
| totals | 90.6% | 90.4% | -0.2pt |
| lines | 89.5% | 88.0% | -1.5pt |
| tax | 96.1% | 94.6% | -1.4pt |
| documents scored | 188 | 223 | +35 |

## Cost and time

| Measure | Before | After |
|---|---|---|
| processing seconds, median | 44.025 | 50.76 |
| LLM calls, mean | 1.963 | 2.234 |
| cost USD, total | 2.116 | 3.756 |
| errors | 0 | 0 |

## Flips

937 of 4200 scored rows changed verdict: 564 newly-scored, 249 fixed, 63 no-longer-scored, 58 newly-failing, 3 regressed. Every one is listed with its before and after evidence in `flips.csv`.

### Regressions

| Test | Check | Expected | Before | After |
|---|---|---|---|---|
| MFG-INV-002 | 1.2 | FAIL | FAIL | PASS |
| TECH-INV-024 | 1.2 | FAIL | FAIL | PASS |
| TECH-INV-087 | 1.3 | FAIL | FAIL | PASS |
