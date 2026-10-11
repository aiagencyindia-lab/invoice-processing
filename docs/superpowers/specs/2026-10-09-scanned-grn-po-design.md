# Scanned GRN and PO documents (4B) — design

Phase 3, sub-project 4B. Brainstormed with the user 2026-10-09; sections 1–5 approved in turn.

## Goal

Let a warehouse GRN or a supplier/buyer PO that arrives as a PDF (scan or export) feed the three-way match tables built in 4A, after a person confirms what was read. Tenants without an ERP export can then use the match.

## Decisions (user rulings)

1. **A person confirms first.** Extracted documents wait in a review queue; only accepted ones feed the match.
2. **Same intake as invoices.** The classifier tells invoices, GRNs and POs apart; no dedicated upload.
3. **A GRN whose PO is not on file waits** (status `blocked`) until the PO arrives, then becomes `pending`.
4. **ERP upload wins.** A scan can create a PO or update a scanned PO, never change an ERP-uploaded one; a later ERP upload replaces a scanned PO.
5. **Architecture:** branch after classification; a `supporting_documents` review table; acceptance reuses the 4A import validation and storage. (Rejected: treating scans as rejected invoices with a convert button; a second job pipeline.)

## 1. Classification and routing

- Classifier (`ClassifierAgent._analyze_document`, prompt + `DOCUMENT_TYPES` + `_TITLE_MARKERS` keyword fallback) gains `goods_receipt` ("goods receipt", "goods received note", "GRN", "material receipt note", "MRN"). `delivery_challan` stays separate and is NOT receipt evidence. Only `purchase_order` and `goods_receipt` take the new path.
- `process_invoice`: after the classifier, if `summary.document_type ∈ {purchase_order, goods_receipt}` and `invoice_count == 0`, stop and return `{"decision": "SUPPORTING", "document_type": ..., "pdf": <path>}`. A bundle containing an invoice stays on the invoice path.
- Worker `process_document`: on `SUPPORTING`, run the supporting extractor and write a `supporting_documents` row; skip phases, projection, GL coding and approval routing. Set `documents.kind`.
- `documents.kind` (nullable String(16): `invoice` | `purchase_order` | `goods_receipt`) set when classification lands. `GET /api/invoices`, the duplicate-detection corpus (`seen_invoices_for`), the sheet and `iter_case_ids`-based listings exclude non-invoice kinds.
- Redelivery/retry: re-extract and overwrite only a `pending`/`blocked` row; `accepted`/`rejected` rows are never touched.
- Extraction failure (unreadable scan) → `pending` row with empty fields and `extraction_error`, so the clerk can key it in or reject.

## 2. Extraction and grounding

Two schemas + prompts following `_extract_waf` / `call_gemini_with_pdf` (Pro model; transcribe, never compute; unreadable → null):

- `PurchaseOrderExtraction`: `po_number, po_date, valid_from, valid_to, vendor_name, vendor_tax_id, customer_name, currency, total_amount, amount_basis ("inc_tax"|"ex_tax"|null), lines[{line_number, item_code, description, quantity, unit, unit_price, hsn_sac}]`.
- `GoodsReceiptExtraction`: `grn_number, received_on, po_number, vendor_name, received_by, lines[{line_number, item_code, description, quantity_received, unit, remarks}]` — read the accepted/received quantity, not ordered/rejected.
- Cleanup: `extraction_cleanup.fold_homoglyphs` + `read_as_printed` on `po_number`, `grn_number`, `item_code`.
- Grounding: `confidence.locate` / OCR helpers over header fields and each line's quantity (+description) → per-field `{value, grounded, page, confidence}` and `_summary{min_confidence, fields_needing_review, has_text_layer}`. Guidance only; nothing auto-accepts.
- GRN lines are paired to PO lines with `three_way.pair()` (item code → line number → description) when the PO is on file; unpaired lines are flagged and must be fixed before acceptance.

## 3. Review record and acceptance

- Table `supporting_documents` (tenant table, RLS): `id, tenant_id, document_id (FK documents), kind, status (pending|blocked|accepted|rejected), extracted JSON, po_number (normalised), blocked_reason, extraction_error, reviewed_by, reviewed_at, note, created_at, updated_at`. Unique `(tenant_id, document_id)`.
- Blocked: GRN whose PO is not on file. When any PO lands (import, PUT, or accepted scan) the API/worker re-checks blocked GRNs for that normalised PO number → `pending`.
- Accept (clerk+): body = corrected fields + lines → 4A validation (`validate_orders`/`validate_receipts`, `_clean_*`) → GRN: `record_receipts(source="document")` + `queue_rematches`; PO: `upsert_orders(source="document")`, **409 if that PO exists with `source == "upload"`**. Row → `accepted`; `purchase_orders.document_id` / `goods_receipts.document_id` (new nullable columns) point at the PDF; audit `supporting_document_accepted` with before/after of corrected fields.
- Reject (clerk+, reason required) → `rejected`, audited `supporting_document_rejected`.
- A GRN scanned twice: the existing per-tenant `grn_number` uniqueness reports "already recorded" (400) on accept.

## 4. API, UI, errors

Routes (clerk+, no `tenant_id`, probes, 503 without DB, `Field(max_length)`):
- `GET /api/supporting-documents?status=` (default pending+blocked)
- `GET /api/supporting-documents/{id}` (extraction + evidence + paired lines + `document_url`)
- `POST /api/supporting-documents/{id}/accept` `{fields, lines}`; `POST .../reject` `{reason}`
- `GET /api/documents` rows gain `kind`; stage text for PO/GRN docs: "Awaiting confirmation on Purchase orders" / "Confirmed" / "Rejected".

UI: `/purchase-orders` gets "Documents awaiting confirmation" cards (kind, number, vendor, date, confidence, blocked reason) and a review drawer (PDF beside editable header fields, flagged first; editable lines with paired PO line; Accept/Reject; per-row errors as imports show them). Documents page gains a Kind column. PO detail receipts show "from scan · <filename>" linking the PDF.

Fail closed: extraction failure → pending, never dropped; invalid accept → 400 naming every problem, nothing stored; ERP-owned PO → 409 "PO-4471 came from your ERP upload; a scan cannot change it."; re-match enqueue failure → audited, GRN kept; classifier unavailable → invoice path as today.

## 5. Testing and measurement

Tests per task (see Part B). Measurement: benchmark replay — 45 clean invoices byte-identical; the 6 PO NONDOC records change REJECT→SUPPORTING (recorded as deliberate). Scan set: a GRN renderer in the dataset generator (from `po_master.csv`, same `degrade()`), ~20 GRN PDFs + the 6 PO PDFs with ground truth; one real Gemini run committed as a fixture; score header fields and per-line quantities and pairing rate; first measurement, no gate.

Out of scope: auto-accept; routing by email sender; splitting multi-document bundles; challans as receipt evidence.

---

