# Scanned GRN and PO Documents (4B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task, ONE implementer at a time. Steps use checkbox (`- [ ]`) syntax.

**Goal:** PO and GRN PDFs are classified, extracted, queued for confirmation, and on acceptance feed the 4A match tables.

**Architecture:** classifier branch in `process_invoice` → worker runs `supporting_docs.extract()` → `supporting_documents` row → review API/UI → acceptance via 4A import validation + repositories.

**Tech Stack:** Python 3.12 (`./.venv/bin/python`), FastAPI, SQLAlchemy 2 + Alembic (Postgres RLS / SQLite), Vertex Gemini via existing `call_gemini_with_pdf`, Next.js 16, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-09-scanned-grn-po-design.md`

## Global Constraints

- HANDOFF10 §4 workflow: ONE implementer at a time; TDD (prove RED via `git stash push -- invoice_processing` or by running before the code exists); commit per task, imperative *why* subject + body + trailer `Co-Authored-By: Claude <model that wrote it> <noreply@anthropic.com>`.
- Controls fail closed. No endpoint takes `tenant_id`; `require_role`; actors `principal.email`; `Field(max_length=…)` on stored strings; refusals 403/409 sentences; DB-only routes 503 "…needs a database…" (UI string-matches "needs a database"); every route has a cross-tenant probe + `PROBED` entry in `tests/test_tenant_isolation_api.py` and new route modules go in `test_no_endpoint_reads_a_tenant_from_the_request`.
- RLS: new table in `TENANT_TABLES` and the migration's own `RLS_TABLES`; repositories filter `tenant_id` explicitly; never edit an applied migration (head `c4d7e8f91a02`); JSON columns `sa.JSON().with_variant(postgresql.JSONB(), "postgresql")`; timestamps via `repositories._iso`.
- DB-backed test files on the CI Postgres list (`.github/workflows/ci.yml` ~L157); extend the post-upgrade forced-policy test (`tests/test_gcs_and_migrations.py` ~L391) and the RLS-list parametrize (~L333).
- No real Gemini in tests (fake `call_gemini_with_pdf` / `call_gemini` as `tests/test_extraction_cleanup.py:245` does). Local runs set `CLOUD_SQL_INSTANCE="" GCS_BUCKET="" PUBSUB_TOPIC=""`.
- Disk nearly full: no worktrees, no `npm ci` (use `frontend/node_modules`).
- Audit event names must map in `repositories._ENTITY_TYPES` (`entity_type_for` matches head-before-last-underscore then first word): add `"supporting_document": "supporting_document"`.

## File Structure

| File | Responsibility |
|---|---|
| `invoice_processing/db/migrations/versions/d8e1f2a3b4c5_supporting_documents.py` | `supporting_documents` table (RLS); `documents.kind`; `purchase_orders.document_id`, `goods_receipts.document_id` |
| `invoice_processing/db/models.py` | `SupportingDocument` ORM; `Document.kind`; two FK columns; `TENANT_TABLES` |
| `invoice_processing/db/repositories.py` | `PgSupportingDocumentRepository`; `PgDocumentRepository.set_kind`; `document_id` in order/receipt dicts |
| `invoice_processing/adapters.py` | `JsonDocumentRepository.set_kind`; `JsonSupportingDocumentRepository` (file fallback, minimal) |
| `invoice_processing/supporting_docs.py` | Schemas, prompts, `extract(pdf_path, kind, *, call=call_gemini_with_pdf) -> dict`, cleanup + grounding, GRN-line pairing helper, `accept_payload_to_import(kind, fields, lines) -> dict` |
| `shared_libraries/acting/general_invoice_agent.py` | `goods_receipt` type; `SUPPORTING` branch in `_run_pipeline` |
| `invoice_processing/worker/runner.py` | `process_document` SUPPORTING tail; `unblock_receipts(tenant, po_number)` helper call sites |
| `invoice_processing/api/supporting_routes.py` | list / detail / accept / reject |
| `invoice_processing/api/service.py` | `get_supporting_documents()` accessor; router; `list_invoices` kind filter |
| `invoice_processing/api/system.py` | `GET /api/documents` `kind` + stage text |
| `invoice_processing/api/po_routes.py`, `config_routes.py` | call `unblock_receipts` after PO import/PUT; `source == "upload"` ownership check exposed as `po_owned_by_upload` |
| `frontend/src/lib/api.ts`, `frontend/src/app/purchase-orders/page.tsx`, `frontend/src/components/SupportingReview.tsx`, `frontend/src/app/documents/page.tsx` | Queue cards, review drawer, Kind column |
| `scripts/e2e_api.py`, `frontend/e2e/golden-path.spec.ts` | seeded pending GRN scan; e2e tests |
| `benchmark/dataset/generator/gen_render.py`, `tests/fixtures/supporting_docs/`, `tests/test_supporting_docs_measured.py`, `benchmark/CHANGELOG.md` | GRN renderer, scan set, measured fixture |

---

### Task 1: Schema — `supporting_documents`, `documents.kind`, `document_id` links

**Files:** migration `d8e1f2a3b4c5_supporting_documents.py` (down_revision `c4d7e8f91a02`, `RLS_TABLES = ["supporting_documents"]`); `db/models.py`; tests `tests/test_supporting_document_repository.py` (new), `tests/test_gcs_and_migrations.py`.

**Produces:**
```python
class SupportingDocument(Base, TenantMixin, TimestampMixin):
    __tablename__ = "supporting_documents"
    __table_args__ = (UniqueConstraint("tenant_id", "document_id", name="uq_supporting_documents_document"),)
    id String(36) pk; document_id String(36) FK documents.id not null index
    kind String(16) not null            # purchase_order | goods_receipt
    status String(16) not null default "pending"   # pending | blocked | accepted | rejected
    extracted JSONType not null default {}
    po_number String(128) nullable index   # normalised
    blocked_reason String(500); extraction_error String(500)
    reviewed_by String(320); reviewed_at DateTime(tz); note String(500)
