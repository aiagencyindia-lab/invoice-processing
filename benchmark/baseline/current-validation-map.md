# The 14 checks, as implemented at `260d7e9`

One row per check: where it runs, what it reads, what switches it off, the exact
string a vendor is told — and what this dataset asks of it. The last column is
the gap the BEFORE run is expected to measure; the measured result lives in
`benchmark/baseline/report.md`, not here.

All line references are `invoice_processing/shared_libraries/acting/general_invoice_agent.py`
("GIA") unless stated. The rulebook that declares each step is
`invoice_processing/shared_libraries/invoice_master_data.yaml:190-318`; the
catalogue the settings screen reads is derived from it (`checks.py:29`).

## Mechanics common to every check

- **Config resolution.** `_FALLBACK_CONFIG` (GIA:207) ⊕ rulebook `config_defaults`
  (yaml:454) → `DEFAULT_CONFIG`, then this tenant's overrides layered on by
  `tenant_settings.apply_to_pipeline` (`tenant_settings.py:267`), which also
  injects `purchase_order_file`. Only nine keys are tenant-settable
  (`tenant_settings.py:29`): `organization_names`, `duplicate_check_enabled`,
  `require_purchase_order`, `require_work_authorization`, `disabled_checks`,
  `po_tolerance_pct`, `po_tolerance_abs`, `high_value_threshold`,
  `rules_binding`. **`tax_id_format`, the tax-rate table and every tolerance are
  not among them** — they are master data, identical for every tenant.
- **Failure.** `_settle` (GIA:1034) is the only definition: `passed is False and
  not informational`. It also rewrites any step in `disabled_checks` to
  `skipped: True, passed: None` — a check nobody ran gets no verdict.
- **Short-circuit.** A phase that fails writes its artifact and stops the
  pipeline (`_run_pipeline:2235` → `_handle_phase_rejection:2159`), and only
  `failed[0]` reaches `08_decision.json` (:1191). Every later check has no
  record at all. `rejection_reason` is propagated from Phase 1 only (:1194), so
  a Phase-3 rejection carries a template and a null reason.
- **Absent ≠ passed.** Several checks emit no entry when their input is missing:
  3.2 without `_tax_id_validation`, 3.3 with an unparseable date, 2.2 with no PO
  and no requirement, 1.4/4.3 when work authorization is not required.

## Phase 1 — Initial intake (`Phase1ValidatorAgent.run`, GIA:1072 → `03_phase1_validation.json`)

