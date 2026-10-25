# GST/ITC v1 engine (FR-26) — Wave 1 spec

Date: 2026-10-15. Parent: `2026-10-14-v2-trust-layer-design.md` §3.1; scaffold:
`2026-10-15-v2-wave1-scaffold-design.md`. Package `invoice_processing/trust/gst_itc/`,
`ENGINE_VERSION = "gst_itc-1"`. `GST-INACTIVE` and `GST-UNVERIFIED` are hold types.

**Purpose.** Vendor GST standing and identity-sharing signals; an
ITC-at-risk ₹ projection per invoice.

**Inputs.** The vendor's `gstin` (or the invoice's `vendor_tax_id`, checked
with `tax_ids.validate_gstin`); `ectx.transactions()`; vendor nodes with
`resolve.candidates` and `links.canonical_map`; invoice `tax_amount`;
provider `gstin.lookup(gstin)` → `{status: active|cancelled|suspended,
missed_periods, registered_on}`.

**Findings.**
- `GST-INACTIVE` (**hold**): status ≠ active.
- `GST-UNVERIFIED` (**hold**): provider unavailable — fail closed.
- `GST-NONFILER` (advisory): `missed_periods ≥ nonfiler_missed_periods`.
- `GST-NEW-LARGE-FIRST` (advisory): vendor first seen within
  `new_vendor_days` and this amount > `first_invoice_multiple` × the tenant's
  median invoice.
- `GST-SHARED-IDENTITY` (advisory): a candidate pair sharing a
  `bank_account`, `pan` or `email_domain` key with two different non-empty
  GSTINs; dedupe per invoice and sorted pair.
- `GST-ITC-RISK`: impact `tax_amount × max(factor)` over the present signals
  (`risk_inactive 1.0, risk_nonfiler 0.5, risk_shared 0.3, risk_new 0.2`);
  cites the evidence node and the invoice. Emits nothing, and logs the gap,
  when `tax_amount` is unknown.

**Thresholds.** `gst.nonfiler_missed_periods 2, new_vendor_days 90,
first_invoice_multiple 3, risk_inactive 1.0, risk_nonfiler 0.5,
risk_shared 0.3, risk_new 0.2`.

**Benchmark `faults/gst_itc.py`.** The provider script marks vendors
cancelled or non-filer; vendor rows share `bank_account`/`pan` across GSTINs;
a new vendor sends an oversized first invoice. Gates: precision ≥ 0.95,
recall ≥ 0.9, `GST-ITC-RISK` impact_error ≤ 0.01.

**Not possible in Wave 1, stated plainly.** IRN/e-invoice verification (no
IRN field exists), address/place-of-business checks (no vendor address in the
master), 2A/2B reconciliation.

**Non-goals (Wave 1).** A real GSP adapter, an ITC ledger, a PAN validator.
