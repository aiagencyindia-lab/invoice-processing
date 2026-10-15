# Wave 1 — Payment Trust engine (FR-21): implementation plan

Spec: `docs/superpowers/specs/2026-10-15-v2-wave1-payment-design.md` (binding).
Branch `v2/w1-payment`, worktree `.worktrees/payment`. TDD, one commit per task.

## Files

| File | What |
|---|---|
| `invoice_processing/trust/payment/__init__.py` | `ENGINE_VERSION = "payment-1"`, type registration, `run(ectx)`: target invoices, vendor master, provider call, findings |
| `invoice_processing/trust/payment/signals.py` | pure functions: robust z-score, cadence gap, domain folding / look-alike, urgency, bank details in a body, masking of excerpts, stored-email parsing (raw MIME, Postmark JSON, SendGrid multipart) |
| `tests/test_trust_payment.py` | unit tests for `signals`, engine tests through `evaluate()`, the AC-21 replay harness |
| `tests/fixtures/trust_payment/<scenario>/` | `vendor.json, invoice.json, email.eml, expected.json` (+ optional `provider.json`, `history.json`, `bank_change.json`) |
| `benchmark/history/faults/payment.py` | `inject`, `GATES`, `provider_scripts` |

## Rulings (spec silent)

1. **Targets.** Per-document: the document's invoice node only. Batch: every
   invoice node with no incoming `settles` edge (an open invoice); a paid
   historical invoice is not a payment about to happen.
2. **Vendor master.** The invoice's `issued_by` vendor, widened to its
   entity-link component; the `vendors` row (via `ref`) wins, else a history
   vendor record's mapped `bank_account`/`bank_code`, else node attrs.
3. **Provider called only when `bank_verified_at` is None.** A master
   verified by two people is the evidence; name match is checked on the
   provider's answer only. Provider answer contract: `result` (`verified` is
   the only pass), optional `name_match` (0..1) or `account_name` (then
   `difflib` on folded names).
4. **No account on file** → `PAY-UNVERIFIED` without a provider call (fail
   closed), citing invoice + vendor node.
5. **Anomaly history** = the vendor's other transactions (same currency or
   none), all dates. `< min_history` → `first_time_vendor` signal only; z and
   cadence need `min_history`. MAD = 0 and amount > median → z = ∞.
   Cadence = days since the previous invoice vs the median consecutive gap.
6. **BEC signals.** From equal to the vendor domain or a subdomain of it is a
   match. Look-alike also counts as "From ≠ vendor domain" (two signals).
   Body account/IFSC counts only when the master has one and it differs.
   Bank change window measured from the change row's latest of
   `created_at`/`decided_at` (read via `ref`), against `ectx.now`.
   Batch runs use the node's domain attrs and its `document_source_ref` blob.
7. **Impact.** UNVERIFIED, NAME-MISMATCH, BEC: invoice amount. ANOMALY:
   `amount − median` when positive. BEC-SIGNAL: None.
8. Provider settings come from `ectx.settings` (a one-tenant store passed to
   `Registry.call`), never the process-global store.

## Tasks

1. `signals` pure functions — tests: `test_robust_z_flags_five_times_the_median`,
   `test_robust_z_zero_mad`, `test_cadence_gap_far_below_median_is_flagged`,
   `test_lookalike_folds_confusables`, `test_subdomain_is_vendor_domain`,
   `test_urgency_words_found_with_excerpt`,
   `test_body_bank_details_differ_and_excerpt_is_masked`,
   `test_parse_raw_mime`, `test_parse_postmark_json`,
   `test_parse_postmark_raw_email`, `test_parse_sendgrid_multipart`,
   `test_parse_garbage_is_empty`.
2. Engine core through `evaluate()` — `test_unverified_vendor_is_held_citing_evidence`,
   `test_verified_master_makes_no_provider_call`,
   `test_provider_unavailable_fails_closed`,
   `test_name_mismatch_holds`, `test_no_account_on_file_is_unverified`,
   `test_amount_outlier_is_an_anomaly_with_impact`,
   `test_first_time_vendor_is_an_anomaly`,
   `test_batch_checks_only_open_invoices`,
   `test_hold_types_registered`,
   `test_no_raw_account_number_in_findings_or_logs`.
3. Replay harness AC-21 — `test_replay[clean]`, `[lookalike_domain]`,
   `[reply_to_swap]`, `[bank_change_urgency]`, `[unverified_first_payment]`,
   `[provider_down]`.
4. Benchmark `faults/payment.py`: 5× outliers (≤ 2 per vendor, only where 5× the median exceeds twice the vendor's largest invoice) on open invoices, a first
   payment to a new unverified vendor; base vendors get accounts the fake
   provider verifies. Gates `PAY-ANOMALY`, `PAY-UNVERIFIED`: precision ≥ 0.9,
   recall ≥ 0.8, impact_error ≤ 0.1.
5. Verification: ruff, full suite, Postgres (`trust_w1_payment`), eval seed 7.

## Review Focus — the 5 riskiest untested inputs

1. Real Postmark/SendGrid payload variants (HTML-only bodies, `Headers` lists
   with a second Reply-To, quoted-printable bodies with soft breaks).
2. IDN/punycode look-alikes beyond the small confusable map (Greek, Armenian).
3. Account numbers written with separators (`1234-5678-9012`) or split over
   lines; and 10-digit phone numbers next to the word "account".
4. Vendors whose history mixes currencies, or invoices with no date (cadence
   skipped), or very large histories (O(n) per invoice in batch).
5. Other engines' benchmark faults (near-duplicates, new GST vendors) landing
   on open invoices and reading as cadence / first-time anomalies after merge.
