# Synthetic Invoice Validation Dataset (v1.0) — 300 submissions, 3 fictional companies, 12 validation checks

**Reference date for all evaluations: 2026-09-03.** Generator seed: 20260903 (fully reproducible: `python3 generator/gen_export.py`).

Everything here is synthetic. Company names, vendor names, people, addresses, bank details, PANs and GSTINs are invented. GSTINs are *structurally* valid (state code + PAN pattern + entity code + "Z" + the standard mod-36 check character) so that checksum validators behave realistically, but they do not belong to any real taxpayer. Email domains use `.example`.

## Contents

| File | What it is |
|---|---|
| `documents/company_{A,B,C}/<invoice_id>.pdf` | The 300 submission files (100 per company). Filenames are canonical; the "as submitted" filename is in `master_invoice_dataset.csv` (`submission_filename`). |
| `master_invoice_dataset.csv` | One row per submission: identifiers, **as-submitted** header values, page count, difficulty, layout, duplicate linkage, expected final decision, first failing check, fault list. |
| `validation_test_matrix.csv` | Long format: 300 × 14 = 4,200 rows, one per (invoice, check): `expected_result`, fault code, correct/injected values, expected action and rejection reason, `is_first_failing_check`, `in_balanced_population`. |
| `validation_test_matrix_wide.csv` | Same, one row per invoice with 14 check columns (PASS / FAIL / INFORMATIONAL — MATCH / INFORMATIONAL — DISCREPANCY / N_A). |
| `ground_truth.json` | Full **correct** invoice data (lines, taxes, totals, references) plus the **as-submitted** values, every injected fault, and per-check expectations. |
| `fault_manifest.csv` | Every deliberately injected fault: `fault_id`, category, description, correct vs injected value, expected action. Faults inherited by duplicates are flagged. |
| `po_master.csv`, `work_authorization_master.csv`, `vendor_master.csv`, `customer_master.csv` | The fictional reference data the application must validate against. |
| `duplicate_manifest.csv` | All 51 duplicate relationships with pattern, order, filenames and SHA-256 of both files. |
| `evaluation_summary.csv` / `balance_report.txt` | Expected PASS/FAIL/INFO/N_A counts per check for the full population and the balanced population. |
| `qa_report.json` | Results of the 18 final QA assertions (all pass). |
| `generator/` | The four Python modules that produced everything. |

## The three companies

| ID | Legal name | Industry | State | GSTIN |
|---|---|---|---|---|
| A (TECH) | Zentrix Cloudworks Private Limited | Technology / SaaS / IT services | Karnataka (29) | see `customer_master.csv` |
| B (MFG) | Ferrovane Industries Private Limited | Manufacturing / industrial, PO-heavy | Gujarat (24) | " |
| C (SVC) | Lumora Brandcraft LLP | Marketing / consulting / agency, retainer & project billing | Maharashtra (27) | " |

Each company has 10 fictional vendors (service vendors bill hours against Work Authorizations; goods vendors bill against POs), 30 POs (incl. cancelled ones), and — for service vendors — active and expired Work Authorizations. Vendors span 11 states so both intra-state (CGST+SGST) and inter-state (IGST) supplies occur. GST rates used: 12% and 18% (plus 0% exempt lines in specific fault cases).

## Composition of the 300 submissions

| Kind | Count | Notes |
|---|---|---|
| Original tax invoices | 199 | 45 fully clean (all checks PASS); 154 carry 1–5 deliberately injected faults |
| Duplicate resubmissions | 51 | Copies of originals (6 patterns); inherit the original's content faults |
| Non-invoice documents | 50 | 8 receipts, 8 quotations, 8 proforma invoices, 7 credit notes, 6 purchase orders, 5 delivery challans, 8 unreadable scans |

Layouts: 7 distinct vendor templates (different fonts, header placement, A4 vs Letter, per-line vs summary tax presentation, ₹ / Rs. / INR currency styles, Indian vs international digit grouping, 6 date formats). Difficulty: EASY 85 / MEDIUM 127 / HARD 58 (rasterized "scans" with rotation, blur, noise, JPEG artefacts) / ADVERSARIAL 30 (scans plus RECEIVED stamps, handwritten notes, COPY watermarks, extra dates such as quotation/delivery dates, extra totals lines, e-way bill numbers). Page counts: 1 page ×210, 2 ×48, 3 ×25, 4 ×15, 5 ×2 (multi-invoice submissions inflate page counts).

## How each check is evaluated (the rules the expectations were derived from)

Expectations are **computed from the as-submitted data by deterministic rules**, not hand-labelled, so they are reproducible.

