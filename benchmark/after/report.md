# Benchmark run `after`

Generated 2026-09-05T08:07:36Z at `6db9408` with profile `benchmark/harness/profiles/after.json`; 300 of 300 submissions recorded.

## Decisions

| Measure | Value |
|---|---|
| total | 300 |
| expected_ACCEPT | 45 |
| expected_REJECT | 255 |
| auto_approved | 14 |
| correctly_auto_approved | 11 |
| incorrectly_auto_approved | 3 |
| rejected | 286 |
| correctly_rejected | 252 |
| incorrectly_rejected | 34 |
| errors_or_no_decision | 0 |
| reason_strict | 207 |
| reason_family | 29 |
| reason_wrong | 16 |
| touchless rate (ACCEPT / 300) | 4.7% |
| correct touchless rate (correct ACCEPT / 300) | 3.7% |
| touchless accuracy (correct ACCEPT / ACCEPT) | 78.6% |
| decision accuracy | 87.7% |
| accepted but routed to review | 12 |

Reason match is scored on correctly rejected invoices: `strict` = the app's reason is the dataset's first-failing reason; `family` = it names another fault the dataset planted on that invoice; `wrong` = neither.

## Per check

Positive class = FAIL (for 4.1, DISCREPANCY). `cascade` follows the dataset README: an expected FAIL counts only where it is the first failing check (a NOT_RUN there is a false negative); an expected PASS counts only where the check was reached. `all findings` scores every check the pipeline actually reached. `balanced` restricts to the dataset's balanced population.

### Cascade-aware, full population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 273 | 49 | 222 | 1 | 1 | 99.3% | 98.0% | 98.0% | 0.4% | 2.0% | 0 | 0 |
| 1.2 customer_verification | 226 | 40 | 179 | 0 | 7 | 96.9% | 100.0% | 85.1% | 0.0% | 14.9% | 3 | 42 |
| 1.3 tax_compliance | 223 | 6 | 212 | 2 | 3 | 97.8% | 75.0% | 66.7% | 0.9% | 33.3% | 1 | 7 |
| 1.4 work_authorization | 130 | 32 | 89 | 0 | 9 | 93.1% | 100.0% | 78.0% | 0.0% | 21.9% | 8 | 44 |
| 1.5 single_document | 206 | 21 | 182 | 0 | 3 | 98.5% | 100.0% | 87.5% | 0.0% | 12.5% | 3 | 0 |
| 2.1 line_items_present | 118 | 6 | 102 | 0 | 10 | 91.5% | 100.0% | 37.5% | 0.0% | 62.5% | 1 | 0 |
| 2.2 po_validation | 93 | 24 | 44 | 23 | 2 | 73.1% | 51.1% | 92.3% | 34.3% | 7.7% | 2 | 1 |
| 3.1 duplicate_detection | 74 | 15 | 56 | 0 | 3 | 96.0% | 100.0% | 83.3% | 0.0% | 16.7% | 2 | 0 |
| 3.2 tax_id_validation | 62 | 5 | 55 | 1 | 1 | 96.8% | 83.3% | 83.3% | 1.8% | 16.7% | 1 | 0 |
| 3.3 future_date_check | 60 | 4 | 55 | 0 | 1 | 98.3% | 100.0% | 80.0% | 0.0% | 20.0% | 1 | 0 |
| 4.1 total_verification | 43 | 2 | 40 | 1 | 0 | 97.7% | 66.7% | 100.0% | 2.4% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 41 | 2 | 15 | 21 | 3 | 41.5% | 8.7% | 40.0% | 58.3% | 60.0% | 1 | 0 |
| 4.3 authorization_hours_check | 19 | 5 | 12 | 0 | 2 | 89.5% | 100.0% | 71.4% | 0.0% | 28.6% | 2 | 0 |
| 4.4 tax_rate_verification | 39 | 0 | 27 | 11 | 1 | 69.2% | 0.0% | 0.0% | 28.9% | 100.0% | 1 | 2 |