Document.kind: Mapped[str | None] = mapped_column(String(16))
PurchaseOrder.document_id / GoodsReceipt.document_id: String(36) FK documents.id nullable
```
Migration adds the table (+RLS on Postgres), `op.add_column` for the three columns (SQLite batch mode if needed — check how earlier migrations add columns, e.g. `a3e6d2f81c57` decided_at). Downgrade reverses.

- [ ] Failing tests: table in `TENANT_TABLES` and built by `create_all`; `documents.kind` exists; parametrize `("d8e1f2a3b4c5", 1)`; add `supporting_documents` to the forced-policy table list.
- [ ] Implement; `alembic upgrade head && downgrade base` on scratch SQLite; full suite; ruff.
- [ ] Add the new test file to the CI Postgres list. Commit `"Store scanned purchase orders and receipts awaiting confirmation"`.

### Task 2: Repositories

**Files:** `db/repositories.py`, `adapters.py`, tests `tests/test_supporting_document_repository.py`.

**Produces:**
```python
class PgSupportingDocumentRepository:
    def upsert_from_extraction(self, tenant_id, *, document_id, kind, extracted: dict, po_number: str|None, blocked_reason: str|None, extraction_error: str|None) -> dict | None
        # creates or overwrites ONLY a pending/blocked row; returns None (no write) if accepted/rejected
    def list(self, tenant_id, statuses: tuple[str, ...]) -> list[dict]
    def get(self, tenant_id, id) -> dict | None
    def mark(self, tenant_id, id, *, status, reviewed_by, note=None, extracted=None) -> dict   # accepted|rejected; also stores the corrected extraction
    def unblock(self, tenant_id, po_number) -> list[str]   # blocked rows with that normalised po_number → pending; returns ids
