# Benchmark run `prod300`

Generated 2026-09-05T21:13:00Z at `e538061` with profile `benchmark/harness/profiles/after.json`; 300 of 300 submissions recorded.

## Decisions

| Measure | Value |
|---|---|
| total | 300 |
| expected_ACCEPT | 45 |
| expected_REJECT | 255 |
| auto_approved | 44 |
| correctly_auto_approved | 41 |
| incorrectly_auto_approved | 3 |
| rejected | 256 |
| correctly_rejected | 252 |
| incorrectly_rejected | 4 |
| errors_or_no_decision | 0 |
| reason_strict | 214 |
| reason_family | 30 |
| reason_wrong | 8 |
| touchless rate (ACCEPT / 300) | 14.7% |
| correct touchless rate (correct ACCEPT / 300) | 13.7% |
| touchless accuracy (correct ACCEPT / ACCEPT) | 93.2% |
| decision accuracy | 97.7% |
| accepted but routed to review | 40 |

Reason match is scored on correctly rejected invoices: `strict` = the app's reason is the dataset's first-failing reason; `family` = it names another fault the dataset planted on that invoice; `wrong` = neither.

## Per check

Positive class = FAIL (for 4.1, DISCREPANCY). `cascade` follows the dataset README: an expected FAIL counts only where it is the first failing check (a NOT_RUN there is a false negative); an expected PASS counts only where the check was reached. `all findings` scores every check the pipeline actually reached. `balanced` restricts to the dataset's balanced population.

### Cascade-aware, full population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 273 | 50 | 223 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 1.2 customer_verification | 226 | 39 | 179 | 0 | 8 | 96.5% | 100.0% | 83.0% | 0.0% | 17.0% | 3 | 43 |
| 1.3 tax_compliance | 223 | 6 | 214 | 0 | 3 | 98.7% | 100.0% | 66.7% | 0.0% | 33.3% | 1 | 8 |
| 1.4 work_authorization | 130 | 32 | 89 | 0 | 9 | 93.1% | 100.0% | 78.0% | 0.0% | 21.9% | 8 | 43 |
| 1.5 single_document | 206 | 21 | 182 | 0 | 3 | 98.5% | 100.0% | 87.5% | 0.0% | 12.5% | 3 | 0 |
| 2.1 line_items_present | 118 | 6 | 102 | 0 | 10 | 91.5% | 100.0% | 37.5% | 0.0% | 62.5% | 1 | 0 |
| 2.2 po_validation | 93 | 24 | 64 | 3 | 2 | 94.6% | 88.9% | 92.3% | 4.5% | 7.7% | 2 | 1 |
| 3.1 duplicate_detection | 92 | 18 | 74 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 3.2 tax_id_validation | 81 | 5 | 70 | 5 | 1 | 92.6% | 50.0% | 83.3% | 6.7% | 16.7% | 1 | 0 |
| 3.3 future_date_check | 78 | 5 | 73 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.1 total_verification | 56 | 3 | 52 | 1 | 0 | 98.2% | 75.0% | 100.0% | 1.9% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 55 | 2 | 49 | 1 | 3 | 92.7% | 66.7% | 40.0% | 2.0% | 60.0% | 2 | 0 |
| 4.3 authorization_hours_check | 31 | 7 | 24 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.4 tax_rate_verification | 50 | 0 | 48 | 1 | 1 | 96.0% | 0.0% | 0.0% | 2.0% | 100.0% | 0 | 2 |

### Cascade-aware, balanced population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 96 | 50 | 46 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 1.2 customer_verification | 86 | 39 | 39 | 0 | 8 | 90.7% | 100.0% | 83.0% | 0.0% | 17.0% | 3 | 0 |
| 1.3 tax_compliance | 52 | 6 | 43 | 0 | 3 | 94.2% | 100.0% | 66.7% | 0.0% | 33.3% | 1 | 0 |
| 1.4 work_authorization | 82 | 32 | 41 | 0 | 9 | 89.0% | 100.0% | 78.0% | 0.0% | 21.9% | 8 | 0 |
| 1.5 single_document | 64 | 21 | 40 | 0 | 3 | 95.3% | 100.0% | 87.5% | 0.0% | 12.5% | 3 | 0 |
| 2.1 line_items_present | 40 | 6 | 24 | 0 | 10 | 75.0% | 100.0% | 37.5% | 0.0% | 62.5% | 1 | 0 |
| 2.2 po_validation | 51 | 24 | 24 | 1 | 2 | 94.1% | 96.0% | 92.3% | 4.0% | 7.7% | 2 | 0 |
| 3.1 duplicate_detection | 37 | 18 | 19 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 3.2 tax_id_validation | 27 | 5 | 19 | 2 | 1 | 88.9% | 71.4% | 83.3% | 9.5% | 16.7% | 1 | 0 |
| 3.3 future_date_check | 20 | 5 | 15 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.1 total_verification | 16 | 3 | 13 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 23 | 2 | 18 | 0 | 3 | 87.0% | 100.0% | 40.0% | 0.0% | 60.0% | 2 | 0 |
| 4.3 authorization_hours_check | 30 | 6 | 24 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.4 tax_rate_verification | 20 | 0 | 18 | 1 | 1 | 90.0% | 0.0% | 0.0% | 5.3% | 100.0% | 0 | 0 |

