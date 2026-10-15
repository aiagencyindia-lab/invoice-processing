# Provenance v1 (FR-22 part) — implementation plan

Spec: `docs/superpowers/specs/2026-10-15-v2-wave1-provenance-design.md`.
Branch `v2/w1-provenance`. Package `invoice_processing/trust/provenance/`.

## Files
- `invoice_processing/trust/provenance/__init__.py` — `ENGINE_VERSION`, type
  registration, `run(ectx)`.
- `invoice_processing/trust/provenance/pdfmeta.py` — PDF facts (metadata,
  pages, fonts, text), PDF date parser, fingerprint.
- `tests/test_trust_provenance.py` — PDF facts, engine (graph-backed, PDFs
  built by hand in the test) and one end-to-end `evaluate()` + gate test. One
  file, so CI's Postgres list (exact name) runs all of it.
- `benchmark/history/faults/provenance.py` — `inject`, `seed_graph`, `GATES`.

## Tasks (TDD, one commit each)
1. **PDF facts.** Tests: `test_facts_read_metadata_pages_fonts_and_text`,
   `test_subset_prefix_is_stripped_from_font_names`,
   `test_pdf_date_parses_offsets_and_partial_forms`,
   `test_fingerprint_ignores_page_count_and_text_but_not_fonts_or_producer`,
   `test_non_pdf_bytes_give_none`.
2. **Ledger and score.** Tests: `test_ledger_node_and_evidences_edge`,
   `test_score_components_email_from_vendor_domain`,
   `test_channel_values_by_source`, `test_template_is_one_below_min_history`,
   `test_template_is_share_of_matching_prior_fingerprints`,
   `test_meta_zero_when_dates_out_of_order_or_no_producer`,
   `test_suspect_below_threshold_cites_the_version`,
   `test_non_pdf_document_is_skipped_and_logged`,
   `test_contract_document_is_ignored`.
3. **Tamper.** Tests: `test_tamper_when_an_approved_twin_has_another_sha`,
   `test_no_tamper_for_same_sha_or_unapproved_twin_or_other_vendor`,
   `test_approval_step_counts_as_approved`,
   `test_batch_run_finds_tamper_across_the_tenant`,
   `test_evaluate_end_to_end_holds_in_enforcing` (through `evaluate()` and
   `gate.payment_hold`).
4. **Benchmark.** `faults/provenance.py`; eval gate `PROV-TAMPER` precision =
   recall = 1.0 including decoys (same sha, other vendor).

## Rulings
- A batch run (`document is None`) checks tamper only; the ledger and score need
  a document's bytes.
- A non-PDF or unreadable document gets no ledger node and no score (logged);
  the tamper check still runs for its invoice.
- Font names drop the 6-letter subset prefix (`ABCDEF+Arial` → `Arial`);
  page sizes are the sorted set of distinct sizes rounded to whole points.
- A missing ModDate counts as equal to CreationDate; a missing CreationDate or
  Producer makes `meta` 0.
- Email from a domain other than the vendor's counts as "other" (0.3).
- Template history is found through the invoice's `document_id` attr, not the
  `evidences` edge (the sync closes edges when an invoice node re-versions).
- A missing blob raises: evaluate() then fails closed in enforcing
  (`TRUST-ENGINE-FAILED`), the scaffold's contract for a hold engine.
- Ledger attr `channel` is the source name; the 0–1 value is in
  `components.channel`. Attr `template` is `{prior, matching}`.
- `PROV-SUSPECT` dedupes per document id; `PROV-TAMPER` per invoice natural
  key (stable across node versions, so a clean run can release it).
- Benchmark pairs use `PRV-*` numbers, no dates and unequal amounts, so sibling
  engines' duplicate rules never see them.
- "Approved" = invoice `decision == "approve"` or an `approval` node with
  status `approved` on it.

## Review Focus (riskiest inputs not fully covered)
1. Real-world PDFs with odd metadata (XMP-only, bytes values, garbage dates).
2. Encrypted PDFs (pdfplumber may raise; treated as unreadable → skipped).
3. Vendors split across unlinked vendor nodes (tamper missed until resolved).
4. Invoice numbers that normalise to "" (skipped — never matched).
5. Very large tenants: per-document run scans all invoice and version nodes.