| # | Implementation | Reads | Gate | Rejection string | What the dataset asks | Gap |
|---|---|---|---|---|---|---|
| **1.1** Extraction success | GIA:1078 | `extraction.extraction_failed` / `extraction_error`, set by the extractor (GIA:838) | none — mandatory, cannot be disabled (`checks.py:29`) | `Document is not a valid Tax Invoice` | FAIL for receipt, quotation, proforma, credit note, purchase order, delivery challan, unreadable scan (50 documents) | The classifier knows only `has_invoice` / `has_work_authorization` (`DocumentContent`, GIA:308). Any document with an invoice-shaped header extracts successfully, so a receipt passes 1.1. |
| **1.2** Customer verification | GIA:1101 | `invoice.customer_name` vs config `organization_names`, folded to alphanumerics by `_fold_name` (GIA:1017), substring match | empty `organization_names` ⇒ vacuous pass | `Invoice addressed to different company` | Bill-to **name and GSTIN** must equal the configured company | Name-only. There is no `customer_tax_id` in the extraction schema, so the GSTIN half cannot be checked. |
| **1.3** Tax compliance | GIA:1126 | presence of `vendor_tax_id`, `invoice_date`, `vendor_name` (≠ `"UNKNOWN"`) | none | `Document is not a Tax Invoice` | FAIL for a Bill of Supply, an "INVOICE" without GSTIN/HSN/tax, a missing GSTIN, or no tax breakup | Presence of three fields only. A Bill of Supply that names its supplier passes. |
| **1.4** Work authorization | GIA:1147 | `extraction.waf_count` > 0; work type from line-item keywords (`_determine_work_type`, GIA:1204); exemptions from `waf_exempt_work_types` | `require_work_authorization` (default off) | `Missing Work Authorization Form` | WA reference on the invoice must exist in the master, belong to this vendor and customer, and be ACTIVE; N/A for goods vendors (153 of 300) | Two gaps. `waf_count` counts **WA forms found inside the submission PDF**, and the worker materialises only `invoice.pdf` (`worker/runner.py:212`) — so it is always 0. `WorkAuthorizationExtraction.reference_number` exists (GIA:377) and nothing reads it; there is no WA master anywhere. Also `_determine_work_type` matches `"pm"` as a bare substring, so "Develo**pm**ent" classifies as PREVENTATIVE and exempts itself. |
| **1.5** Single document | GIA:1167 | `extraction.invoice_count` (sum of the classifier's per-file counts, GIA:723) | none | `Multiple invoices in submission` | FAIL where 2–3 invoices from one vendor are merged into one PDF (46 cases) | Depends entirely on the Flash classifier counting invoice headers; no structural check. |

## Phase 2 — Content (`Phase2ValidatorAgent.run`, GIA:1240 → `04_phase2_validation.json`)

| # | Implementation | Reads | Gate | Rejection string | What the dataset asks | Gap |
|---|---|---|---|---|---|---|
| **2.1** Line items present | GIA:1246 | `len(invoice.line_items)` | none | `Invoice charges not itemized` | FAIL for a lump-sum line, an omitted table, or a grand total only | Matches. |
| **2.2** PO validation | GIA:1260, logic in `po_matching.py` | tenant PO file (`paths.purchase_order_file`, `paths.py:64`), records `{po_number, vendor_name, total_amount, currency, status, amount_invoiced}` (`po_matching.py:65`); compares `invoice_total_inc_tax` | runs when `require_purchase_order` **or** the invoice quotes a PO | seven specific strings (`po_matching.py:351`) — `Purchase order not recognised`, `Purchase order belongs to a different supplier`, `Invoice exceeds purchase order value`, … The rulebook still declares the generic `Invalid purchase order` (yaml:249), which the code never emits | PO must exist, belong to vendor **and customer**, be ACTIVE, invoice date within `valid_from`/`valid_to`; for goods, line 1 quantity ≤ approved and unit price = approved | Header-level two-way match only: no customer, no validity window, no quantity, no unit price. And the comparison is inc-tax against a PO value the dataset states ex-tax, so a correct 18 % GST invoice over-bills its PO by construction. |

## Phase 3 — External (`Phase3ValidatorAgent.run`, GIA:1414 → `05_phase3_validation.json`)

| # | Implementation | Reads | Gate | Rejection string | What the dataset asks | Gap |
|---|---|---|---|---|---|---|
| **3.1** Duplicate detection | GIA:1420, logic in `duplicates.py` | `InvoiceKey` of vendor name/tax id, invoice number, date, total (`duplicates.py:55`); corpus from this tenant's projected cases (`worker/runner.py:129`) | `duplicate_check_enabled` (default on) | `Invoice already processed` | 51 duplicates in 6 patterns, always submitted after their original | Three tiers: `exact` and `normalized` block, `fuzzy` only warns (`passed: True`, evidence "review recommended, not blocking", `duplicates.py:276`). Separately, **27 of the 51 are byte-identical and are refused at ingest** by the SHA-256 check (`api/ingest.py:159`) before the pipeline runs — real detection, but at a different seam, so the benchmark records it as `outcome: file_hash`. `file_sha256` on the key itself is never populated by this pipeline. |
| **3.2** Tax ID validation | computed in the extractor GIA:849, rendered at GIA:1434 | `invoice.vendor_tax_id` through `tax_ids.validate(id, config["tax_id_format"])` | `validate_tax_id_checksum`; **no entry at all** when the key is absent | `Vendor tax ID invalid` | GSTIN present, checksum valid, **and equal to this vendor's master GSTIN** | `tax_id_format` is `"ABN"` for every tenant and is not settable, so a 15-character GSTIN is rejected as `Invalid length: 15 (must be 11)` — right answer, wrong reason, and it rejects the 200 valid ones too. The GSTIN validator is registered but self-flagged UNVERIFIED (`tax_ids.py:112`); it in fact validates all 33 of this dataset's master GSTINs and rejects all 23 bad-checksum/altered-digit faults. The remaining 18 faults (another vendor's GSTIN, a structurally valid unknown one) need a vendor master, which does not exist. |
| **3.3** Future date | GIA:1449 | `parse_date(invoice.invoice_date)` (GIA:634, five formats) vs `date.today()` | none; **no entry** when the date will not parse | `Invoice is future dated` | `invoice_date ≤ 2026-09-03`; 48 future-dated cases | The reference date is `date.today()` with no injection point, so the boundary moves every day and the result is not reproducible. The dataset prints seven date formats; `parse_date` knows five. |

## Phase 4 — Calculations (`Phase4ValidatorAgent.run`, GIA:1499 → `06_phase4_validation.json`, terminal `ACCEPT`)

| # | Implementation | Reads | Gate | Rejection string | What the dataset asks | Gap |
|---|---|---|---|---|---|---|
| **4.1** Total verification | GIA:1506 | `\|inc − (ex + tax)\| ≤ balance_tolerance` (0.02) | none | **none — `informational: True`**, excluded from failures by `_settle` | `subtotal − discount + tax + other charges + round-off = grand total`; never rejects | Correctly informational. The formula ignores discount, other charges and round-off, none of which are in the extraction schema; Indian invoices routinely carry a round-off line. |
| **4.2** Line sum | GIA:1541 | `\|Σ line.amount_ex_tax − invoice_total_ex_tax\| ≤ line_sum_tolerance` (1.00) | none | `Invoice amounts do not reconcile` | Σ line totals = subtotal **and** per line qty × rate − discount = line total | The per-line arithmetic is not checked, so a quantity changed without its total is invisible unless the subtotal moves too. |
| **4.3** Authorization hours | GIA:1561 | labour hours by keyword (`_calculate_labour_hours`, GIA:1683) vs `extraction.work_authorization.authorized_hours` | `require_work_authorization` (default off) | `Invoice does not match work authorization` | Σ hours ≤ approved, rate = authorized rate, project matches, service period within the WA period | Hours only, and only from a WAF document that this deployment never has. Rate, project and period have nowhere to come from. |
| **4.4** Tax rate | GIA:1613 | single rate from `tax_rates_by_currency[currency]` else `default_tax_rate`; `\|tax − round(ex × rate, 2)\| ≤ balance_tolerance` (0.02) | none | `Invoice tax amount incorrect` | Stated tax within ±1.00 of per-HSN statutory rates apportioned by line value, plus tax on other charges, **and** components consistent with place of supply (CGST = SGST, IGST = 0 intra-state; IGST only inter-state) | No INR entry in the rate table, so every invoice is measured against 10 %; a correct 18 % GST invoice fails. `TaxBreakdownLine` (GIA:329) is extracted and ignored. CGST/SGST/IGST, HSN/SAC and place of supply appear nowhere in the package. The tolerance is 0.02 where the dataset allows 1.00. |

## Outcomes other than ACCEPT/REJECT

- `SET_ASIDE` / "To Verify" exists in the status map (GIA:2015) but this
  pipeline never produces it.
- Confidence routing (`confidence.py:357`) writes `AUTO` or `REVIEW` into
  `invoice["_routing"]` from per-field text-layer grounding. **Advisory only** —
  no phase reads it, and it cannot change a decision. The benchmark reports how
  many accepted invoices were flagged `REVIEW` alongside the touchless rate.
- A fuzzy duplicate passes with a "review recommended" note.
- A disabled check is `skipped`, distinct from both pass and fail.

## Instrumentation

`_metrics_store` (GIA:275) counts LLM calls, tokens and cost per case, reset in
`process_invoice` (GIA:2364) — process-global, which is why the benchmark runs
one case at a time per process. It undercounts by one: the PDF-to-text Flash
call (`extract_pdf_with_gemini`, GIA:442) never touches the metrics. Timing and
cost land in `09_audit_log.json` and, through `case_projection.run`, in
`pipeline_runs`. There is **no deadline and no retry** on any Vertex call
(GIA:455, :565, :608); the only throttle is a 1 s sleep after each call.
