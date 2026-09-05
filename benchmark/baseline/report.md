# Benchmark run `baseline`

Generated 2026-09-04T19:34:44Z at `dcf0ba3` with profile `benchmark/harness/profiles/baseline.json`; 300 of 300 submissions recorded.

## Decisions

| Measure | Value |
|---|---|
| total | 300 |
| expected_ACCEPT | 45 |
| expected_REJECT | 255 |
| auto_approved | 0 |
| correctly_auto_approved | 0 |
| incorrectly_auto_approved | 0 |
| rejected | 300 |
| correctly_rejected | 255 |
| incorrectly_rejected | 45 |
| errors_or_no_decision | 0 |
| reason_strict | 120 |
| reason_family | 33 |
| reason_wrong | 102 |
| touchless rate (ACCEPT / 300) | 0.0% |
| correct touchless rate (correct ACCEPT / 300) | 0.0% |
| touchless accuracy (correct ACCEPT / ACCEPT) | - |
| decision accuracy | 85.0% |
| accepted but routed to review | 0 |

Reason match is scored on correctly rejected invoices: `strict` = the app's reason is the dataset's first-failing reason; `family` = it names another fault the dataset planted on that invoice; `wrong` = neither.

## Per check

Positive class = FAIL (for 4.1, DISCREPANCY). `cascade` follows the dataset README: an expected FAIL counts only where it is the first failing check (a NOT_RUN there is a false negative); an expected PASS counts only where the check was reached. `all findings` scores every check the pipeline actually reached. `balanced` restricts to the dataset's balanced population.

### Cascade-aware, full population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 270 | 34 | 188 | 32 | 16 | 82.2% | 51.5% | 68.0% | 14.5% | 32.0% | 0 | 0 |
| 1.2 customer_verification | 223 | 42 | 150 | 26 | 5 | 86.1% | 61.8% | 89.4% | 14.8% | 10.6% | 3 | 35 |
| 1.3 tax_compliance | 220 | 0 | 179 | 32 | 9 | 81.4% | 0.0% | 0.0% | 15.2% | 100.0% | 1 | 8 |
| 1.4 work_authorization | 41 | 0 | 0 | 0 | 41 | 0.0% | - | 0.0% | - | 100.0% | 41 | 0 |
| 1.5 single_document | 203 | 17 | 179 | 0 | 7 | 96.5% | 100.0% | 70.8% | 0.0% | 29.2% | 3 | 0 |
| 2.1 line_items_present | 134 | 2 | 118 | 0 | 14 | 89.5% | 100.0% | 12.5% | 0.0% | 87.5% | 7 | 0 |
| 2.2 po_validation | 94 | 21 | 12 | 56 | 5 | 35.1% | 27.3% | 80.8% | 82.3% | 19.2% | 5 | 41 |
| 3.1 duplicate_detection | 34 | 10 | 14 | 2 | 8 | 70.6% | 83.3% | 55.6% | 12.5% | 44.4% | 8 | 0 |
| 3.2 tax_id_validation | 19 | 1 | 0 | 13 | 5 | 5.3% | 7.1% | 16.7% | 100.0% | 83.3% | 5 | 7 |
| 3.3 future_date_check | 19 | 0 | 14 | 0 | 5 | 73.7% | - | 0.0% | 0.0% | 100.0% | 5 | 0 |
| 4.1 total_verification | 0 | 0 | 0 | 0 | 0 | - | - | - | - | - | 0 | 0 |
| 4.2 line_sum_validation | 5 | 0 | 0 | 0 | 5 | 0.0% | - | 0.0% | - | 100.0% | 5 | 0 |
| 4.3 authorization_hours_check | 7 | 0 | 0 | 0 | 7 | 0.0% | - | 0.0% | - | 100.0% | 7 | 0 |
| 4.4 tax_rate_verification | 1 | 0 | 0 | 0 | 1 | 0.0% | - | 0.0% | - | 100.0% | 1 | 0 |