PgDocumentRepository.set_kind(tenant_id, document_id, kind) ; list_documents rows include "kind"
PgPurchaseOrderRepository.upsert_orders(..., document_id=None) / record_receipts(..., document_id=None) store the link; get_order/list_receipts dicts include document_id and source
PgPurchaseOrderRepository.po_owned_by_upload(tenant_id, po_number) -> bool
JsonDocumentRepository.set_kind; JsonSupportingDocumentRepository with the same five methods over DATA_DIR/supporting_documents.json (single-process fallback)
```
Dict shape: `{id, document_id, kind, status, extracted, po_number, blocked_reason, extraction_error, reviewed_by, reviewed_at (_iso), note, created_at (_iso), filename (joined from documents)}`.

- [ ] Failing tests: upsert creates; second upsert overwrites pending; no overwrite after mark accepted; unblock moves only matching normalised PO; tenant isolation; set_kind; document_id stored on order/receipt; po_owned_by_upload true for source upload, false for document/absent.
- [ ] Implement; full suite; ruff. Commit `"Keep a confirmable record of each scanned PO and receipt"`.

### Task 3: Extraction module `supporting_docs.py`

**Files:** `invoice_processing/supporting_docs.py` (new); tests `tests/test_supporting_docs.py`.

**Produces:**
```python
class PoLineExtraction(BaseModel): line_number:int|None; item_code:str|None; description:str=""; quantity:float|None; unit:str|None; unit_price:float|None; hsn_sac:str|None
class PurchaseOrderExtraction(BaseModel): po_number:str|None; po_date:str|None; valid_from; valid_to; vendor_name; vendor_tax_id; customer_name; currency; total_amount:float|None; amount_basis:str|None; lines:list[PoLineExtraction]=[]
class GrnLineExtraction(BaseModel): line_number; item_code; description; quantity_received:float|None; unit; remarks
class GoodsReceiptExtraction(BaseModel): grn_number; received_on; po_number; vendor_name; received_by; lines:list[GrnLineExtraction]=[]
PO_PROMPT, GRN_PROMPT: str   # transcribe-not-compute rules; "accepted/received qty, not ordered/rejected"; nulls for unreadable
def extract(pdf_path, kind, *, call=None) -> dict
    # call defaults to general_invoice_agent.call_gemini_with_pdf(pdf_path, prompt, GEMINI_PRO_MODEL, schema) (lazy import)
    # returns {"kind", "fields": {...header...}, "lines": [...], "_evidence": {field: {value, grounded, page, confidence}, "_summary": {min_confidence, fields_needing_review, has_text_layer}}, "_repairs": [...]}
    # cleanup: extraction_cleanup.fold_homoglyphs + read_as_printed on po_number/grn_number/item_code against confidence.extract_page_text(pdf) (OCR fallback via confidence.ocr_page_text when no text layer)
    # grounding: confidence.locate for each header field and each line quantity(+description); confidence.score-style HIGH/MEDIUM/LOW
