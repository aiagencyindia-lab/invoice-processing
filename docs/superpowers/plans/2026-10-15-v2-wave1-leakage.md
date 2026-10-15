# Wave 1 Leakage engine (FR-20): implementation plan

Spec: `docs/superpowers/specs/2026-10-15-v2-wave1-leakage-design.md` (binding).
Branch `v2/w1-leakage`, worktree `.worktrees/leakage`. Strict TDD, one commit per task.

## Files

- `invoice_processing/trust/leakage/__init__.py` — `ENGINE_VERSION`, `register_type` calls, `run(ectx)`, scope filter.
- `invoice_processing/trust/leakage/clauses.py` — candidate spans, regex params, per-page `decide()`, clause nodes and `part_of` edges.
- `invoice_processing/trust/leakage/contracts.py` — contract → vendor, `governed_by`, LEAK-RATE / ESCALATOR / TIER / REBATE / TERM / DISCOUNT, RADAR-*.
- `invoice_processing/trust/leakage/dups.py` — DUP-NEAR / DUP-SPLIT / DUP-CROSS.
- `invoice_processing/trust/leakage/statements.py` — STMT-UNAPPLIED-CREDIT / DOUBLE-PAID / DISPUTED.
- `tests/test_trust_leakage.py` — every test below (one file, so CI's Postgres list covers it).
- `benchmark/history/faults/leakage.py` — `inject`, `seed_graph`, `GATES`.

## Tasks and tests

1. **Skeleton.** `test_types_are_registered_to_leakage_and_none_hold`, `test_empty_graph_finds_nothing`.
2. **Clause params by regex.** `test_params_rate`, `test_params_escalator`, `test_params_tier`, `test_params_rebate`, `test_params_discount_terms`, `test_params_term_dates`, `test_params_unparseable_is_none`, `test_candidates_pick_money_percent_and_keywords`, `test_bank_numbers_are_masked_in_excerpts`.
3. **Clause extraction.** `test_contract_document_extracts_clauses_from_pdf`, `test_model_fallback_writes_no_clause`, `test_max_pages_caps_decide_calls`, `test_reviewed_clause_is_not_overwritten`, `test_model_never_supplies_params`.
4. **Contract rules.** `test_rate_overcharge`, `test_rate_within_tolerance_is_clean`, `test_unmatched_description_is_clean`, `test_escalator_allows_escalated_price`, `test_escalator_overcharge`, `test_tier_price_after_cumulative_qty`, `test_rebate_without_credit`, `test_rebate_with_credit_is_clean`, `test_invoice_outside_term`, `test_governed_by_prefers_covering_contract`, `test_contract_vendor_from_attrs_when_no_governs_edge`, `test_low_confidence_clause_is_ignored`, `test_unreviewed_clause_marks_needs_review`.
5. **Discount.** `test_missed_discount_paid_early_in_full`, `test_discount_taken_is_clean`, `test_late_payment_is_clean`, `test_discount_terms_from_contract_clause`.
6. **Duplicates.** `test_dup_near_normalized_number`, `test_dup_near_edit_distance_needs_equal_amount`, `test_dup_near_outside_window_is_clean`, `test_dup_cross_two_vendor_nodes_one_canonical`, `test_dup_split`, `test_dup_split_off_when_threshold_zero`, `test_same_invoice_live_and_history_is_not_a_duplicate`.
7. **Statements.** `test_unapplied_credit`, `test_applied_credit_is_clean`, `test_double_paid`, `test_disputed_statement_line`.
8. **Radar.** `test_radar_renewal_within_notice`, `test_radar_escalator_anniversary`.
9. **Scope + runtime.** `test_invoice_run_only_returns_its_findings`, `test_evaluate_contract_document_end_to_end`, `test_every_finding_cites_same_tenant_nodes_and_prefixed_dedupe`.
10. **Benchmark.** `faults/leakage.py`; `python -m benchmark.history.eval --seed 7` passes with LEAK-RATE, DUP-NEAR, STMT-UNAPPLIED-CREDIT, LEAK-DISCOUNT gated.

## Review Focus (five riskiest untested inputs)

1. Contract text with Indian digit grouping and mixed currency marks (`Rs. 1,00,000/-`, `₹1.5 lakh`) — the money regex reads digits only, "lakh" words are not parsed.
2. Multi-item rate tables laid out in columns: pdfplumber line order may split item and price across lines, so a rate candidate loses its price and is skipped.
3. Vendor statements with a sign convention other than "credit is negative".
4. Payments without a `settles` edge matched to invoices by normalised number only — a reused number across years could attach a payment to the wrong invoice.
5. A contract node re-versioned (re-upload with a new filename) retires its `part_of` edges; clauses are then invisible until the contract is re-extracted.
