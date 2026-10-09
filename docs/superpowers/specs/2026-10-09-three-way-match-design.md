# Three-way match (PO lines + goods receipts) — design

Phase 3, sub-project 4 (part A). Brainstormed with the user 2026-10-09; every
section below was approved in turn.

## Goal

Match each invoice line against its purchase-order line and the goods actually
received, so an invoice cannot be paid for goods that were not ordered, not
delivered, or priced above the order — without rejecting invoices that merely
arrived before the goods.

## Decisions (user rulings)

1. **Sources: structured upload now, scanned GRN/PO documents next.** This
   slice stores POs (with lines) and GRNs in DB tables fed by CSV/JSON upload.
   Sub-project B will extract GRN/PO PDFs into the same tables; every table
   carries `source` (`upload` | `document`) so the match never changes.
2. **Receipt required: per PO line, vendor as fallback.** `po_lines.receipt_required`
   true/false wins; null falls back to the vendor master `kind` (goods → yes,
   services → no); an unknown vendor → yes (fail closed: hold).
3. **Receipt shortfall holds for review, never rejects.** Invoices often arrive
   before goods; a later GRN re-matches and clears them automatically.
4. **Line pairing: fixed order, fail safe.** Item code → PO line number →
   unambiguous description match → otherwise unpaired (held, never guessed).
5. **Billed-so-far = upload opening balance + lines of invoices Ledgerly
   approved since.** Pending/rejected invoices do not count; the race between
   two invoices for the same units is closed at approval time.
6. **Line price over PO holds for review.** The hard two-way failures (PO not
   found, vendor mismatch, closed, outside validity, header over-billing) keep
   rejecting at step 2.2.
7. **Architecture: pure matcher + DB tables + new informational step 2.3.**
   (Rejected: growing `po_matching.py`; matching outside the pipeline.)

## 1. Data and ingest

**Tables** (all in `TENANT_TABLES` and the migration's own `RLS_TABLES`; one
new Alembic revision):

- `purchase_orders`: `id`, `tenant_id`, `po_number` (normalised with
  `po_matching.normalize_po_number`; unique per tenant), `po_number_display`,
  `vendor_name`, `total_amount`, `currency`, `status`, `amount_invoiced`,
  `customer_name`, `valid_from`, `valid_to`, `amount_basis`, `approved_quantity`,
  `approved_unit_price`, `hsn_sac` (every field `PurchaseOrder` reads today),
  `source`, `uploaded_at`, `uploaded_by`.
- `po_lines`: `id`, `tenant_id`, `purchase_order_id`, `line_number` (unique per
  PO), `item_code`, `description`, `qty_ordered`, `unit_price`, `uom`,
  `receipt_required` (nullable bool), `qty_invoiced_opening`.
- `goods_receipts`: `id`, `tenant_id`, `grn_number` (unique per tenant),
  `purchase_order_id`, `received_on`, `reason` (required when any line is
  negative), `source`, `recorded_at`, `recorded_by`.
- `grn_lines`: `id`, `tenant_id`, `goods_receipt_id`, `po_line_id`,
  `qty_received` (may be negative only as a correction).

**Ingest** (`invoice_processing/api/po_routes.py`; clerk+; 503 without DB):

- `POST /api/purchase-orders/import` — JSON `{purchase_orders: [{…, lines: [...]}]}`
  or CSV (one row per line, header columns repeated). **Upsert by PO number**:
  header updated, lines replaced, opening balances reset to the uploaded
  `qty_invoiced`. POs absent from the upload are untouched.
- `POST /api/goods-receipts/import` — JSON `{goods_receipts: [{grn_number,
  po_number, received_on, reason?, lines: [{line_number | item_code,
  qty_received}]}]}` or CSV. **Immutable once accepted**; a repeated
  `grn_number` is a no-op (idempotent re-send); a correction is a new GRN with
  negative quantities and a reason. A GRN naming an unknown PO or PO line is
  refused.
- **Whole-file validation in one transaction**: any bad row → 400 listing every
  problem with its row number; nothing stored.
