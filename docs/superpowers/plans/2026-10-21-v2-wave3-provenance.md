# Wave 3 Provenance v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Provenance v2 (FR-22.1 / 22.3): three more deterministic signal families folded into the authenticity score, KYC and proof documents scored through the same ledger, `PROV-KYC-SUSPECT` and `PROV-EDITED`, the vendor-document and on-demand authenticity routes, explain templates, and a `PROV-EDITED` benchmark gate.

**Spec:** `docs/superpowers/specs/2026-10-21-v2-wave3-provenance-design.md`. Program plan B7. Scaffold contracts on `main` @ `d28b919`.

**Tech:** Python 3.11+, pdfplumber (already a dependency), bytes. No new dependencies, no ML, no copy-move.

## Ownership (nothing else is edited)

`invoice_processing/trust/provenance/**` (new `forensics.py`; `__init__.py`, `pdfmeta.py`), `invoice_processing/api/trust_provenance_routes.py`, `invoice_processing/trust/explain/templates/provenance.py`, `benchmark/history/faults/provenance.py`, `tests/test_trust_provenance*.py`, `tests/fixtures/trust_provenance/**`, `tests/fixtures/trust_explain/PROV-KYC-SUSPECT.json`, `tests/fixtures/trust_explain/PROV-EDITED.json`, this plan.

`tests/test_tenant_isolation_api.py::TestWave3StubsIsolation.STUBS` still lists the two scaffold stubs this branch fills (`/api/vendors/{vendor_id}/documents` POST, `/api/trust/documents/{document_id}/authenticity` POST); their 503 assertions fail on this branch by design until the parent session removes the two entries (it owns that file).

## Decisions (builder's calls, recorded)