### Cascade-aware, balanced population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 96 | 34 | 43 | 3 | 16 | 80.2% | 91.9% | 68.0% | 6.5% | 32.0% | 0 | 0 |
| 1.2 customer_verification | 84 | 42 | 32 | 5 | 5 | 88.1% | 89.4% | 89.4% | 13.5% | 10.6% | 3 | 0 |
| 1.3 tax_compliance | 52 | 0 | 35 | 8 | 9 | 67.3% | 0.0% | 0.0% | 18.6% | 100.0% | 1 | 0 |
| 1.4 work_authorization | 41 | 0 | 0 | 0 | 41 | 0.0% | - | 0.0% | - | 100.0% | 41 | 0 |
| 1.5 single_document | 62 | 17 | 38 | 0 | 7 | 88.7% | 100.0% | 70.8% | 0.0% | 29.2% | 3 | 0 |
| 2.1 line_items_present | 44 | 2 | 28 | 0 | 14 | 68.2% | 100.0% | 12.5% | 0.0% | 87.5% | 7 | 0 |
| 2.2 po_validation | 55 | 21 | 6 | 23 | 5 | 49.1% | 47.7% | 80.8% | 79.3% | 19.2% | 5 | 0 |
| 3.1 duplicate_detection | 21 | 10 | 3 | 0 | 8 | 61.9% | 100.0% | 55.6% | 0.0% | 44.4% | 8 | 0 |
| 3.2 tax_id_validation | 10 | 1 | 0 | 4 | 5 | 10.0% | 20.0% | 16.7% | 100.0% | 83.3% | 5 | 0 |
| 3.3 future_date_check | 10 | 0 | 5 | 0 | 5 | 50.0% | - | 0.0% | 0.0% | 100.0% | 5 | 0 |
| 4.1 total_verification | 0 | 0 | 0 | 0 | 0 | - | - | - | - | - | 0 | 0 |
| 4.2 line_sum_validation | 5 | 0 | 0 | 0 | 5 | 0.0% | - | 0.0% | - | 100.0% | 5 | 0 |
| 4.3 authorization_hours_check | 6 | 0 | 0 | 0 | 6 | 0.0% | - | 0.0% | - | 100.0% | 6 | 0 |
| 4.4 tax_rate_verification | 1 | 0 | 0 | 0 | 1 | 0.0% | - | 0.0% | - | 100.0% | 1 | 0 |

### All findings, full population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 270 | 34 | 188 | 32 | 16 | 82.2% | 51.5% | 68.0% | 14.5% | 32.0% | 0 | 0 |
| 1.2 customer_verification | 220 | 42 | 150 | 26 | 2 | 87.3% | 61.8% | 95.5% | 14.8% | 4.5% | 0 | 35 |
| 1.3 tax_compliance | 262 | 28 | 179 | 32 | 23 | 79.0% | 46.7% | 54.9% | 15.2% | 45.1% | 0 | 8 |
| 1.4 work_authorization | 0 | 0 | 0 | 0 | 0 | - | - | - | - | - | 0 | 0 |
| 1.5 single_document | 220 | 31 | 179 | 0 | 10 | 95.5% | 100.0% | 75.6% | 0.0% | 24.4% | 0 | 0 |
| 2.1 line_items_present | 132 | 4 | 118 | 0 | 10 | 92.4% | 100.0% | 28.6% | 0.0% | 71.4% | 0 | 0 |
| 2.2 po_validation | 95 | 27 | 12 | 56 | 0 | 41.0% | 32.5% | 100.0% | 82.3% | 0.0% | 0 | 41 |
| 3.1 duplicate_detection | 46 | 30 | 14 | 2 | 0 | 95.7% | 93.8% | 100.0% | 12.5% | 0.0% | 0 | 0 |
| 3.2 tax_id_validation | 16 | 3 | 0 | 13 | 0 | 18.8% | 18.8% | 100.0% | 100.0% | 0.0% | 0 | 7 |
| 3.3 future_date_check | 16 | 2 | 14 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.1 total_verification | 0 | 0 | 0 | 0 | 0 | - | - | - | - | - | 0 | 0 |
| 4.2 line_sum_validation | 0 | 0 | 0 | 0 | 0 | - | - | - | - | - | 0 | 0 |
| 4.3 authorization_hours_check | 0 | 0 | 0 | 0 | 0 | - | - | - | - | - | 0 | 0 |
| 4.4 tax_rate_verification | 0 | 0 | 0 | 0 | 0 | - | - | - | - | - | 0 | 0 |

