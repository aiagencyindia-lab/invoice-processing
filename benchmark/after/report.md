# Benchmark run `after`

Generated 2026-09-05T12:10:28Z at `18843b3` with profile `benchmark/harness/profiles/after.json`; 300 of 300 submissions recorded.

## Decisions

| Measure | Value |
|---|---|
| total | 300 |
| expected_ACCEPT | 45 |
| expected_REJECT | 255 |
| auto_approved | 26 |
| correctly_auto_approved | 23 |
| incorrectly_auto_approved | 3 |
| rejected | 274 |
| correctly_rejected | 252 |
| incorrectly_rejected | 22 |
| errors_or_no_decision | 0 |
| reason_strict | 216 |
| reason_family | 28 |
| reason_wrong | 8 |
| touchless rate (ACCEPT / 300) | 8.7% |
| correct touchless rate (correct ACCEPT / 300) | 7.7% |
| touchless accuracy (correct ACCEPT / ACCEPT) | 88.5% |
| decision accuracy | 91.7% |
| accepted but routed to review | 22 |

Reason match is scored on correctly rejected invoices: `strict` = the app's reason is the dataset's first-failing reason; `family` = it names another fault the dataset planted on that invoice; `wrong` = neither.

## Per check

Positive class = FAIL (for 4.1, DISCREPANCY). `cascade` follows the dataset README: an expected FAIL counts only where it is the first failing check (a NOT_RUN there is a false negative); an expected PASS counts only where the check was reached. `all findings` scores every check the pipeline actually reached. `balanced` restricts to the dataset's balanced population.

### Cascade-aware, full population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 273 | 50 | 221 | 2 | 0 | 99.3% | 96.2% | 100.0% | 0.9% | 0.0% | 0 | 0 |
| 1.2 customer_verification | 226 | 41 | 177 | 2 | 6 | 96.5% | 95.3% | 87.2% | 1.1% | 12.8% | 3 | 42 |
| 1.3 tax_compliance | 223 | 6 | 211 | 3 | 3 | 97.3% | 66.7% | 66.7% | 1.4% | 33.3% | 1 | 8 |
| 1.4 work_authorization | 130 | 32 | 88 | 1 | 9 | 92.3% | 97.0% | 78.0% | 1.1% | 21.9% | 8 | 44 |
| 1.5 single_document | 206 | 21 | 182 | 0 | 3 | 98.5% | 100.0% | 87.5% | 0.0% | 12.5% | 3 | 0 |
| 2.1 line_items_present | 117 | 6 | 101 | 0 | 10 | 91.5% | 100.0% | 37.5% | 0.0% | 62.5% | 2 | 0 |
| 2.2 po_validation | 92 | 24 | 64 | 2 | 2 | 95.7% | 92.3% | 92.3% | 3.0% | 7.7% | 2 | 0 |
| 3.1 duplicate_detection | 92 | 18 | 74 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 3.2 tax_id_validation | 80 | 6 | 71 | 3 | 0 | 96.2% | 66.7% | 100.0% | 4.0% | 0.0% | 0 | 0 |
| 3.3 future_date_check | 78 | 5 | 73 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.1 total_verification | 56 | 3 | 53 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 54 | 3 | 29 | 20 | 2 | 59.3% | 13.0% | 60.0% | 40.8% | 40.0% | 1 | 0 |
| 4.3 authorization_hours_check | 31 | 6 | 24 | 0 | 1 | 96.8% | 100.0% | 85.7% | 0.0% | 14.3% | 1 | 0 |
| 4.4 tax_rate_verification | 50 | 1 | 37 | 12 | 0 | 76.0% | 7.7% | 100.0% | 24.5% | 0.0% | 0 | 1 |