- **P1 Producer.** Order: the vendor's prior documents (same family: invoices, or KYC of the same subkind, or proofs) include one with the same producer (case-insensitive) → 1.0; a known editor in `/Producer` or `/Creator` → 0.3; a known renderer in either → 0.8; empty → 0.6; any other producer → 0.6 (unrecognised is as informative as empty). A vendor that always produces with an editor matches its own history and is not penalised; an edited copy of a renderer-made invoice changes the producer and is.
- **P2 Edits.** `incremental_updates` = number of `startxref <offset> %%EOF` trailers − 1 (the bare word in page text or an embedded file does not count), one fewer for a linearised file (its first-page section carries its own `startxref`), never below 0. `saves = incremental_updates`, or 1 when there are none and `ModDate − CreationDate ≥ 1 day` (a full rewrite on a later day). `saves ≥ provenance.edits_suspect` → 0.2; `saves ≥ 1` → 0.7; else 1.0.
- **P3 Fonts.** `pdfmeta.facts()["font_names"] = {"body": [...], "amounts": [...]}`, raw names with subset prefixes. An amount line is a line (chars grouped by rounded `top`) holding a money-like number (`\d[\d,]*\.\d{2}`). Mixed = a subset font on an amount line whose base name also appears under a different subset prefix anywhere in the document (the signature of digits re-typed in an editor, which embeds a fresh subset) → 0.3, else 1.0. A bold totals font alone never trips it. No text layer → 0.0 (nothing to read, scored as v1's `text`): with the spec's weights alone a scanned document emailed from a foreign domain moved from suspect (v1 0.42) to not suspect (0.53); this keeps every v1 verdict.
- **P4 PROV-EDITED** on invoice documents only: `incremental_updates ≥ provenance.edits_suspect`, unless both dates are known and the last `ModDate` falls on or before the invoice date (a generator's own saves, e.g. signing, on the day of issue). Only true incremental saves count, not the date inference of P2. Insight, impact None, dedupe `PROV-EDITED:{document_id}`, cites the `document_version` node.
- **P5 KYC/proof.** The upload route writes the `document_version` node (`documents:{id}:{sha}`, attrs `kind`, `subkind`, `vendor_id`, `filename`, `uploaded_by_role`) with `evidences` → the vendor node when it exists; `run()` re-reads that node, scores, and re-draws every open edge into or out of the prior version, any kind (a re-version closes them all), plus `evidences` → vendor for KYC. The reality branch writes the proof's node under the same natural key with the edge to the invoice; provenance keeps it. Channel by uploader role: clerk 0.6, approver/admin 0.8, unknown 0.6. `PROV-KYC-SUSPECT` cites the version, `transaction_node` = vendor node (the version node when the vendor has no node yet). Proofs get no finding of their own (the score reaches the reality branch through the node).
- **P6 Batch run.** A batch (no document) also ledgers every invoice whose document has no open `provenance-2` version node, reading bytes at the content-addressed key `documents/{sha}.pdf`; a missing blob is skipped silently. One parse per document per engine version; this is also the v1 → v2 upgrade of existing nodes. This is what lets the benchmark (a batch run) measure `PROV-EDITED`.
- **P7 Regression (±0.05).** The repo has no PDF fixtures; the "v1 fixtures" are the v1 test's document variants. `tests/fixtures/trust_provenance/v1_scores.json` freezes their v1 scores (computed before the change). Every variant keeps its suspect verdict at the default threshold; the ordinary documents (producer, text layer, consistent metadata) score within ±0.05. Variants missing a v1 family move more by construction (three new families dilute each old one) and the test says so.
- **P7b Duplicate KYC upload** (same bytes): same vendor and subkind → 200 `duplicate` and queued again; another vendor or subkind → 409; a KYC row whose node is missing gets it planted.
- **P8 Authenticity route** scores synchronously and writes the node, never findings (findings come from `trust.evaluate` under the tenant's mode). A non-PDF is 422; a contract or supporting document is 409; an invoice whose node is not synced yet is synced first, as `evaluate` does.
- **P9** The PDF builder (hand-written bytes, incremental saves, subset fonts) lives in `benchmark/history/faults/provenance.py` (`make_pdf`) because the benchmark plants PDFs and cannot import tests; the tests import it and the v1 test's own builder is deleted.

## Tasks (TDD: failing test → minimal code → green → commit)

- [ ] **T1 PDF builder + facts.** Move `make_pdf` into the faults module with `saves=[mod dates]`, `amount_font`, `linearized`; `pdfmeta.facts()` += `incremental_updates`, `font_names`. Tests: counts 0/1/2, linearised, subset fonts per line, pdfplumber still reads an incrementally saved file (its last Info wins).
- [ ] **T2 forensics.py.** `producer(facts, prior_producers)`, `edits(facts, threshold)`, `fonts(facts)`, `saves(facts)`; table tests.
- [ ] **T3 Weighted score v2.** `WEIGHTS` v2 (sum 1), `ENGINE_VERSION="provenance-2"`, `RULE_VERSION="FR-22-v2"`; components carry all seven; v1 regression fixture + test; update v1 expectations that changed by design.
- [ ] **T4 PROV-EDITED.** Raised/not raised per P4; cites the version.
- [ ] **T5 KYC/proof scoring** per P5; `PROV-KYC-SUSPECT`; prior by subkind; edges kept; contract still ignored.
- [ ] **T6 Batch ledger** per P6.
- [ ] **T7 Routes** (`POST`/`GET /api/vendors/{vendor_id}/documents`, `POST /api/trust/documents/{document_id}/authenticity`) + `PROBED` two-tenant probes in `tests/test_trust_provenance_routes.py`.
- [ ] **T8 Explain** templates `PROV-KYC-SUSPECT`, `PROV-EDITED`, `PROV-SUSPECT` names the weakest family when present; fixtures.
- [ ] **T9 Benchmark** `PROV-EDITED` cases + decoys as PDFs under `data_dir/blobs`; `GATES` += `PROV-EDITED {precision 1.0, recall 0.9}`; `PROV-TAMPER` unchanged.
- [ ] **T10 Verify** ruff, full suite, Postgres glob, eval; fable review; one fix pass; PR.

## Review focus

1. A signed (one incremental save) invoice is not `PROV-EDITED`; two saves on the issue day are not either.
2. Re-scoring a proof keeps the reality branch's `evidences` edge to the invoice.
3. The batch run never re-parses a document already at `provenance-2` and never fails on a missing blob.
4. Two-tenant probes: a foreign vendor id or document id is a 404, never a score or a row.
