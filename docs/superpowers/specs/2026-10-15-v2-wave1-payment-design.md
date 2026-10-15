# Payment Trust engine (FR-21) — Wave 1 spec

Date: 2026-10-15. Parent: `2026-10-14-v2-trust-layer-design.md` §3.1; scaffold:
`2026-10-15-v2-wave1-scaffold-design.md`. Package `invoice_processing/trust/payment/`,
`ENGINE_VERSION = "payment-1"`. Hold types gate through the scaffold's
`payment_hold` in approval and export.

**Purpose.** Hold payment to unverified or impersonated payees; flag
behavioural anomalies.

**Inputs.** Invoice and vendor nodes (the vendor row via `ref` for
`bank_account_number`, `bank_code`, `bank_account_name`, `bank_verified_at`,
`email_domain`); `bank_change` nodes; `ectx.transactions()` for the vendor;
the stored `.eml` via `ectx.blobs.get(document["source_ref"])` when
`document["source"] == "email"` — raw MIME or the Postmark/SendGrid JSON that
ingest stores, both parsed; provider `bank_verification.verify(account, code,
name)`.

**Findings.**
- `PAY-UNVERIFIED` (**hold**): `bank_verified_at` is None and the provider
  does not return `verified`, or is `unavailable`; impact the invoice amount;
  cites the `provider_evidence` node and the invoice.
- `PAY-NAME-MISMATCH` (**hold**): provider name match below `name_match_min`.
- `PAY-ANOMALY` (advisory): robust z-score `(amount − median) / (1.4826 × MAD)`
  > `amount_zscore`, cadence gap < `cadence_ratio_min` × median gap, or a
  first-time vendor with fewer than `min_history` transactions; impact
  `amount − median` when positive; `details.signals`.
- `PAY-BEC` (**hold** at ≥ `bec_hold_signals` signals) / `PAY-BEC-SIGNAL`
  (advisory at one). Signals: From domain ≠ `vendor.email_domain`; look-alike
  domain (`difflib` ratio ≥ `lookalike_ratio` after confusable folding, and
  not equal); Reply-To domain ≠ From domain; urgency words; an account/IFSC in
  the body that differs from the vendor master; a `bank_change` within
  `bank_change_recent_days`. Excerpts pass through `masking.mask_bank`.

**Thresholds.** `payment.amount_zscore 3.0, min_history 5,
cadence_ratio_min 0.25, name_match_min 0.8, bec_hold_signals 2,
lookalike_ratio 0.8, bank_change_recent_days 30`.

**Replay harness (AC-21).** `tests/fixtures/trust_payment/<scenario>/
{vendor.json, invoice.json, email.eml, expected.json}`, parametrised in
`tests/test_trust_payment.py`: clean, look-alike domain, reply-to swap, bank
change + urgency, unverified first payment, provider down.

**Benchmark `faults/payment.py`.** Amount outliers (5×), first payment to an
unverified account (provider unscripted). Gates: precision ≥ 0.9,
recall ≥ 0.8. BEC is proven by the replay harness, not the CSV benchmark.

**Non-goals (Wave 1).** Payment execution, a real verifier, mailbox polling,
a Treasury queue UI, a sheet "hold" column (an enforced hold never reaches
`decision=approve`; export is the second guard).