- Audited: `purchase_orders_imported` (counts), `goods_receipt_recorded`
  (per GRN).
- **File fallback**: the pipeline reads POs from the DB where the tenant has
  any, else the existing per-tenant JSON (benchmark, single-process dev). The
  existing `GET`/`PUT /api/purchase-orders` routes (`config_routes.py`) move
  onto the DB when one is configured: `PUT` keeps its replace-all meaning (old
  clients keep working) and `GET` gains the new fields below.

## 2. The matcher (`invoice_processing/three_way.py`, pure)

```
match(invoice_lines, po, po_lines, received, billed, *, vendor_kind,
      qty_tol=0, price_tol_pct=0.02, price_tol_abs=50.0) -> ThreeWay
ThreeWay = (lines: list[LineResult], signals: list[str], evidence: list[str])
LineResult = (invoice_line, po_line_number|None, paired_by|None, billed,
              received, signals)
```

`received[po_line]` = sum of `qty_received` over the PO's GRNs (corrections
included). `billed[po_line]` = opening balance + quantity on invoices approved
since the PO was last uploaded. The glue computes both.

**Pairing**, per invoice line, each PO line claimed at most once per invoice:
1. item code (both present; casefolded, spaces/dashes removed) equal;
2. invoice quotes a PO line number that exists;
3. description: casefold, strip punctuation and unit tokens, word sets;
   accept the best PO line only if Jaccard overlap ≥ 0.6 **and** it beats the
   runner-up by ≥ 0.2;
4. else **unpaired**.

**Checks per paired line** (quantity `q`, unit price `p` from the invoice line):
- receipt required (rule 2) → `billed + q ≤ received + qty_tol`;
- `p ≤ po_line.unit_price + max(price_tol_pct × unit_price, price_tol_abs)`
  (under-billing passes);
- `billed + q ≤ qty_ordered`.

**Signals**: `not_received` (received 0), `short_received`, `price_over_po`,
`over_ordered`, `unpaired_line`, `unreadable_line` (quantity or price missing
or not a number — never treated as 0). Each carries one evidence sentence, e.g.
*"Line 2 'M8 hex bolt': billed 100, received 60 (GRN-881, GRN-884); 40 still
to arrive."*

No signals → pass. Any signal → hold. Never reject.

**Not run**: no PO quoted, PO not found (step 2.2 owns those), or the PO has no
lines — then step 2.3 records *"No PO lines on file; header match only."* and
passes.

## 3. Pipeline, re-match and approval

**Step 2.3** (phase 2, after 2.2; only when 2.2 found the PO and did not
reject): validation `{step: "2.3", rule: "Invoice lines match PO lines and goods
received", passed, informational: true, evidence, signals, lines}`. On failure
it holds exactly as step 3.4 does: `invoice["_routing"] = "REVIEW"` and
`receipt: <signal>` entries in `_evidence._summary.fields_needing_review`.
Declared in `invoice_master_data.yaml` with `informational_only: true`.