### Cascade-aware, balanced population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 96 | 49 | 46 | 0 | 1 | 99.0% | 100.0% | 98.0% | 0.0% | 2.0% | 0 | 0 |
| 1.2 customer_verification | 86 | 40 | 39 | 0 | 7 | 91.9% | 100.0% | 85.1% | 0.0% | 14.9% | 3 | 0 |
| 1.3 tax_compliance | 52 | 6 | 43 | 0 | 3 | 94.2% | 100.0% | 66.7% | 0.0% | 33.3% | 1 | 0 |
| 1.4 work_authorization | 82 | 32 | 41 | 0 | 9 | 89.0% | 100.0% | 78.0% | 0.0% | 21.9% | 8 | 0 |
| 1.5 single_document | 64 | 21 | 40 | 0 | 3 | 95.3% | 100.0% | 87.5% | 0.0% | 12.5% | 3 | 0 |
| 2.1 line_items_present | 40 | 6 | 24 | 0 | 10 | 75.0% | 100.0% | 37.5% | 0.0% | 62.5% | 1 | 0 |
| 2.2 po_validation | 51 | 24 | 13 | 12 | 2 | 72.5% | 66.7% | 92.3% | 48.0% | 7.7% | 2 | 0 |
| 3.1 duplicate_detection | 31 | 15 | 13 | 0 | 3 | 90.3% | 100.0% | 83.3% | 0.0% | 16.7% | 2 | 0 |
| 3.2 tax_id_validation | 22 | 5 | 15 | 1 | 1 | 90.9% | 83.3% | 83.3% | 6.2% | 16.7% | 1 | 0 |
| 3.3 future_date_check | 18 | 4 | 13 | 0 | 1 | 94.4% | 100.0% | 80.0% | 0.0% | 20.0% | 1 | 0 |
| 4.1 total_verification | 13 | 2 | 10 | 1 | 0 | 92.3% | 66.7% | 100.0% | 9.1% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 16 | 2 | 5 | 6 | 3 | 43.8% | 25.0% | 40.0% | 54.5% | 60.0% | 1 | 0 |
| 4.3 authorization_hours_check | 18 | 4 | 12 | 0 | 2 | 88.9% | 100.0% | 66.7% | 0.0% | 33.3% | 2 | 0 |
| 4.4 tax_rate_verification | 15 | 0 | 9 | 5 | 1 | 60.0% | 0.0% | 0.0% | 35.7% | 100.0% | 1 | 0 |

### All findings, full population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 273 | 49 | 222 | 1 | 1 | 99.3% | 98.0% | 98.0% | 0.4% | 2.0% | 0 | 0 |
| 1.2 customer_verification | 223 | 40 | 179 | 0 | 4 | 98.2% | 100.0% | 90.9% | 0.0% | 9.1% | 0 | 42 |
| 1.3 tax_compliance | 265 | 41 | 212 | 2 | 10 | 95.5% | 95.3% | 80.4% | 0.9% | 19.6% | 0 | 7 |
| 1.4 work_authorization | 129 | 38 | 89 | 0 | 2 | 98.5% | 100.0% | 95.0% | 0.0% | 5.0% | 0 | 44 |
| 1.5 single_document | 223 | 41 | 182 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 2.1 line_items_present | 117 | 6 | 102 | 0 | 9 | 92.3% | 100.0% | 40.0% | 0.0% | 60.0% | 0 | 0 |
| 2.2 po_validation | 91 | 24 | 44 | 23 | 0 | 74.7% | 51.1% | 100.0% | 34.3% | 0.0% | 0 | 1 |
| 3.1 duplicate_detection | 90 | 33 | 56 | 0 | 1 | 98.9% | 100.0% | 97.1% | 0.0% | 2.9% | 0 | 0 |
| 3.2 tax_id_validation | 63 | 7 | 55 | 1 | 0 | 98.4% | 87.5% | 100.0% | 1.8% | 0.0% | 0 | 0 |
| 3.3 future_date_check | 63 | 8 | 55 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.1 total_verification | 43 | 2 | 40 | 1 | 0 | 97.7% | 66.7% | 100.0% | 2.4% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 40 | 2 | 15 | 21 | 2 | 42.5% | 8.7% | 50.0% | 58.3% | 50.0% | 0 | 0 |
| 4.3 authorization_hours_check | 18 | 6 | 12 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.4 tax_rate_verification | 40 | 2 | 27 | 11 | 0 | 72.5% | 15.4% | 100.0% | 28.9% | 0.0% | 0 | 2 |

