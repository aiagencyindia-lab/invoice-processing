# Benchmark run `after`

Generated 2026-09-04T22:20:42Z at `2521ff9` with profile `benchmark/harness/profiles/after.json`; 300 of 300 submissions recorded.

## Decisions

| Measure | Value |
|---|---|
| total | 300 |
| expected_ACCEPT | 45 |
| expected_REJECT | 255 |
| auto_approved | 14 |
| correctly_auto_approved | 12 |
| incorrectly_auto_approved | 2 |
| rejected | 286 |
| correctly_rejected | 253 |
| incorrectly_rejected | 33 |
| errors_or_no_decision | 0 |
| reason_strict | 194 |
| reason_family | 29 |
| reason_wrong | 30 |
| touchless rate (ACCEPT / 300) | 4.7% |
| correct touchless rate (correct ACCEPT / 300) | 4.0% |
| touchless accuracy (correct ACCEPT / ACCEPT) | 85.7% |
| decision accuracy | 88.3% |
| accepted but routed to review | 12 |

Reason match is scored on correctly rejected invoices: `strict` = the app's reason is the dataset's first-failing reason; `family` = it names another fault the dataset planted on that invoice; `wrong` = neither.

## Per check

Positive class = FAIL (for 4.1, DISCREPANCY). `cascade` follows the dataset README: an expected FAIL counts only where it is the first failing check (a NOT_RUN there is a false negative); an expected PASS counts only where the check was reached. `all findings` scores every check the pipeline actually reached. `balanced` restricts to the dataset's balanced population.

### Cascade-aware, full population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 273 | 49 | 223 | 0 | 1 | 99.6% | 100.0% | 98.0% | 0.0% | 2.0% | 0 | 0 |
| 1.2 customer_verification | 226 | 40 | 174 | 5 | 7 | 94.7% | 88.9% | 85.1% | 2.8% | 14.9% | 3 | 42 |
| 1.3 tax_compliance | 223 | 6 | 201 | 13 | 3 | 92.8% | 31.6% | 66.7% | 6.1% | 33.3% | 1 | 8 |
| 1.4 work_authorization | 130 | 32 | 89 | 0 | 9 | 93.1% | 100.0% | 78.0% | 0.0% | 21.9% | 8 | 43 |
| 1.5 single_document | 206 | 21 | 182 | 0 | 3 | 98.5% | 100.0% | 87.5% | 0.0% | 12.5% | 3 | 0 |
| 2.1 line_items_present | 115 | 0 | 99 | 0 | 16 | 86.1% | - | 0.0% | 0.0% | 100.0% | 7 | 0 |
| 2.2 po_validation | 86 | 23 | 37 | 23 | 3 | 69.8% | 50.0% | 88.5% | 38.3% | 11.5% | 3 | 0 |
| 3.1 duplicate_detection | 73 | 15 | 55 | 0 | 3 | 95.9% | 100.0% | 83.3% | 0.0% | 16.7% | 2 | 0 |
| 3.2 tax_id_validation | 62 | 4 | 54 | 2 | 2 | 93.5% | 66.7% | 66.7% | 3.6% | 33.3% | 2 | 0 |
| 3.3 future_date_check | 59 | 4 | 54 | 0 | 1 | 98.3% | 100.0% | 80.0% | 0.0% | 20.0% | 1 | 0 |
| 4.1 total_verification | 42 | 2 | 40 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 40 | 3 | 16 | 19 | 2 | 47.5% | 13.6% | 60.0% | 54.3% | 40.0% | 1 | 0 |
| 4.3 authorization_hours_check | 19 | 5 | 12 | 0 | 2 | 89.5% | 100.0% | 71.4% | 0.0% | 28.6% | 2 | 0 |
| 4.4 tax_rate_verification | 38 | 0 | 26 | 11 | 1 | 68.4% | 0.0% | 0.0% | 29.7% | 100.0% | 1 | 2 |

