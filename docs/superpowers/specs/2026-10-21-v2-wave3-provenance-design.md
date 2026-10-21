# Wave 3 — Provenance v2 (FR-22.1 / 22.3) design (`v2/w3-provenance`)

**Program plan:** `docs/superpowers/plans/2026-10-21-v2-wave3-program.md`. **Scaffold contracts:** `docs/superpowers/specs/2026-10-21-v2-wave3-scaffold-design.md`. Builder's call B7: more deterministic signal families, no ML, no copy-move.

**Owns:** `invoice_processing/trust/provenance/**` (new `forensics.py`; edits to `__init__.py`, `pdfmeta.py`), `invoice_processing/api/trust_provenance_routes.py` (fills the stub), `invoice_processing/trust/explain/templates/provenance.py`, `benchmark/history/faults/provenance.py`, `tests/test_trust_provenance*.py`, `tests/fixtures/trust_provenance/**`, its plan doc. Nothing else.

## Signal families (`forensics.py`; bytes + pdfplumber only)

- `producer`: generator/editor fingerprint from `/Producer` and `/Creator`. A known editor (PDF-XChange, iLovePDF, Sejda, Smallpdf, Canva, Foxit Editor, …) → 0.3; a known renderer (wkhtmltopdf, Chromium/Skia, ReportLab, Prince, iText, …) → 0.8; empty → 0.6; a producer that matches the vendor's prior document fingerprints → 1.0.
- `edits`: incremental updates = count of `startxref` − 1 (equivalently `%%EOF` occurrences − 1) and `mod_date − creation_date`; ≥ `provenance.edits_suspect` saves → 0.2; one save → 0.7; none → 1.0.
- `fonts`: distinct font names on the lines holding amounts/totals vs the page body; mixed subset fonts on number lines → 0.3, else 1.0.

`WEIGHTS` v2 `{channel .3, template .2, text .1, meta .1, producer .15, edits .1, fonts .05}` (sum 1). `ENGINE_VERSION = "provenance-2"`, `RULE_VERSION = "FR-22-v2"`. `components` keep every family in the `document_version` node and the finding details. `pdfmeta.facts()` gains `incremental_updates` and `font_names`; `MAX_BYTES` / `MAX_PAGES` unchanged.

## KYC and proofs

`run()` scores documents with `document["kind"] in ("kyc", "proof")` with `invoice_node=None` through the same `_ledger`: channel = upload by role (clerk 0.6, approver/admin 0.8), template vs the vendor's prior documents of the same `subkind`. Node `document_version` gets `attrs.kind` and `attrs.subkind`; edge `evidences` → vendor node (kyc) — the proof's edge to the invoice is drawn by the reality branch's route. New types: `PROV-KYC-SUSPECT` (insight, authenticity, `transaction_node` = the vendor node, dedupe `PROV-KYC-SUSPECT:{document_id}`), `PROV-EDITED` (insight, authenticity, raised when an invoice document shows ≥ `provenance.edits_suspect` incremental saves after its issue date). `PROV-SUSPECT` and `PROV-TAMPER` unchanged.

**Routes**: `POST /api/vendors/{vendor_id}/documents` multipart `{file, subkind ∈ gst_certificate|pan|udyam|cancelled_cheque|abn}` (clerk) → `Document(kind="kyc")` stored through the blob store like `_store_contract`, enqueue `trust.evaluate`; `GET /api/vendors/{vendor_id}/documents` (viewer) → rows with the latest authenticity score; `POST /api/trust/documents/{document_id}/authenticity` (viewer; scores now, returns the node attrs — what `/v2/documents/{id}/authenticity` fronts). Two-tenant probes; `tests/test_trust_provenance_routes.py` exports `PROBED`.

## Explain

Templates for `PROV-KYC-SUSPECT` and `PROV-EDITED`; the `PROV-SUSPECT` template reads the new components defensively (v1 nodes lack them).

## Benchmark

`faults/provenance.py` plants edited-after-approval and editor-produced PDFs under `data_dir/blobs`; `GATES += PROV-EDITED {precision 1.0, recall .9}`; the `PROV-TAMPER` gate stays.

## Tests

Forensics on fixture PDFs (incremental-save PDF bytes built in the fixture, readable by pdfplumber); weights sum to 1; every v1 fixture still scores within ±0.05 (regression); KYC scoring path; routes with `PROBED`.

**Done:** gates green; `check()` passes for the new codes; ruff; suite on both dialects.