def pair_grn_lines(lines: list[dict], po_lines: list[dict]) -> list[int|None]   # three_way.pair over {description, item_code, po_line_number: line_number}
def to_import_payload(kind, fields: dict, lines: list[dict]) -> dict   # the exact dict validate_orders/validate_receipts accept: PO → {po_number, vendor_name, total_amount, currency, status:"open", valid_from, valid_to, amount_basis, lines[{line_number, item_code, description, qty_ordered, unit_price, uom, receipt_required: None}]}; GRN → {grn_number, po_number, received_on, lines[{line_number|item_code, qty_received}]}
```
Line numbers default to position (1-based) when the scan has none.

- [ ] Failing tests with a fake `call` returning schema instances: PO and GRN field mapping; nulls preserved; homoglyph/O-0 repair against page text (use a small generated PDF or monkeypatch `extract_page_text`); grounding marks a quantity not on the page as ungrounded/LOW; `pair_grn_lines` order; `to_import_payload` shapes validate cleanly through `po_routes.validate_orders/validate_receipts`; a `call` raising → `extract` raises (the worker records the error).
- [ ] Implement; full suite; ruff. Commit `"Read purchase orders and goods receipts out of PDFs, grounded against the page"`.

### Task 4: Classifier + pipeline branch + worker tail

**Files:** `general_invoice_agent.py` (`DOCUMENT_TYPES` ~L609-628, prompt ~L1195-1240, `_TITLE_MARKERS` ~L1255, `_run_pipeline` ~L3189 / `process_invoice` ~L3294), `worker/runner.py` (`process_document` ~L236, `seen_invoices_for` ~L134), `api/service.py` (`get_supporting_documents()` accessor like `get_gl_rules`, `list_invoices` ~L723 kind filter, conftest reset), `db/repositories.py` (`iter_case_ids`/invoice listings skip non-invoice kinds where used by the sheet `_base` ~L2435); tests `tests/test_document_taxonomy.py`, `tests/test_worker.py`, `tests/test_pipeline_progress.py` (if the step list changes), `tests/test_workbench_api.py`.

**Produces:** `GOODS_RECEIPT = "goods_receipt"` in `DOCUMENT_TYPES` (not in `NON_PAYABLE_TYPES`? — it IS non-payable; add it to `NON_PAYABLE_TYPES` too so a bundle-with-invoice keeps today's semantics); `SUPPORTING_TYPES = {"purchase_order", "goods_receipt"}`; `process_invoice` returns `{"decision": "SUPPORTING", "document_type": t, "pdf": str(path)}` when `invoice_count == 0 and document_type in SUPPORTING_TYPES` (artifacts `01_classification.json` still written); `runner.process_supporting(ctx, document, pdf_path, document_type, *, cases, documents, supporting, purchase_orders)`: `supporting_docs.extract` (exceptions → `extraction_error`, empty fields), normalise `po_number`, `blocked_reason` = "Purchase order X is not on file" when kind is GRN and `purchase_orders.book(...)` is None, `supporting.upsert_from_extraction(...)`, `documents.set_kind`, store `supporting_extraction` artifact for traceability, audit `supporting_document_extracted`; **skip** `_apply_rules`, `_code_gl`, `project_case`, `route`. `process_document` sets `documents.set_kind(…, "invoice")` on the invoice path. `list_invoices` and `seen_invoices_for` exclude cases whose document kind is not null and ≠ invoice.

- [ ] Failing tests: keyword fallback maps "GOODS RECEIPT NOTE"/"GRN No." → goods_receipt and "DELIVERY CHALLAN" stays delivery_challan; prompt text contains goods_receipt; `process_invoice` with a stubbed classifier → SUPPORTING and no later agent runs; bundle (`invoice_count 1`) → invoice path; worker: SUPPORTING result → supporting row written, kind set, no projection/route (fakes as in `TestRoutingAfterProjection`); GRN with no PO → blocked; extractor raising → pending with extraction_error; retry after accepted → no overwrite; `/api/invoices` hides a case whose document kind is goods_receipt; `seen_invoices_for` excludes it.
- [ ] Implement; full suite; ruff. Commit `"Route scanned purchase orders and receipts to confirmation instead of the invoice queue"`.

### Task 5: Routes, acceptance, unblocking, documents listing

**Files:** `api/supporting_routes.py` (new), `api/service.py` (router), `api/system.py` (`GET /api/documents` kind + stage ~L196/L298), `api/po_routes.py` + `api/config_routes.py` (call `unblock` after PO import/PUT; `queue_rematches` reuse), `db/repositories.py` `_ENTITY_TYPES`; tests `tests/test_supporting_routes.py` (new, CI Postgres list), `tests/test_tenant_isolation_api.py`.

**Produces:**
```
GET  /api/supporting-documents?status=pending,blocked   → {"documents": [row + filename + document_url]}
GET  /api/supporting-documents/{id}                      → row + "paired": [po_line_number|None per line] (when PO on file) + "po_on_file": bool + document_url
POST /api/supporting-documents/{id}/accept  {fields: {...}, lines: [...]}  → GRN: 200 {recorded, rematch_queued} ; PO: 201 {created, updated, lines}
POST /api/supporting-documents/{id}/reject  {reason (1..500)}             → 200 {status: "rejected"}
NEEDS_DB = "Scanned documents need a database. Configure DATABASE_URL."
```
Accept: 404 unknown; 409 if status not pending ("already accepted/rejected") or blocked ("Purchase order X is not on file"); build payload with `supporting_docs.to_import_payload`, run `validate_*` → 400 listing problems; PO: `po_owned_by_upload` → 409 "PO-4471 came from your ERP upload; a scan cannot change it."; call `upsert_orders(source="document", document_id=...)` / `record_receipts(source="document", document_id=...)` (ValueError → 400); `mark(accepted, extracted=corrected)`; audits `supporting_document_accepted {kind, number, changed: {field: {before, after}}}`; GRN → `queue_rematches`; PO → `supporting.unblock(tenant, po_number)` (audit `supporting_document_unblocked` per id). PO import and PUT also call `unblock` for every PO number they stored. `GET /api/documents` rows gain `kind` and stage text "Awaiting confirmation on Purchase orders" / "Blocked: <reason>" / "Confirmed" / "Rejected".

- [ ] Failing tests: list/detail shapes and roles (viewer 403); accept GRN → receipt recorded with source document + document_id, rematch queued, row accepted, audit has changed fields; accept with bad quantity → 400 row error, nothing stored; blocked → 409; accept PO → created; PO owned by upload → 409; scanned PO then ERP upload replaces it (source becomes upload); reject requires reason; second accept → 409; PO import unblocks a blocked GRN; 503 without DB on all four; cross-tenant probe + PROBED + module list; documents listing shows kind + stage.
- [ ] Implement; full suite; ruff. Commit `"Confirm a scanned PO or receipt into the match through the same checks as an import"`.

### Task 6: Frontend

**Files:** `frontend/src/lib/api.ts` (types `SupportingDocument`, `SupportingDetail`; `DocumentRow.kind`), `frontend/src/components/SupportingReview.tsx` (drawer: PDF iframe via `document_url`, header fields editable with flagged-first ordering and confidence hint like the workbench field editor, lines table editable `line_number/item_code/description/quantity`, paired PO line column, Accept/Reject, per-row errors via `ApiError.friendly`), `frontend/src/app/purchase-orders/page.tsx` (section "Documents awaiting confirmation" above the PO list; cards; opens the drawer; refresh list + PO list after accept), `frontend/src/app/documents/page.tsx` (Kind column).
- [ ] `cd frontend && npm run lint && npx tsc --noEmit`. Commit `"Let a clerk confirm what was read from a scanned PO or receipt"`.

### Task 7: e2e

**Files:** `scripts/e2e_api.py` (seed a pending GRN `supporting_documents` row for `PO-E2E-1` with a line quantity deliberately misread as 10 where the PO ordered 3, plus a blocked GRN for `PO-NOT-YET`; both reference a seeded document row pointing at a golden PDF), `frontend/e2e/golden-path.spec.ts`.
- [ ] Tests: the pending card shows on `/purchase-orders`; the drawer shows the misread quantity flagged; clerk corrects to 1, accepts → toast "Recorded 1 receipt; 1 invoice queued for re-match" and `case_004_po` clears via the existing drain (reuse the poll pattern); the blocked card shows "not on file" and Accept is disabled; Documents page shows Kind "Goods receipt". Keep tests order-independent (the held-invoice test from 4A also clears `case_004_po` — give the e2e GRN scan its own PO/case: seed `case_004_po2` + `PO-E2E-2` so the two tests do not share state).
- [ ] `npm run test:e2e` (expect 27). Commit `"Prove in the browser that a confirmed receipt scan clears a held invoice"`.

### Task 8: GRN renderer, scan set, measured fixture, benchmark guard

**Files:** `benchmark/dataset/generator/gen_render.py` (`render_grn(rec, path)` from `po_master.csv`, same fonts/table style as `render_nondoc`, then `degrade()`), a scratchpad script that renders ~20 GRNs (varied: single/multi-line, received < ordered, stamps) + reuses the 6 PO PDFs with ground truth JSON; `tests/fixtures/supporting_docs/measured.json` (ground truth + one real `supporting_docs.extract` run per PDF, captured once with Gemini locally — PDFs themselves are NOT committed; the fixture records file names, ground truth and extracted output); `tests/test_supporting_docs_measured.py` (scores header-field accuracy, per-line quantity accuracy, pairing rate from the fixture; pins the figures with a `# ponytail:` note, no gate); `benchmark/CHANGELOG.md`.
- [ ] Replay guard: `replay` over `after-ecb9bbd` → clean 45 invoices identical; the 6 PO NONDOC records now `SUPPORTING` — record before/after. If the replay harness cannot represent SUPPORTING (it expects a decision), teach `benchmark/harness` to score SUPPORTING as "correctly not an invoice" (same bucket REJECT occupied) and say so.
- [ ] CHANGELOG entry with the measured figures. Commit `"Measure PO and GRN scan extraction on a generated scan set; benchmark invoices unchanged"`.