### All findings, full population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 273 | 50 | 223 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 1.2 customer_verification | 223 | 39 | 179 | 0 | 5 | 97.8% | 100.0% | 88.6% | 0.0% | 11.4% | 0 | 43 |
| 1.3 tax_compliance | 265 | 42 | 214 | 0 | 9 | 96.6% | 100.0% | 82.3% | 0.0% | 17.6% | 0 | 8 |
| 1.4 work_authorization | 129 | 38 | 89 | 0 | 2 | 98.5% | 100.0% | 95.0% | 0.0% | 5.0% | 0 | 43 |
| 1.5 single_document | 223 | 41 | 182 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 2.1 line_items_present | 117 | 6 | 102 | 0 | 9 | 92.3% | 100.0% | 40.0% | 0.0% | 60.0% | 0 | 0 |
| 2.2 po_validation | 91 | 24 | 64 | 3 | 0 | 96.7% | 88.9% | 100.0% | 4.5% | 0.0% | 0 | 1 |
| 3.1 duplicate_detection | 110 | 36 | 74 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 3.2 tax_id_validation | 83 | 8 | 70 | 5 | 0 | 94.0% | 61.5% | 100.0% | 6.7% | 0.0% | 0 | 0 |
| 3.3 future_date_check | 83 | 10 | 73 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.1 total_verification | 56 | 3 | 52 | 1 | 0 | 98.2% | 75.0% | 100.0% | 1.9% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 53 | 2 | 49 | 1 | 1 | 96.2% | 66.7% | 66.7% | 2.0% | 33.3% | 0 | 0 |
| 4.3 authorization_hours_check | 32 | 8 | 24 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.4 tax_rate_verification | 53 | 3 | 48 | 1 | 1 | 96.2% | 75.0% | 75.0% | 2.0% | 25.0% | 0 | 2 |

## First failing check: expected → actual

| expected → actual | n |
|---|---|
| 1.1->1.1 | 50 |
| none->none | 41 |
| 1.2->1.2 | 39 |
| 1.4->1.4 | 32 |
| 2.2->2.2 | 24 |
| 1.5->1.5 | 21 |
| 3.1->3.1 | 18 |
| 1.4->3.1 | 8 |
| 4.3->4.3 | 7 |
| 1.3->1.3 | 6 |
| 2.1->2.1 | 6 |
| 1.2->1.5 | 5 |
| 3.2->3.2 | 5 |
| 3.3->3.3 | 4 |
| 1.2->3.1 | 3 |
| 1.5->3.1 | 3 |
| 2.1->3.3 | 3 |
| 1.3->1.5 | 2 |
| 2.1->2.2 | 2 |
| 2.1->4.4 | 2 |
| 2.2->3.1 | 2 |
| 4.2->3.2 | 2 |
| 4.2->4.2 | 2 |
| none->3.2 | 2 |
| 1.3->3.1 | 1 |
| 1.4->1.5 | 1 |
| 2.1->3.1 | 1 |
| 2.1->3.2 | 1 |
| 2.1->none | 1 |
| 3.2->2.2 | 1 |
| 3.3->3.2 | 1 |
| 4.2->none | 1 |
| 4.4->none | 1 |
| none->2.2 | 1 |
| none->4.2 | 1 |

## Extraction (vs as-submitted values)

Scored on 223 extracted invoice documents (0 not extracted; non-invoice documents and ingest-level duplicates excluded).

| Group | Accuracy |
|---|---|
| header | 94.3% |
| totals | 91.4% |
| lines | 88.2% |
| tax | 94.8% |
| all header+totals fields correct | 143 docs |

| Field | matched | mismatched | missing | rate |
|---|---|---|---|---|
| cgst_amount | 104 | 7 | 112 | 93.7% |
| customer_name | 218 | 5 | 0 | 97.8% |
| customer_tax_id | 210 | 13 | 0 | 94.2% |
| due_date | 215 | 8 | 0 | 96.4% |
| igst_amount | 90 | 2 | 131 | 97.8% |
| invoice_date | 212 | 11 | 0 | 95.1% |
| invoice_number | 203 | 20 | 0 | 91.0% |
| invoice_total_ex_tax | 161 | 50 | 12 | 76.3% |
| invoice_total_inc_tax | 214 | 9 | 0 | 96.0% |
| line.amount_ex_tax | 731 | 2 | 0 | 99.7% |
| line.description | 730 | 3 | 0 | 99.6% |
| line.hsn_sac | 709 | 24 | 0 | 96.7% |
| line.quantity | 728 | 1 | 4 | 99.9% |
| line.unit_price | 728 | 3 | 2 | 99.6% |
| line_count | 172 | 51 | 0 | 77.1% |
| po_number | 214 | 9 | 0 | 96.0% |
| sgst_amount | 103 | 8 | 112 | 92.8% |
| tax_amount | 195 | 10 | 18 | 95.1% |
| vendor_tax_id | 194 | 29 | 0 | 87.0% |
| work_authorization_reference | 217 | 6 | 0 | 97.3% |

## Timing and cost

| Group | docs | processing s (mean/median/p95) | wall s mean | LLM calls mean | cost USD sum | errors |
|---|---|---|---|---|---|---|
| all | 300 | 81.3/78.86/135.57 | 152.604 | 2.231 | 3.74 | 0 |
| A | 100 | 73.078/71.95/115.87 | 145.368 | 2.297 | 1.152 | 0 |
| B | 100 | 92.557/93.23/151.62 | 163.386 | 2.143 | 1.471 | 0 |
| C | 100 | 78.263/75.88/127.17 | 149.059 | 2.253 | 1.116 | 0 |

Not measured: OCR cost (folded into Gemini cost), infrastructure cost, human review time.