## First failing check: expected → actual

| expected → actual | n |
|---|---|
| 1.1->1.1 | 49 |
| 1.2->1.2 | 39 |
| 1.4->1.4 | 32 |
| 2.2->2.2 | 24 |
| 1.5->1.5 | 21 |
| none->4.2 | 20 |
| 3.1->3.1 | 15 |
| none->2.2 | 13 |
| none->none | 11 |
| 1.4->3.1 | 8 |
| 1.3->1.3 | 6 |
| 2.1->2.1 | 6 |
| 3.2->3.2 | 5 |
| 1.2->1.5 | 4 |
| 3.3->3.3 | 4 |
| 4.3->4.3 | 4 |
| 1.2->3.1 | 3 |
| 1.5->3.1 | 3 |
| 2.1->2.2 | 3 |
| 1.3->1.5 | 2 |
| 2.1->3.3 | 2 |
| 2.1->4.4 | 2 |
| 2.2->3.1 | 2 |
| 3.1->2.2 | 2 |
| 4.2->4.2 | 2 |
| 4.2->none | 2 |
| 4.3->2.2 | 2 |
| 1.1->1.2 | 1 |
| 1.2->1.1 | 1 |
| 1.3->3.1 | 1 |
| 1.4->1.5 | 1 |
| 2.1->3.1 | 1 |
| 2.1->3.2 | 1 |
| 2.1->none | 1 |
| 3.1->3.2 | 1 |
| 3.2->2.2 | 1 |
| 3.3->2.2 | 1 |
| 4.2->2.2 | 1 |
| 4.3->4.2 | 1 |
| 4.4->2.2 | 1 |
| none->3.2 | 1 |

## Extraction (vs as-submitted values)

Scored on 222 extracted invoice documents (1 not extracted; non-invoice documents and ingest-level duplicates excluded).

| Group | Accuracy |
|---|---|
| header | 94.7% |
| totals | 90.8% |
| lines | 87.9% |
| tax | 94.4% |
| all header+totals fields correct | 142 docs |

| Field | matched | mismatched | missing | rate |
|---|---|---|---|---|
| cgst_amount | 102 | 8 | 112 | 92.7% |
| customer_name | 218 | 4 | 0 | 98.2% |
| customer_tax_id | 212 | 10 | 0 | 95.5% |
| due_date | 213 | 9 | 0 | 96.0% |
| igst_amount | 90 | 2 | 130 | 97.8% |
| invoice_date | 211 | 11 | 0 | 95.0% |
| invoice_number | 201 | 21 | 0 | 90.5% |
| invoice_total_ex_tax | 156 | 53 | 13 | 74.6% |
| invoice_total_inc_tax | 212 | 10 | 0 | 95.5% |
| line.amount_ex_tax | 728 | 1 | 1 | 99.9% |
| line.description | 727 | 3 | 0 | 99.6% |
| line.hsn_sac | 707 | 23 | 0 | 96.9% |
| line.quantity | 727 | 1 | 2 | 99.9% |
| line.unit_price | 727 | 2 | 1 | 99.7% |
| line_count | 156 | 66 | 0 | 70.3% |
| po_number | 213 | 9 | 0 | 96.0% |
| sgst_amount | 102 | 8 | 112 | 92.7% |
| tax_amount | 193 | 11 | 18 | 94.6% |
| vendor_tax_id | 198 | 24 | 0 | 89.2% |
| work_authorization_reference | 216 | 6 | 0 | 97.3% |

## Timing and cost

| Group | docs | processing s (mean/median/p95) | wall s mean | LLM calls mean | cost USD sum | errors |
|---|---|---|---|---|---|---|
| all | 300 | 371.892/50.3/103.44 | 339.846 | 2.238 | 3.529 | 0 |
| A | 100 | 367.335/46.63/96.18 | 335.674 | 2.341 | 1.131 | 0 |
| B | 100 | 380.757/63.8/104.67 | 347.958 | 2.143 | 1.347 | 0 |
| C | 100 | 367.583/49.32/90.06 | 335.906 | 2.231 | 1.051 | 0 |

Not measured: OCR cost (folded into Gemini cost), infrastructure cost, human review time.
