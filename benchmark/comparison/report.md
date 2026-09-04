# BEFORE (`baseline`) vs AFTER (`after`)

`baseline` at `dcf0ba3`, 300 submissions; `after` at `2521ff9`, 300 submissions. Same documents, same order, same ground truth, same scoring code.

## Per check (cascade-aware, full population)

| Check | n before→after | Accuracy before | after | change | Recall b→a | Precision b→a | FP b→a | FN b→a |
|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 270→273 | 82.2% | 99.6% | +17.4pt | 68.0%→98.0% | 51.5%→100.0% | 32→0 | 16→1 |
| 1.2 customer_verification | 223→226 | 86.1% | 94.7% | +8.6pt | 89.4%→85.1% | 61.8%→88.9% | 26→5 | 5→7 |
| 1.3 tax_compliance | 220→223 | 81.4% | 92.8% | +11.5pt | 0.0%→66.7% | 0.0%→31.6% | 32→13 | 9→3 |
| 1.4 work_authorization | 41→130 | 0.0% | 93.1% | +93.1pt | 0.0%→78.0% | -→100.0% | 0→0 | 41→9 |
| 1.5 single_document | 203→206 | 96.5% | 98.5% | +2.0pt | 70.8%→87.5% | 100.0%→100.0% | 0→0 | 7→3 |
| 2.1 line_items_present | 134→115 | 89.5% | 86.1% | -3.5pt | 12.5%→0.0% | 100.0%→- | 0→0 | 14→16 |
| 2.2 po_validation | 94→86 | 35.1% | 69.8% | +34.7pt | 80.8%→88.5% | 27.3%→50.0% | 56→23 | 5→3 |
| 3.1 duplicate_detection | 34→73 | 70.6% | 95.9% | +25.3pt | 55.6%→83.3% | 83.3%→100.0% | 2→0 | 8→3 |
| 3.2 tax_id_validation | 19→62 | 5.3% | 93.5% | +88.3pt | 16.7%→66.7% | 7.1%→66.7% | 13→2 | 5→2 |
| 3.3 future_date_check | 19→59 | 73.7% | 98.3% | +24.6pt | 0.0%→80.0% | -→100.0% | 0→0 | 5→1 |
| 4.1 total_verification | 0→42 | - | 100.0% | - | -→100.0% | -→100.0% | 0→0 | 0→0 |
| 4.2 line_sum_validation | 5→40 | 0.0% | 47.5% | +47.5pt | 0.0%→60.0% | -→13.6% | 0→19 | 5→2 |
| 4.3 authorization_hours_check | 7→19 | 0.0% | 89.5% | +89.5pt | 0.0%→71.4% | -→100.0% | 0→0 | 7→2 |
| 4.4 tax_rate_verification | 1→38 | 0.0% | 68.4% | +68.4pt | 0.0%→0.0% | -→0.0% | 0→11 | 1→1 |

A check whose `n` moved was measured on a different number of documents, usually because an earlier check stopped rejecting everything. Read the accuracy change together with `n`.

## Decisions

| Measure | Before | After | Change |
|---|---|---|---|
| touchless rate | 0.0% | 4.7% | +4.7pt |
| correct touchless rate | 0.0% | 4.0% | +4.0pt |
| touchless accuracy | - | 85.7% | - |
| decision accuracy | 85.0% | 88.3% | +3.3pt |
| correctly auto-approved | 0 | 12 | +12 |
| incorrectly auto-approved | 0 | 2 | +2 |
| correctly rejected | 255 | 253 | -2 |
| incorrectly rejected | 45 | 33 | -12 |
| rejection reason exact | 120 | 194 | +74 |
| rejection reason another real fault | 33 | 29 | -4 |
| rejection reason wrong | 102 | 30 | -72 |
| errors / no decision | 0 | 0 | +0 |

## Extraction

| Group | Before | After | Change |
|---|---|---|---|
| header | 92.0% | 94.6% | +2.6pt |
| totals | 90.6% | 90.6% | -0.0pt |
| lines | 89.5% | 87.6% | -1.9pt |
| tax | 96.1% | 94.6% | -1.4pt |
| documents scored | 188 | 223 | +35 |

## Cost and time

| Measure | Before | After |
|---|---|---|
| processing seconds, median | 44.025 | 50.96 |
| LLM calls, mean | 1.963 | 2.245 |
| cost USD, total | 2.116 | 3.565 |
| errors | 0 | 0 |

## Flips

851 of 4200 scored rows changed verdict: 458 newly-scored, 210 fixed, 85 newly-failing, 74 no-longer-scored, 24 regressed. Every one is listed with its before and after evidence in `flips.csv`.

### Regressions

| Test | Check | Expected | Before | After |
|---|---|---|---|---|
| MFG-INV-002 | 1.2 | FAIL | FAIL | PASS |
| MFG-INV-033 | 1.2 | PASS | PASS | FAIL |
| MFG-INV-057 | 1.2 | PASS | PASS | FAIL |
| MFG-INV-078 | 1.2 | PASS | PASS | FAIL |
| TECH-INV-006 | 1.2 | PASS | PASS | FAIL |
| TECH-INV-024 | 1.2 | FAIL | FAIL | PASS |
| MFG-INV-008 | 1.3 | PASS | PASS | FAIL |
| MFG-INV-054 | 1.3 | PASS | PASS | FAIL |
| SVC-INV-001 | 1.3 | PASS | PASS | FAIL |
| SVC-INV-051 | 1.3 | PASS | PASS | FAIL |
| SVC-INV-070 | 1.3 | PASS | PASS | FAIL |
| TECH-INV-087 | 1.3 | FAIL | FAIL | PASS |
| MFG-INV-054 | 2.1 | FAIL | FAIL | NOT_RUN |
| SVC-INV-051 | 2.1 | FAIL | FAIL | NOT_RUN |
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