### Cascade-aware, balanced population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 96 | 49 | 46 | 0 | 1 | 99.0% | 100.0% | 98.0% | 0.0% | 2.0% | 0 | 0 |
| 1.2 customer_verification | 86 | 40 | 36 | 3 | 7 | 88.4% | 93.0% | 85.1% | 7.7% | 14.9% | 3 | 0 |
| 1.3 tax_compliance | 52 | 6 | 39 | 4 | 3 | 86.5% | 60.0% | 66.7% | 9.3% | 33.3% | 1 | 0 |
| 1.4 work_authorization | 82 | 32 | 41 | 0 | 9 | 89.0% | 100.0% | 78.0% | 0.0% | 21.9% | 8 | 0 |
| 1.5 single_document | 64 | 21 | 40 | 0 | 3 | 95.3% | 100.0% | 87.5% | 0.0% | 12.5% | 3 | 0 |
| 2.1 line_items_present | 40 | 0 | 24 | 0 | 16 | 60.0% | - | 0.0% | 0.0% | 100.0% | 7 | 0 |
| 2.2 po_validation | 49 | 23 | 11 | 12 | 3 | 69.4% | 65.7% | 88.5% | 52.2% | 11.5% | 3 | 0 |
| 3.1 duplicate_detection | 32 | 15 | 14 | 0 | 3 | 90.6% | 100.0% | 83.3% | 0.0% | 16.7% | 2 | 0 |
| 3.2 tax_id_validation | 22 | 4 | 15 | 1 | 2 | 86.4% | 80.0% | 66.7% | 6.2% | 33.3% | 2 | 0 |
| 3.3 future_date_check | 18 | 4 | 13 | 0 | 1 | 94.4% | 100.0% | 80.0% | 0.0% | 20.0% | 1 | 0 |
| 4.1 total_verification | 13 | 2 | 11 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 16 | 3 | 6 | 5 | 2 | 56.2% | 37.5% | 60.0% | 45.5% | 40.0% | 1 | 0 |
| 4.3 authorization_hours_check | 18 | 4 | 12 | 0 | 2 | 88.9% | 100.0% | 66.7% | 0.0% | 33.3% | 2 | 0 |
| 4.4 tax_rate_verification | 14 | 0 | 8 | 5 | 1 | 57.1% | 0.0% | 0.0% | 38.5% | 100.0% | 1 | 0 |

### All findings, full population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 273 | 49 | 223 | 0 | 1 | 99.6% | 100.0% | 98.0% | 0.0% | 2.0% | 0 | 0 |
| 1.2 customer_verification | 223 | 40 | 174 | 5 | 4 | 96.0% | 88.9% | 90.9% | 2.8% | 9.1% | 0 | 42 |
| 1.3 tax_compliance | 265 | 41 | 201 | 13 | 10 | 91.3% | 75.9% | 80.4% | 6.1% | 19.6% | 0 | 8 |
| 1.4 work_authorization | 129 | 39 | 89 | 0 | 1 | 99.2% | 100.0% | 97.5% | 0.0% | 2.5% | 0 | 43 |
| 1.5 single_document | 223 | 41 | 182 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 2.1 line_items_present | 108 | 0 | 99 | 0 | 9 | 91.7% | - | 0.0% | 0.0% | 100.0% | 0 | 0 |
| 2.2 po_validation | 83 | 23 | 37 | 23 | 0 | 72.3% | 50.0% | 100.0% | 38.3% | 0.0% | 0 | 0 |
| 3.1 duplicate_detection | 89 | 33 | 55 | 0 | 1 | 98.9% | 100.0% | 97.1% | 0.0% | 2.9% | 0 | 0 |
| 3.2 tax_id_validation | 62 | 6 | 54 | 2 | 0 | 96.8% | 75.0% | 100.0% | 3.6% | 0.0% | 0 | 0 |
| 3.3 future_date_check | 62 | 8 | 54 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.1 total_verification | 42 | 2 | 40 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 39 | 3 | 16 | 19 | 1 | 48.7% | 13.6% | 75.0% | 54.3% | 25.0% | 0 | 0 |
| 4.3 authorization_hours_check | 18 | 6 | 12 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.4 tax_rate_verification | 39 | 2 | 26 | 11 | 0 | 71.8% | 15.4% | 100.0% | 29.7% | 0.0% | 0 | 2 |

## First failing check: expected → actual

