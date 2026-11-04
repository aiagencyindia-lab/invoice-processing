# Benchmark run `prod-2026-11-05`

Generated 2026-11-04T21:34:18Z at `e4c22e3` with profile `benchmark/harness/profiles/after.json`; 300 of 300 submissions recorded.

## Decisions

| Measure | Value |
|---|---|
| total | 300 |
| expected_ACCEPT | 45 |
| expected_REJECT | 255 |
| auto_approved | 46 |
| correctly_auto_approved | 43 |
| incorrectly_auto_approved | 3 |
| rejected | 248 |
| correctly_rejected | 246 |
| incorrectly_rejected | 2 |
| errors_or_no_decision | 0 |
| correctly_routed | 6 |
| incorrectly_routed | 0 |
| reason_strict | 219 |
| reason_family | 21 |
| reason_wrong | 6 |
| touchless rate (ACCEPT / 300) | 15.3% |
| correct touchless rate (correct ACCEPT / 300) | 14.3% |
| touchless accuracy (correct ACCEPT / ACCEPT) | 93.5% |
| decision accuracy | 98.3% |
| accepted but routed to review | 6 |
| posted untouched (clean, ACCEPT and routed AUTO / 45 clean) | 39 (86.7%) |
| posted untouched wrongly (faulty, ACCEPT and routed AUTO) | 1 |

Reason match is scored on correctly rejected invoices: `strict` = the app's reason is the dataset's first-failing reason; `family` = it names another fault the dataset planted on that invoice; `wrong` = neither.

## Per check

Positive class = FAIL (for 4.1, DISCREPANCY). `cascade` follows the dataset README: an expected FAIL counts only where it is the first failing check (a NOT_RUN there is a false negative); an expected PASS counts only where the check was reached. `all findings` scores every check the pipeline actually reached. `balanced` restricts to the dataset's balanced population.

### Cascade-aware, full population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 273 | 40 | 222 | 1 | 10 | 96.0% | 97.6% | 80.0% | 0.4% | 20.0% | 6 | 0 |
| 1.2 customer_verification | 226 | 44 | 178 | 1 | 3 | 98.2% | 97.8% | 93.6% | 0.6% | 6.4% | 3 | 35 |
| 1.3 tax_compliance | 223 | 6 | 213 | 1 | 3 | 98.2% | 85.7% | 66.7% | 0.5% | 33.3% | 1 | 8 |
| 1.4 work_authorization | 130 | 33 | 89 | 0 | 8 | 93.8% | 100.0% | 80.5% | 0.0% | 19.5% | 8 | 36 |
| 1.5 single_document | 206 | 21 | 182 | 0 | 3 | 98.5% | 100.0% | 87.5% | 0.0% | 12.5% | 3 | 0 |
| 2.1 line_items_present | 117 | 14 | 101 | 0 | 2 | 98.3% | 100.0% | 87.5% | 0.0% | 12.5% | 1 | 0 |
| 2.2 po_validation | 92 | 24 | 64 | 2 | 2 | 95.7% | 92.3% | 92.3% | 3.0% | 7.7% | 2 | 0 |
| 3.1 duplicate_detection | 86 | 18 | 68 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 3.2 tax_id_validation | 75 | 6 | 68 | 1 | 0 | 98.7% | 85.7% | 100.0% | 1.5% | 0.0% | 0 | 0 |
| 3.3 future_date_check | 76 | 5 | 71 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.1 total_verification | 56 | 5 | 51 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 56 | 2 | 51 | 0 | 3 | 94.6% | 100.0% | 40.0% | 0.0% | 60.0% | 0 | 0 |
| 4.3 authorization_hours_check | 33 | 7 | 26 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.4 tax_rate_verification | 53 | 0 | 51 | 1 | 1 | 96.2% | 0.0% | 0.0% | 1.9% | 100.0% | 0 | 0 |

### Cascade-aware, balanced population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 96 | 40 | 46 | 0 | 10 | 89.6% | 100.0% | 80.0% | 0.0% | 20.0% | 6 | 0 |
| 1.2 customer_verification | 86 | 44 | 39 | 0 | 3 | 96.5% | 100.0% | 93.6% | 0.0% | 6.4% | 3 | 0 |
| 1.3 tax_compliance | 52 | 6 | 43 | 0 | 3 | 94.2% | 100.0% | 66.7% | 0.0% | 33.3% | 1 | 0 |
| 1.4 work_authorization | 82 | 33 | 41 | 0 | 8 | 90.2% | 100.0% | 80.5% | 0.0% | 19.5% | 8 | 0 |
| 1.5 single_document | 64 | 21 | 40 | 0 | 3 | 95.3% | 100.0% | 87.5% | 0.0% | 12.5% | 3 | 0 |
| 2.1 line_items_present | 40 | 14 | 24 | 0 | 2 | 95.0% | 100.0% | 87.5% | 0.0% | 12.5% | 1 | 0 |
| 2.2 po_validation | 51 | 24 | 25 | 0 | 2 | 96.1% | 100.0% | 92.3% | 0.0% | 7.7% | 2 | 0 |
| 3.1 duplicate_detection | 34 | 18 | 16 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 3.2 tax_id_validation | 28 | 6 | 21 | 1 | 0 | 96.4% | 85.7% | 100.0% | 4.5% | 0.0% | 0 | 0 |
| 3.3 future_date_check | 18 | 5 | 13 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.1 total_verification | 19 | 5 | 14 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 25 | 2 | 20 | 0 | 3 | 88.0% | 100.0% | 40.0% | 0.0% | 60.0% | 0 | 0 |
| 4.3 authorization_hours_check | 32 | 6 | 26 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.4 tax_rate_verification | 20 | 0 | 18 | 1 | 1 | 90.0% | 0.0% | 0.0% | 5.3% | 100.0% | 0 | 0 |