### Cascade-aware, balanced population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 96 | 50 | 45 | 1 | 0 | 99.0% | 98.0% | 100.0% | 2.2% | 0.0% | 0 | 0 |
| 1.2 customer_verification | 86 | 41 | 39 | 0 | 6 | 93.0% | 100.0% | 87.2% | 0.0% | 12.8% | 3 | 0 |
| 1.3 tax_compliance | 52 | 6 | 41 | 2 | 3 | 90.4% | 75.0% | 66.7% | 4.7% | 33.3% | 1 | 0 |
| 1.4 work_authorization | 82 | 32 | 40 | 1 | 9 | 87.8% | 97.0% | 78.0% | 2.4% | 21.9% | 8 | 0 |
| 1.5 single_document | 64 | 21 | 40 | 0 | 3 | 95.3% | 100.0% | 87.5% | 0.0% | 12.5% | 3 | 0 |
| 2.1 line_items_present | 39 | 6 | 23 | 0 | 10 | 74.4% | 100.0% | 37.5% | 0.0% | 62.5% | 2 | 0 |
| 2.2 po_validation | 51 | 24 | 24 | 1 | 2 | 94.1% | 96.0% | 92.3% | 4.0% | 7.7% | 2 | 0 |
| 3.1 duplicate_detection | 36 | 18 | 18 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 3.2 tax_id_validation | 28 | 6 | 21 | 1 | 0 | 96.4% | 85.7% | 100.0% | 4.5% | 0.0% | 0 | 0 |
| 3.3 future_date_check | 20 | 5 | 15 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.1 total_verification | 16 | 3 | 13 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 24 | 3 | 11 | 8 | 2 | 58.3% | 27.3% | 60.0% | 42.1% | 40.0% | 1 | 0 |
| 4.3 authorization_hours_check | 30 | 5 | 24 | 0 | 1 | 96.7% | 100.0% | 83.3% | 0.0% | 16.7% | 1 | 0 |
| 4.4 tax_rate_verification | 20 | 1 | 14 | 5 | 0 | 75.0% | 16.7% | 100.0% | 26.3% | 0.0% | 0 | 0 |

### All findings, full population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 273 | 50 | 221 | 2 | 0 | 99.3% | 96.2% | 100.0% | 0.9% | 0.0% | 0 | 0 |
| 1.2 customer_verification | 223 | 41 | 177 | 2 | 3 | 97.8% | 95.3% | 93.2% | 1.1% | 6.8% | 0 | 42 |
| 1.3 tax_compliance | 265 | 41 | 211 | 3 | 10 | 95.1% | 93.2% | 80.4% | 1.4% | 19.6% | 0 | 8 |
| 1.4 work_authorization | 129 | 38 | 88 | 1 | 2 | 97.7% | 97.4% | 95.0% | 1.1% | 5.0% | 0 | 44 |
| 1.5 single_document | 223 | 41 | 182 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 2.1 line_items_present | 115 | 6 | 101 | 0 | 8 | 93.0% | 100.0% | 42.9% | 0.0% | 57.1% | 0 | 0 |
| 2.2 po_validation | 90 | 24 | 64 | 2 | 0 | 97.8% | 92.3% | 100.0% | 3.0% | 0.0% | 0 | 0 |
| 3.1 duplicate_detection | 110 | 36 | 74 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 3.2 tax_id_validation | 83 | 9 | 71 | 3 | 0 | 96.4% | 75.0% | 100.0% | 4.0% | 0.0% | 0 | 0 |
| 3.3 future_date_check | 83 | 10 | 73 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.1 total_verification | 56 | 3 | 53 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 53 | 3 | 29 | 20 | 1 | 60.4% | 13.0% | 75.0% | 40.8% | 25.0% | 0 | 0 |
| 4.3 authorization_hours_check | 31 | 7 | 24 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.4 tax_rate_verification | 53 | 4 | 37 | 12 | 0 | 77.4% | 25.0% | 100.0% | 24.5% | 0.0% | 0 | 1 |

## First failing check: expected → actual

