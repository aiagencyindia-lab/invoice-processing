# Benchmark run `after`

Generated 2026-09-05T15:34:49Z at `d3d07fc` with profile `benchmark/harness/profiles/after.json`; 300 of 300 submissions recorded.

## Decisions

| Measure | Value |
|---|---|
| total | 300 |
| expected_ACCEPT | 45 |
| expected_REJECT | 255 |
| auto_approved | 46 |
| correctly_auto_approved | 42 |
| incorrectly_auto_approved | 4 |
| rejected | 254 |
| correctly_rejected | 251 |
| incorrectly_rejected | 3 |
| errors_or_no_decision | 0 |
| reason_strict | 214 |
| reason_family | 30 |
| reason_wrong | 7 |
| touchless rate (ACCEPT / 300) | 15.3% |
| correct touchless rate (correct ACCEPT / 300) | 14.0% |
| touchless accuracy (correct ACCEPT / ACCEPT) | 91.3% |
| decision accuracy | 97.7% |
| accepted but routed to review | 42 |

Reason match is scored on correctly rejected invoices: `strict` = the app's reason is the dataset's first-failing reason; `family` = it names another fault the dataset planted on that invoice; `wrong` = neither.

## Per check

Positive class = FAIL (for 4.1, DISCREPANCY). `cascade` follows the dataset README: an expected FAIL counts only where it is the first failing check (a NOT_RUN there is a false negative); an expected PASS counts only where the check was reached. `all findings` scores every check the pipeline actually reached. `balanced` restricts to the dataset's balanced population.

### Cascade-aware, full population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 273 | 50 | 223 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 1.2 customer_verification | 226 | 40 | 179 | 0 | 7 | 96.9% | 100.0% | 85.1% | 0.0% | 14.9% | 3 | 42 |
| 1.3 tax_compliance | 223 | 6 | 214 | 0 | 3 | 98.7% | 100.0% | 66.7% | 0.0% | 33.3% | 1 | 8 |
| 1.4 work_authorization | 130 | 31 | 89 | 0 | 10 | 92.3% | 100.0% | 75.6% | 0.0% | 24.4% | 8 | 43 |
| 1.5 single_document | 206 | 21 | 182 | 0 | 3 | 98.5% | 100.0% | 87.5% | 0.0% | 12.5% | 3 | 0 |
| 2.1 line_items_present | 118 | 6 | 102 | 0 | 10 | 91.5% | 100.0% | 37.5% | 0.0% | 62.5% | 1 | 0 |
| 2.2 po_validation | 94 | 24 | 66 | 2 | 2 | 95.7% | 92.3% | 92.3% | 2.9% | 7.7% | 2 | 2 |
| 3.1 duplicate_detection | 92 | 18 | 74 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 3.2 tax_id_validation | 81 | 5 | 71 | 4 | 1 | 93.8% | 55.6% | 83.3% | 5.3% | 16.7% | 1 | 0 |
| 3.3 future_date_check | 78 | 5 | 73 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.1 total_verification | 56 | 4 | 52 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 54 | 2 | 49 | 0 | 3 | 94.4% | 100.0% | 40.0% | 0.0% | 60.0% | 1 | 0 |
| 4.3 authorization_hours_check | 31 | 6 | 24 | 0 | 1 | 96.8% | 100.0% | 85.7% | 0.0% | 14.3% | 1 | 0 |
| 4.4 tax_rate_verification | 51 | 0 | 49 | 1 | 1 | 96.1% | 0.0% | 0.0% | 2.0% | 100.0% | 0 | 2 |

### Cascade-aware, balanced population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 96 | 50 | 46 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 1.2 customer_verification | 86 | 40 | 39 | 0 | 7 | 91.9% | 100.0% | 85.1% | 0.0% | 14.9% | 3 | 0 |
| 1.3 tax_compliance | 52 | 6 | 43 | 0 | 3 | 94.2% | 100.0% | 66.7% | 0.0% | 33.3% | 1 | 0 |
| 1.4 work_authorization | 82 | 31 | 41 | 0 | 10 | 87.8% | 100.0% | 75.6% | 0.0% | 24.4% | 8 | 0 |
| 1.5 single_document | 64 | 21 | 40 | 0 | 3 | 95.3% | 100.0% | 87.5% | 0.0% | 12.5% | 3 | 0 |
| 2.1 line_items_present | 40 | 6 | 24 | 0 | 10 | 75.0% | 100.0% | 37.5% | 0.0% | 62.5% | 1 | 0 |
| 2.2 po_validation | 52 | 24 | 26 | 0 | 2 | 96.2% | 100.0% | 92.3% | 0.0% | 7.7% | 2 | 0 |
| 3.1 duplicate_detection | 36 | 18 | 18 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 3.2 tax_id_validation | 28 | 5 | 21 | 1 | 1 | 92.9% | 83.3% | 83.3% | 4.5% | 16.7% | 1 | 0 |
| 3.3 future_date_check | 20 | 5 | 15 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.1 total_verification | 17 | 4 | 13 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 23 | 2 | 18 | 0 | 3 | 87.0% | 100.0% | 40.0% | 0.0% | 60.0% | 1 | 0 |
| 4.3 authorization_hours_check | 30 | 6 | 24 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.4 tax_rate_verification | 20 | 0 | 18 | 1 | 1 | 90.0% | 0.0% | 0.0% | 5.3% | 100.0% | 0 | 0 |