**`PurchaseOrderBook` port** — `find(po_number) -> {po, lines, received,
billed} | None` — with a DB implementation (the four tables + projected
`invoice_lines` of invoices approved since `uploaded_at`) and a file
implementation (today's JSON, optional `lines`). `tenant_settings.apply_to_pipeline`
picks the DB book when the tenant has POs in the DB, else the file — as vendors
do today.

**Re-match on GRN arrival**: after a GRN import commits, the API enqueues a
`rematch` job for each undecided case that quotes that PO and is held by step
2.3. The worker: loads the stored extraction and phase-2 artifact → reruns
`three_way.match` → in one `update_artifact` rewrites step 2.3 and the
`receipt:` entries → if `fields_needing_review` is now empty, sets routing
`AUTO` → re-projects and re-routes approvals → audits `three_way_rematched`
(old → new signals). No LLM call, no full pipeline run. A decided case is never
re-matched.

**At approval** (`approval_flow.decide`, final step only, PO invoices with
lines): rerun the match with current `billed`. If approving would exceed
received or ordered quantity → 409 naming the line and what consumed it (*"Line
2 would bill 160 of 100 received; INV-77 was approved for 60 since this was
checked."*). Reject is always allowed. Admin `override_reason` works and is
audited, as for other refusals.

**Settings** (`tenant_settings.SETTABLE`): `three_way_qty_tolerance` (number ≥
0, default 0). Line prices reuse `po_tolerance_pct` / `po_tolerance_abs`.

## 4. API, UI, errors

**Routes** (role-gated, cross-tenant probe + `PROBED` entry each, no
`tenant_id` parameter, 503 without DB, `Field(max_length=…)` on stored strings,
5 MB upload cap):
- `POST /api/purchase-orders/import` (clerk+) → `{created, updated, lines}`.
- `GET /api/purchase-orders` (existing route, extended; viewer+) → today's
  fields plus line count and received / billed totals.
- `GET /api/purchase-orders/{po_number}` (viewer+) → lines with ordered /
  received / billed / remaining, and the PO's GRNs.
- `POST /api/goods-receipts/import` (clerk+) → `{recorded, skipped_duplicates,
  rematch_queued}`.
- `GET /api/goods-receipts?po_number=` (viewer+).

**UI**:
- `/purchase-orders`: upload buttons (PO file, GRN file; CSV or JSON);
  downloadable CSV templates; PO list expanding to lines (ordered / received /
  billed / remaining) and GRNs.
- Invoice workbench: a "Three-way match" panel when step 2.3 ran — per line:
  paired PO line and how, billed, received, price vs PO, signal chips, evidence.
- Approval refusal shows the server's sentence (`ApiError.friendly`).

**Fail closed**:
- PO book unavailable/erroring in the pipeline → step 2.3 "could not be
  checked", hold.
- Re-match failure → case stays held; logged; audited `three_way_rematch_failed`.
- Approval re-check failure → approval refused (503).
- Imports: one transaction per file; partial imports never stored.

## 5. Testing and measurement

**Tests** (TDD; each fails first):
- `tests/test_three_way.py` — pure: pairing order and ambiguity (near-tie
  descriptions, a PO line claimed twice), every signal, cumulative maths
  (negative-GRN corrections, opening balances), receipt rule fallback, unknown
  vendor, unreadable quantities, tolerances, PO without lines.
- `tests/test_purchase_order_repository.py` — upsert, GRN immutability and
  idempotency, tenant isolation, received/billed totals. CI Postgres list.
- `tests/test_po_routes.py` — imports (JSON, CSV, every-row errors, 5 MB cap,
  503, roles), cross-tenant probes. CI Postgres list.
- Pipeline: 2.3 holds not rejects; 2.2 rejections unchanged; book unavailable
  holds.
- Worker: GRN arrival clears a held case; another hold reason keeps it held;
  decided cases untouched.
- Approval: the over-billing race → 409; admin override.
- Migration: RLS-list test; post-upgrade forced-policy test extended to the
  four tables.
- e2e: upload PO + GRN in the UI and see the lines; a held invoice shows the
  three-way panel; a GRN upload clears it.

**Measurement** (`benchmark/CHANGELOG.md`):
1. Guard: replay over `after-ecb9bbd` identical to `replay-gl-coding` (the
   benchmark PO file has no lines, so 2.3 never runs); posted-untouched-wrongly
   must not rise.
2. Planted set `tests/fixtures/three_way/planted.json`: Company B PO invoices
   from ground truth, each given PO lines and a GRN; about half carry one planted
   fault (short receipt, no receipt, price over PO, extra unpaired line, billing
   beyond ordered) and the rest include split deliveries that must still pass.
   Run over the stored extractions (no LLM). Report clean-passed-untouched,
   faults held with the right signal, and description-fallback pairing rate.
3. Independent scripted run against a fresh DB-backed stack; browser check on
   the manual stack.

## Out of scope

GRN/PO PDF extraction (sub-project B); ERP connectors; PO-line GL inheritance
(small follow-up once PO lines exist); reserving quantity for pending invoices.
