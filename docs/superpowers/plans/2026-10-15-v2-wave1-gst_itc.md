# GST/ITC v1 engine (FR-26) — implementation plan

Spec: `docs/superpowers/specs/2026-10-15-v2-wave1-gst_itc-design.md` (binding).
Branch `v2/w1-gst_itc`, package `invoice_processing/trust/gst_itc/`,
`ENGINE_VERSION = "gst_itc-1"`.

## Shape

One module, `trust/gst_itc/__init__.py`: `register_type` for the six types,
`run(ectx)`. Per run it builds, once: invoice nodes (one in a per-document
run, all in a batch), `issued_by` vendor, `links.canonical_map`, the tenant
median and each canonical vendor's first-seen date from
`ectx.transactions()`, and the shared-identity pairs from
`resolve.candidates`. Provider lookups are cached per GSTIN within a run.

Per invoice:

| Signal | Rule | Finding |
|---|---|---|
| GSTIN | vendor node `gstin`, else canonical vendor's, else invoice `vendor_tax_id`; normalised; only a 15-character value is treated as a GSTIN | none when absent |
| bad checksum | `tax_ids.validate_gstin` false | `GST-UNVERIFIED` (hold), cites vendor/invoice |
| provider | `ectx.providers.call(ctx, "gstin", gstin)`; `unavailable` or no recognised `status` | `GST-UNVERIFIED` (hold), cites evidence + invoice |
| status ≠ active | | `GST-INACTIVE` (hold), factor `gst.risk_inactive` |
| `missed_periods ≥ gst.nonfiler_missed_periods` | | `GST-NONFILER`, factor `gst.risk_nonfiler` |
| invoice date − vendor first seen ≤ `gst.new_vendor_days` and amount > `gst.first_invoice_multiple` × tenant median | | `GST-NEW-LARGE-FIRST`, factor `gst.risk_new` |
| candidate pair sharing `bank_account`/`pan`/`email_domain`, two different non-empty GSTINs, not one canonical entity | | `GST-SHARED-IDENTITY` per invoice and sorted pair, factor `gst.risk_shared` |
| any factor present and `tax_amount` known | impact = `tax_amount × max(factor)` (Decimal) | `GST-ITC-RISK`; unknown tax → nothing, one log line with a count |

Dedupe keys: `<TYPE>:<invoice node>` (shared identity adds `:<a>:<b>`).

## Tasks (TDD, one commit each)

1. Plan (this file).
2. Provider verdicts — `tests/test_trust_gst_itc.py`:
   `test_active_vendor_yields_no_findings`,
   `test_cancelled_gstin_is_an_inactive_hold_with_full_itc_at_risk`,
   `test_provider_unavailable_fails_closed_as_unverified`,
   `test_unknown_provider_answer_is_unverified`,
   `test_bad_checksum_is_unverified_without_calling_provider`,
   `test_non_gstin_tax_id_is_skipped`,
   `test_nonfiler_at_threshold_is_advisory_with_half_factor`,
   `test_types_are_registered_with_holds`.
3. History signals: `test_new_vendor_large_first_invoice`,
   `test_new_vendor_normal_invoice_is_quiet`,
   `test_shared_pan_across_gstins_is_flagged_once_per_invoice_and_pair`,
   `test_accepted_link_suppresses_shared_identity`,
   `test_itc_risk_uses_max_factor`,
   `test_unknown_tax_emits_no_itc_risk_and_logs_without_ids`,
   `test_batch_run_covers_every_invoice`, `test_threshold_override_applies`,
   `test_evaluate_enforcing_holds_and_clean_rerun_releases` (through `evaluate`).
4. Benchmark `benchmark/history/faults/gst_itc.py`: cancelled, non-filer,
   shared-PAN pair, new vendor oversized first invoice; `seed_graph` writes
   18% `tax_amount` onto the family's invoices (history CSVs carry no tax)
   and fills `provider_scripts` from the vendor file; gates per spec.

## Review Focus (riskiest inputs not covered by a test)

1. A GSTIN whose provider answer has `status` in an unexpected case or
   spelling (`"Active"`, `"ACT"`) — normalised by lower-case only.
2. An invoice whose `issued_by` vendor is retired mid-run by a concurrent
   sync (edges closed): the invoice is treated as vendor-less.
3. A tenant with tens of thousands of vendors sharing one `email_domain`
   (e.g. `gmail.com`): candidate blocking is n² inside that bucket.
4. `tax_amount` stored negative (credit notes): impact goes negative.
5. Two GSTINs on the vendor and invoice that disagree: the vendor's wins.