### All findings, full population

| Check | n | TP | TN | FP | FN | Acc | Prec | Recall | FPR | FNR | not run→FN | spurious on N/A |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.1 extraction_success | 267 | 40 | 222 | 1 | 4 | 98.1% | 97.6% | 90.9% | 0.4% | 9.1% | 0 | 0 |
| 1.2 customer_verification | 223 | 44 | 178 | 1 | 0 | 99.6% | 97.8% | 100.0% | 0.6% | 0.0% | 0 | 35 |
| 1.3 tax_compliance | 259 | 34 | 213 | 1 | 11 | 95.4% | 97.1% | 75.6% | 0.5% | 24.4% | 0 | 8 |
| 1.4 work_authorization | 129 | 40 | 89 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 36 |
| 1.5 single_document | 223 | 41 | 182 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 2.1 line_items_present | 116 | 14 | 101 | 0 | 1 | 99.1% | 100.0% | 93.3% | 0.0% | 6.7% | 0 | 0 |
| 2.2 po_validation | 90 | 24 | 64 | 2 | 0 | 97.8% | 92.3% | 100.0% | 3.0% | 0.0% | 0 | 0 |
| 3.1 duplicate_detection | 104 | 36 | 68 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 3.2 tax_id_validation | 77 | 8 | 68 | 1 | 0 | 98.7% | 88.9% | 100.0% | 1.5% | 0.0% | 0 | 0 |
| 3.3 future_date_check | 77 | 6 | 71 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.1 total_verification | 56 | 5 | 51 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.2 line_sum_validation | 56 | 2 | 51 | 0 | 3 | 94.6% | 100.0% | 40.0% | 0.0% | 60.0% | 0 | 0 |
| 4.3 authorization_hours_check | 35 | 9 | 26 | 0 | 0 | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 0 | 0 |
| 4.4 tax_rate_verification | 56 | 3 | 51 | 1 | 1 | 96.4% | 75.0% | 75.0% | 1.9% | 25.0% | 0 | 0 |

## First failing check: expected → actual

| expected → actual | n |
|---|---|
| 1.2->1.2 | 44 |
| none->none | 43 |
| 1.1->1.1 | 40 |
| 1.4->1.4 | 32 |
| 2.2->2.2 | 24 |
| 1.5->1.5 | 21 |
| 3.1->3.1 | 18 |
| 2.1->2.1 | 14 |
| 1.4->3.1 | 8 |
| 4.3->4.3 | 7 |
| 1.1->none | 6 |
| 1.3->1.3 | 6 |
| 3.2->3.2 | 6 |
| 3.3->3.3 | 5 |
| 1.1->1.2 | 4 |
| 1.2->3.1 | 3 |
| 1.5->3.1 | 3 |
| 1.3->1.5 | 2 |
| 2.2->3.1 | 2 |
| 4.2->4.2 | 2 |
| 4.2->none | 2 |
| 1.3->3.1 | 1 |
| 1.4->1.1 | 1 |
| 2.1->2.2 | 1 |
| 2.1->3.1 | 1 |
| 4.2->4.3 | 1 |
| 4.4->none | 1 |
| none->1.4 | 1 |
| none->3.2 | 1 |

## Extraction (vs as-submitted values)

Scored on 222 extracted invoice documents (1 not extracted; non-invoice documents and ingest-level duplicates excluded).

| Group | Accuracy |
|---|---|
| header | 99.3% |
| totals | 99.1% |
| lines | 91.6% |
| tax | 99.8% |
| all header+totals fields correct | 203 docs |

| Field | matched | mismatched | missing | rate |
|---|---|---|---|---|
| cgst_amount | 106 | 0 | 116 | 100.0% |
| customer_name | 222 | 0 | 0 | 100.0% |
| customer_tax_id | 222 | 0 | 0 | 100.0% |
| due_date | 222 | 0 | 0 | 100.0% |
| igst_amount | 90 | 0 | 132 | 100.0% |
| invoice_date | 222 | 0 | 0 | 100.0% |
| invoice_number | 222 | 0 | 0 | 100.0% |
| invoice_total_ex_tax | 200 | 5 | 17 | 97.6% |
| invoice_total_inc_tax | 222 | 0 | 0 | 100.0% |
| line.amount_ex_tax | 729 | 2 | 0 | 99.7% |
| line.description | 731 | 0 | 0 | 100.0% |
| line.hsn_sac | 707 | 24 | 0 | 96.7% |
| line.quantity | 731 | 0 | 0 | 100.0% |
| line.unit_price | 731 | 0 | 0 | 100.0% |
| line_count | 202 | 20 | 0 | 91.0% |
| po_number | 222 | 0 | 0 | 100.0% |
| sgst_amount | 105 | 1 | 116 | 99.1% |
| tax_amount | 204 | 0 | 18 | 100.0% |
| vendor_tax_id | 209 | 13 | 0 | 94.1% |
| work_authorization_reference | 222 | 0 | 0 | 100.0% |

## Timing and cost

| Group | docs | processing s (mean/median/p95) | wall s mean | LLM calls mean | cost USD sum | errors |
|---|---|---|---|---|---|---|
| all | 300 | 67.173/57.65/133.48 | 61.21 | 2.277 | 1.053 | 0 |
| A | 100 | 59.129/53.76/111.06 | 53.548 | 2.391 | 0.33 | 0 |
| B | 100 | 73.468/66.78/141.23 | 67.643 | 2.154 | 0.419 | 0 |
| C | 100 | 68.6/57.1/158.19 | 62.438 | 2.292 | 0.304 | 0 |

Not measured: OCR cost (folded into Gemini cost), infrastructure cost, human review time.