| Check | PASS when | Notes |
|---|---|---|
| 1.1 Extraction success | Document is an invoice-type document that can be read | FAIL for receipt, quotation, proforma, credit note, PO, challan, unreadable scan |
| 1.2 Customer verification | Bill-to name and GSTIN equal the configured company | FAIL cases are addressed to one of the other two fictional companies |
| 1.3 Tax compliance | Document is a tax invoice with supplier GSTIN and GST breakup | FAIL: non-tax documents, "Bill of Supply", "INVOICE" without GSTIN/HSN/tax, missing GSTIN, no tax breakup. N_A for unreadable |
| 1.4 Work authorization | Referenced WA exists, belongs to this vendor and customer, status ACTIVE | Applies to service invoices only (N_A for goods). FAIL: missing / non-existent / expired / another vendor's WA |
| 1.5 Single document | Submission PDF contains exactly one invoice | FAIL submissions contain 2–3 invoices from the same vendor merged into one PDF (the extra invoice numbers are in ground_truth.json) |
| 2.1 Line items present | An itemized line table exists | FAIL: single lump-sum line, table omitted, only a grand total |
| 2.2 PO validation | PO exists, belongs to vendor and customer, ACTIVE, invoice date within validity; for goods, line 1 qty ≤ approved qty and unit price = approved price | Applies where a PO is referenced or the vendor is a goods vendor; for service invoices only the reference is validated (qty/price governance is via the WA) |
| 3.1 Duplicate detection | Not previously processed in this company | FAIL: exact PDF, same number different filename, minor format change (re-rendered in a different layout), uploaded twice, metadata changed, same vendor+number re-rendered. Duplicates always come *after* their original in `submission_seq` |
| 3.2 Tax ID validation | GSTIN present, checksum-valid, and equals this vendor's master GSTIN | FAIL: bad check character, altered digit, another vendor's GSTIN, structurally valid but unknown GSTIN, missing. N_A when 1.3 already removed the GSTIN |
| 3.3 Future date | invoice_date ≤ 2026-09-03 | FAIL dates range 2026-09-04 … 2026-11-02 |
| 4.1 Total verification (INFORMATIONAL) | stated subtotal − discount + tax + other charges + round-off = stated grand total | Never rejects. DISCREPANCY cases alter only the stated total. N_A when only a total is shown |
| 4.2 Line sum validation | Σ line totals = stated subtotal, and each line's qty × rate − discount = line total | FAIL: altered line total, qty changed without total, wrong line discount, wrong subtotal (with tax/total cascaded from it), a line omitted from the table |
| 4.3 Authorization hours | Σ invoiced hours ≤ approved hours, hourly rate = authorized rate, project matches, service period within WA period | Only when 1.4 passed and lines are itemized |
| 4.4 Tax rate verification | stated tax within ±1.00 of (stated taxable amount apportioned by line value × statutory rate per HSN/SAC) + tax on other charges, AND components consistent with place of supply (CGST=SGST, IGST=0 intra-state; IGST only inter-state) | FAIL: wrong rate, wrong amount, wrong base, CGST/SGST↔IGST mismatch, tax on an exempt 0% line, rounding beyond tolerance. N_A when not itemized or no tax shown |

**Cascading.** Real pipelines stop at the first failing check. `first_failing_check` and `expected_final_decision` in the master file give the end-to-end expectation. The per-check matrix, however, records what each check *would* return on the document's content, so an application that runs all checks (or reports all findings) can be scored per check. If your pipeline stops at the first failure, score check X only on invoices where X is the first failing check or X passes (`is_first_failing_check` column), and use the full matrix for the "all findings" mode.

**Balanced population.** For every check, `in_balanced_population = true` marks an equal number of PASS and FAIL invoices (41–52 per side, spread across the three companies). Use it to compute unbiased per-check accuracy; use the full population for realistic base rates.

## Suggested metrics

- Extraction accuracy: field-level match of extracted values vs `ground_truth.json → as_submitted` (what is printed) — and vs `correct` if you want to measure "did the system also spot the fabricated value".
- Validation accuracy per check: agreement with `expected_result` over applicable invoices (or the balanced population).
- Rejection accuracy: `expected_final_decision` and `expected_rejection_reason` vs the application's decision/reason.
- False positive rate: clean invoices (45) or per-check PASS cases wrongly rejected. False negative rate: FAIL cases accepted.
- Touchless accuracy: share of the 45 clean invoices accepted with all fields correct.
- Calculation accuracy: 4.1/4.2/4.4 agreement; exception detection accuracy: recall over `fault_manifest.csv`.

## Design notes and caveats

- 12 blocking checks × ~50 FAIL cases cannot fit in 300 documents with one fault each, so 154 faulty originals carry 1–5 faults (mean ≈ 3). Combinations that would make a fault unobservable or introduce an unintended failure are excluded by construction (e.g. an un-itemized invoice cannot also be tested for line-sum or tax-rate errors), and the exporter asserts that no clean case fails any check.
- Duplicates inherit their original's faults; the duplicate's own primary fault is 3.1.
- GST rounding: some invoices carry a "Round Off" line to whole rupees, as is common in India; 4.1 accounts for it.
- The dataset is Indian-GST-centric (₹, GSTIN, CGST/SGST/IGST, HSN/SAC). Company A's vendors include inter-state and SaaS subscription patterns; Company B is PO/GRN-style goods; Company C is retainer/project services.