### Task 9: Gates, review, verification, PR
- [ ] `bash benchmark/harness/gates.sh` 9/9; whole-branch review (most capable model) → one fix wave → scoped re-review; independent scripted verification on a fresh DB stack (classify-stub → extract-fake → pending → accept → receipt → re-match clears; blocked → unblocked by PO import; ERP-owned PO 409; isolation; 503); browser check on the manual stack with one real scan if available.
- [ ] Ask the user, push `phase3/scanned-grn-po`, open PR, merge only on explicit word with `--match-head-commit`.

## Verification (end-to-end)
1. Full pytest + 9 gates green.
2. Manual stack: upload a PO PDF and a GRN PDF (generated by Task 8's renderer) through `/documents`; they appear on `/purchase-orders` awaiting confirmation, not in the invoice queue; accept both; a held PO invoice clears; PO detail shows "from scan".
3. CI green (test, postgres, frontend, e2e).

## Self-review notes
- Spec §1→Task 4; §2→Task 3; §3→Tasks 1,2,5; §4→Tasks 5,6; §5→Tasks 7,8,9.
- Names used consistently: `SupportingDocument`, `PgSupportingDocumentRepository.{upsert_from_extraction,list,get,mark,unblock}`, `supporting_docs.{extract,pair_grn_lines,to_import_payload}`, `runner.process_supporting`, `po_owned_by_upload`, `documents.kind`, `document_id` links, decision `"SUPPORTING"`.