## First failing check: expected → actual

| expected → actual | n |
|---|---|
| none->2.2 | 37 |
| 1.2->1.2 | 36 |
| 1.1->1.1 | 34 |
| 2.2->2.2 | 21 |
| 1.4->2.2 | 16 |
| 1.5->1.5 | 16 |
| 3.1->3.1 | 10 |
| 1.4->1.1 | 9 |
| 1.4->3.1 | 9 |
| 1.1->2.2 | 8 |
| 1.1->3.2 | 7 |
| 2.1->2.2 | 7 |
| 3.1->2.2 | 7 |
| 1.2->1.1 | 6 |
| 2.1->1.1 | 6 |
| 4.3->2.2 | 6 |
| none->3.2 | 6 |
| 1.4->3.2 | 5 |
| 1.5->1.1 | 5 |
| 3.2->2.2 | 5 |
| 4.2->2.2 | 5 |
| 3.3->2.2 | 4 |
| 1.2->3.1 | 3 |
| 1.3->2.2 | 3 |
| 1.5->3.1 | 3 |
| 2.2->1.1 | 3 |
| 1.2->1.5 | 2 |
| 1.3->1.5 | 2 |
| 1.3->3.1 | 2 |
| 1.3->3.2 | 2 |
| 1.4->2.1 | 2 |
| 2.1->2.1 | 2 |
| 2.2->3.1 | 2 |
| 1.1->1.2 | 1 |
| 2.1->3.1 | 1 |
| 3.1->1.1 | 1 |
| 3.2->3.2 | 1 |
| 3.3->1.1 | 1 |
| 4.3->3.1 | 1 |
| 4.4->2.2 | 1 |
| none->1.1 | 1 |
| none->3.1 | 1 |

## Extraction (vs as-submitted values)

Scored on 188 extracted invoice documents (32 not extracted; non-invoice documents and ingest-level duplicates excluded).

| Group | Accuracy |
|---|---|
| header | 92.0% |
| totals | 90.6% |
| lines | 89.5% |
| tax | 96.1% |
| all header+totals fields correct | 94 docs |

| Field | matched | mismatched | missing | rate |
|---|---|---|---|---|
| customer_name | 186 | 2 | 0 | 98.9% |
| due_date | 184 | 4 | 0 | 97.9% |
| invoice_date | 184 | 4 | 0 | 97.9% |
| invoice_number | 175 | 13 | 0 | 93.1% |
| invoice_total_ex_tax | 142 | 41 | 5 | 77.6% |
| invoice_total_inc_tax | 183 | 5 | 0 | 97.3% |
| line.amount_ex_tax | 595 | 3 | 0 | 99.5% |
| line.description | 595 | 3 | 0 | 99.5% |
| line.quantity | 595 | 3 | 0 | 99.5% |
| line.unit_price | 594 | 3 | 1 | 99.5% |
| line_count | 137 | 51 | 0 | 72.9% |
| po_number | 138 | 50 | 0 | 73.4% |
| tax_amount | 171 | 7 | 10 | 96.1% |
| vendor_tax_id | 171 | 17 | 0 | 91.0% |

## Timing and cost

| Group | docs | processing s (mean/median/p95) | wall s mean | LLM calls mean | cost USD sum | errors |
|---|---|---|---|---|---|---|
| all | 300 | 44.92/44.025/84.9 | 41.855 | 1.963 | 2.116 | 0 |
| A | 100 | 39.03/41.58/70.66 | 36.574 | 2.044 | 0.679 | 0 |
| B | 100 | 49.918/45.45/92.19 | 46.364 | 1.833 | 0.772 | 0 |
| C | 100 | 45.814/46.625/83.31 | 42.629 | 2.011 | 0.665 | 0 |

Not measured: OCR cost (folded into Gemini cost), infrastructure cost, human review time.