| expected → actual | n |
|---|---|
| 1.1->1.1 | 50 |
| 1.2->1.2 | 41 |
| 1.4->1.4 | 31 |
| 2.2->2.2 | 24 |
| none->none | 23 |
| 1.5->1.5 | 21 |
| 3.1->3.1 | 18 |
| none->4.2 | 18 |
| 1.4->3.1 | 8 |
| 1.3->1.3 | 6 |
| 2.1->2.1 | 6 |
| 3.2->3.2 | 6 |
| 3.3->3.3 | 5 |
| 4.3->4.3 | 4 |
| 1.2->1.5 | 3 |
| 1.2->3.1 | 3 |
| 1.5->3.1 | 3 |
| 2.1->3.3 | 3 |
| 4.2->4.2 | 3 |
| 1.3->1.5 | 2 |
| 2.1->none | 2 |
| 2.2->3.1 | 2 |
| 4.3->4.2 | 2 |
| none->3.2 | 2 |
| 1.3->3.1 | 1 |
| 1.4->1.3 | 1 |
| 1.4->1.5 | 1 |
| 2.1->1.1 | 1 |
| 2.1->2.2 | 1 |
| 2.1->3.1 | 1 |
| 2.1->3.2 | 1 |
| 2.1->4.4 | 1 |
| 4.2->3.2 | 1 |
| 4.2->none | 1 |
| 4.3->2.2 | 1 |
| 4.4->4.4 | 1 |
| none->1.1 | 1 |
| none->4.4 | 1 |

## Extraction (vs as-submitted values)

Scored on 221 extracted invoice documents (2 not extracted; non-invoice documents and ingest-level duplicates excluded).

| Group | Accuracy |
|---|---|
| header | 95.4% |
| totals | 91.5% |
| lines | 88.3% |
| tax | 95.9% |
| all header+totals fields correct | 135 docs |

| Field | matched | mismatched | missing | rate |
|---|---|---|---|---|
| cgst_amount | 103 | 5 | 113 | 95.4% |
| customer_name | 218 | 3 | 0 | 98.6% |
| customer_tax_id | 212 | 9 | 0 | 95.9% |
| due_date | 215 | 6 | 0 | 97.3% |
| igst_amount | 90 | 2 | 129 | 97.8% |
| invoice_date | 213 | 8 | 0 | 96.4% |
| invoice_number | 203 | 18 | 0 | 91.9% |
| invoice_total_ex_tax | 150 | 56 | 15 | 72.8% |
| invoice_total_inc_tax | 213 | 8 | 0 | 96.4% |
| line.amount_ex_tax | 728 | 1 | 0 | 99.9% |
| line.description | 726 | 3 | 0 | 99.6% |
| line.hsn_sac | 706 | 23 | 0 | 96.8% |
| line.quantity | 728 | 1 | 0 | 99.9% |
| line.unit_price | 727 | 2 | 0 | 99.7% |
| line_count | 156 | 65 | 0 | 70.6% |
| po_number | 214 | 7 | 0 | 96.8% |
| sgst_amount | 102 | 6 | 113 | 94.4% |
| tax_amount | 193 | 8 | 20 | 96.0% |
| vendor_tax_id | 198 | 23 | 0 | 89.6% |
| work_authorization_reference | 214 | 7 | 0 | 96.8% |

## Timing and cost

| Group | docs | processing s (mean/median/p95) | wall s mean | LLM calls mean | cost USD sum | errors |
|---|---|---|---|---|---|---|
| all | 300 | 58.721/51.44/104.61 | 53.466 | 2.234 | 3.458 | 0 |
| A | 100 | 47.994/46.12/88.82 | 43.706 | 2.341 | 1.114 | 0 |
| B | 100 | 65.22/64.89/115.32 | 59.38 | 2.121 | 1.332 | 0 |
| C | 100 | 62.947/48.66/108.32 | 57.311 | 2.242 | 1.013 | 0 |

Not measured: OCR cost (folded into Gemini cost), infrastructure cost, human review time.