### All findings, full population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 273 | 50 | 223 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 1.2 customer_verification | 223 | 40 | 179 | 0 | 4 | 98.2% | 100.0% | 90.9% | 0.0% | 9.1% | 0 | 42 |
| 1.3 tax_compliance | 265 | 41 | 214 | 0 | 10 | 96.2% | 100.0% | 80.4% | 0.0% | 19.6% | 0 | 8 |
| 1.4 work_authorization | 129 | 38 | 89 | 0 | 2 | 98.5% | 100.0% | 95.0% | 0.0% | 5.0% | 0 | 43 |
| 1.5 single_document | 223 | 41 | 182 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 2.1 line_items_present | 118 | 7 | 102 | 0 | 9 | 92.4% | 100.0% | 43.8% | 0.0% | 56.2% | 0 | 0 |
| 2.2 po_validation | 92 | 24 | 66 | 2 | 0 | 97.8% | 92.3% | 100.0% | 2.9% | 0.0% | 0 | 2 |
| 3.1 duplicate_detection | 110 | 36 | 74 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 3.2 tax_id_validation | 83 | 8 | 71 | 4 | 0 | 95.2% | 66.7% | 100.0% | 5.3% | 0.0% | 0 | 0 |
| 3.3 future_date_check | 83 | 10 | 73 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.1 total_verification | 56 | 4 | 52 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 53 | 2 | 49 | 0 | 2 | 96.2% | 100.0% | 50.0% | 0.0% | 50.0% | 0 | 0 |
| 4.3 authorization_hours_check | 31 | 7 | 24 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.4 tax_rate_verification | 53 | 2 | 49 | 1 | 1 | 96.2% | 66.7% | 66.7% | 2.0% | 33.3% | 0 | 2 |

## First failing check: expected → actual

| expected → actual | n |
|---|---|
| 1.1->1.1 | 50 |
| none->none | 42 |
| 1.2->1.2 | 40 |
| 1.4->1.4 | 31 |
| 2.2->2.2 | 24 |
| 1.5->1.5 | 21 |
| 3.1->3.1 | 18 |
| 1.4->3.1 | 8 |
| 1.3->1.3 | 6 |
| 2.1->2.1 | 6 |
| 4.3->4.3 | 6 |
| 3.2->3.2 | 5 |
| 3.3->3.3 | 5 |
| 1.2->1.5 | 4 |
| 1.2->3.1 | 3 |
| 1.5->3.1 | 3 |
| 2.1->3.3 | 3 |
| none->3.2 | 3 |
| 1.3->1.5 | 2 |
| 2.1->2.2 | 2 |
| 2.1->4.4 | 2 |
| 2.2->3.1 | 2 |
| 4.2->4.2 | 2 |
| 4.2->none | 2 |
| 1.3->3.1 | 1 |
| 1.4->1.5 | 1 |
| 1.4->2.1 | 1 |
| 2.1->3.1 | 1 |
| 2.1->3.2 | 1 |
| 2.1->none | 1 |
| 3.2->2.2 | 1 |
| 4.2->2.2 | 1 |
| 4.3->3.2 | 1 |
| 4.4->none | 1 |

## Extraction (vs as-submitted values)

Scored on 223 extracted invoice documents (0 not extracted; non-invoice documents and ingest-level duplicates excluded).

| Group | Accuracy |
|---|---|
| header | 94.5% |
| totals | 90.4% |
| lines | 88.0% |
| tax | 94.6% |
| all header+totals fields correct | 135 docs |

| Field | matched | mismatched | missing | rate |
|---|---|---|---|---|
| cgst_amount | 103 | 8 | 112 | 92.8% |
| customer_name | 219 | 4 | 0 | 98.2% |
| customer_tax_id | 214 | 9 | 0 | 96.0% |
| due_date | 214 | 9 | 0 | 96.0% |
| igst_amount | 90 | 2 | 131 | 97.8% |
| invoice_date | 211 | 12 | 0 | 94.6% |
| invoice_number | 203 | 20 | 0 | 91.0% |
| invoice_total_ex_tax | 153 | 57 | 13 | 72.9% |
| invoice_total_inc_tax | 213 | 10 | 0 | 95.5% |
| line.amount_ex_tax | 732 | 1 | 0 | 99.9% |
| line.description | 731 | 2 | 0 | 99.7% |
| line.hsn_sac | 710 | 23 | 0 | 96.9% |
| line.quantity | 730 | 1 | 2 | 99.9% |
| line.unit_price | 730 | 2 | 1 | 99.7% |
| line_count | 167 | 56 | 0 | 74.9% |
| po_number | 212 | 11 | 0 | 95.1% |
| sgst_amount | 103 | 8 | 112 | 92.8% |
| tax_amount | 198 | 10 | 15 | 95.2% |
| vendor_tax_id | 196 | 27 | 0 | 87.9% |
| work_authorization_reference | 217 | 6 | 0 | 97.3% |

## Timing and cost

| Group | docs | processing s (mean/median/p95) | wall s mean | LLM calls mean | cost USD sum | errors |
|---|---|---|---|---|---|---|
| all | 300 | 59.508/50.76/108.61 | 54.194 | 2.234 | 3.756 | 0 |
| A | 100 | 51.48/47.44/95.36 | 46.892 | 2.341 | 1.217 | 0 |
| B | 100 | 71.85/61.45/132.68 | 65.42 | 2.132 | 1.457 | 0 |
| C | 100 | 55.194/46.39/91.16 | 50.268 | 2.231 | 1.083 | 0 |

Not measured: OCR cost (folded into Gemini cost), infrastructure cost, human review time.