| expected → actual | n |
|---|---|
| 1.1->1.1 | 49 |
| 1.2->1.2 | 40 |
| 1.4->1.4 | 27 |
| 2.2->2.2 | 23 |
| 1.5->1.5 | 19 |
| none->4.2 | 18 |
| 3.1->3.1 | 15 |
| none->2.2 | 13 |
| none->none | 12 |
| 1.4->3.1 | 8 |
| 1.3->1.3 | 6 |
| 2.1->1.3 | 6 |
| 1.4->1.3 | 5 |
| 1.2->1.5 | 4 |
| 3.2->3.2 | 4 |
| 3.3->3.3 | 4 |
| 4.3->4.3 | 4 |
| 1.2->3.1 | 3 |
| 1.5->3.1 | 3 |
| 2.1->2.2 | 3 |
| 4.2->4.2 | 3 |
| 1.3->1.5 | 2 |
| 1.5->1.2 | 2 |
| 2.1->3.3 | 2 |
| 2.1->4.4 | 2 |
| 2.2->3.1 | 2 |
| 3.1->2.2 | 2 |
| 4.3->2.2 | 2 |
| 1.1->1.2 | 1 |
| 1.3->3.1 | 1 |
| 1.4->1.5 | 1 |
| 2.1->3.1 | 1 |
| 2.1->3.2 | 1 |
| 2.1->none | 1 |
| 2.2->1.2 | 1 |
| 3.1->3.2 | 1 |
| 3.2->1.2 | 1 |
| 3.2->2.2 | 1 |
| 3.3->2.2 | 1 |
| 4.2->3.2 | 1 |
| 4.2->none | 1 |
| 4.3->4.2 | 1 |
| 4.4->2.2 | 1 |
| none->1.2 | 1 |
| none->3.2 | 1 |

## Extraction (vs as-submitted values)

Scored on 223 extracted invoice documents (0 not extracted; non-invoice documents and ingest-level duplicates excluded).

| Group | Accuracy |
|---|---|
| header | 94.6% |
| totals | 90.6% |
| lines | 87.6% |
| tax | 94.6% |
| all header+totals fields correct | 139 docs |

| Field | matched | mismatched | missing | rate |
|---|---|---|---|---|
| cgst_amount | 104 | 8 | 111 | 92.9% |
| customer_name | 219 | 4 | 0 | 98.2% |
| customer_tax_id | 212 | 11 | 0 | 95.1% |
| due_date | 214 | 9 | 0 | 96.0% |
| igst_amount | 90 | 2 | 131 | 97.8% |
| invoice_date | 212 | 11 | 0 | 95.1% |
| invoice_number | 202 | 21 | 0 | 90.6% |
| invoice_total_ex_tax | 153 | 57 | 13 | 72.9% |
| invoice_total_inc_tax | 213 | 10 | 0 | 95.5% |
| line.amount_ex_tax | 729 | 3 | 0 | 99.6% |
| line.description | 727 | 5 | 0 | 99.3% |
| line.hsn_sac | 711 | 21 | 0 | 97.1% |
| line.quantity | 729 | 3 | 0 | 99.6% |
| line.unit_price | 728 | 4 | 0 | 99.5% |
| line_count | 156 | 67 | 0 | 70.0% |
| po_number | 212 | 11 | 0 | 95.1% |
| sgst_amount | 104 | 8 | 111 | 92.9% |
| tax_amount | 196 | 10 | 17 | 95.2% |
| vendor_tax_id | 199 | 24 | 0 | 89.2% |
| work_authorization_reference | 218 | 5 | 0 | 97.8% |

## Timing and cost

| Group | docs | processing s (mean/median/p95) | wall s mean | LLM calls mean | cost USD sum | errors |
|---|---|---|---|---|---|---|
| all | 300 | 59.669/50.96/114.93 | 55.931 | 2.245 | 3.565 | 0 |
| A | 100 | 52.323/45.13/103.68 | 49.194 | 2.341 | 1.17 | 0 |
| B | 100 | 71.313/70.43/122.08 | 66.671 | 2.143 | 1.355 | 0 |
| C | 100 | 55.37/50.06/90.61 | 51.927 | 2.253 | 1.04 | 0 |

Not measured: OCR cost (folded into Gemini cost), infrastructure cost, human review time.
