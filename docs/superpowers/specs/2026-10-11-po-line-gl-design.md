# PO-line GL inheritance — design

Phase 3, sub-project 6 (a small follow-up to GL auto-coding and the three-way
match). Brainstormed with the user 2026-10-11; every section below was approved
in turn.

## Goal

An invoice line paired to a purchase-order line takes that PO line's GL
account, so the buyer's coding at ordering time carries onto the invoice
instead of being guessed again by vendor and keyword rules.

## Decisions (user rulings)

1. **PO line wins over a matching GL rule.** The buyer coded the PO when they
   raised it; that is the most specific intent. Rules fill lines the PO leaves
   blank and lines that could not be paired.
2. **The account comes from the ERP PO upload only** (CSV/JSON). Scanned POs
   rarely print GL codes, and the ERP upload already wins over scans; a scanned
   PO's lines have no account.
3. **Nothing recodes automatically when a PO's accounts change later.** Open
   invoices keep their coding; a document retry re-runs the pipeline and
   recodes. Manual lines and decided invoices are never touched (existing
   rulings from the GL auto-coding spec).
4. **Approach: inherit inside the worker's GL step** from the stored step 2.3
   pairing and the PO book, not inside the pipeline's matcher and not as a
   rule kind.

## 1. Data and import

- `po_lines.gl_account` TEXT NULL, at most 64 characters (the same bound as
  `invoice_lines.gl_account` and `gl_rules.gl_account`). One migration
  revising `a7b3c9d2e4f6`, `RLS_TABLES = []`, SQLite-safe downgrade through
  `batch_alter_table`. No backfill, no index.
- PO import (`api/po_routes.py`): `PoLineIn.gl_account: str | None`, max
  length 64; `"gl_account"` is added to `PO_LINE` so the CSV grouper carries
  it. Trimmed; blank is none; too long is the usual "row N: …" error and the
  whole file is refused.
- `PgPurchaseOrderRepository`: the upsert writes it with the other line
  fields; `_line_dict` returns it, so `book()`, `get_order` and the PO detail
  route expose it. `po_book.FileBook` passes the raw JSON line through.
- `supporting_docs.to_import_payload` does not set it.
- A replace-all or re-import overwrites it with the rest of the line.

## 2. Coding

- `gl_coding.code_lines(invoice, lines, rules, previous=None, po_accounts=None)`.
  `po_accounts` maps a projected invoice line number to
  `(po_line_number, gl_account)`. Precedence per line: manual entry kept from
  `previous`, then PO account, then the best rule, then uncoded.
- A PO-coded entry is
  `{"line_number", "gl_account", "source": "po", "rule_id": null,
  "po_line_number": int, "coded_by": null, "coded_at": null}`.
  `valid_entries` is unchanged.
- A new pure helper `gl_coding.po_accounts_from_match(three_way_lines,
  book_lines, projected_count)` builds the map. It returns `{}` unless the
  2.3 entry exists and `len(three_way_lines) == projected_count`: step 2.3
  numbers lines by extraction position while the projection uses the
  transformer's line numbers, and the two agree only when the counts do (the
  same guard the export uses). For each 2.3 line with a `po_line_number`, the
  book line's `gl_account` is used when it is a non-blank string.
- The worker's `_code_gl` reads the 2.3 entry from
  `artifacts["phase2"]["validations"]` (`step == "2.3"`) and the book through
  `config["purchase_order_book"].book(po_number)`, passes the map to
  `code_lines`, and on any exception logs and codes by rules alone (the
  existing fail-safe).
- Nothing else changes: the approval rule kind `gl`, the Xero `AccountCode`,
  the sheet's GL accounts column, the per-invoice CSV and the manual PATCH
  route all read the account from the same artifact. A manual entry still
  beats a PO account on later runs; "remember for this vendor" is unchanged.

## 3. UI

- `GlCodingLine.source` gains `"po"` and an optional `po_line_number`.
- The workbench GL column's source label reads "PO line N" for a `po` entry.
- The purchase-orders page's lines table gains a "GL" column, blank when none.

## 4. Errors

- A set-but-blank account is none.
- A book read error during coding leaves rule coding in place.
- Coding never holds or rejects an invoice (existing ruling).

## 5. Testing (TDD; each test proven to fail without its change)

- `gl_coding`: PO beats rule; a rule still codes a line with no PO account;
  manual survives a PO account; the `po` entry shape;
  `po_accounts_from_match` count guard, missing `po_line_number`, blank account.
- Worker: a paired invoice is coded from the PO line; no 2.3 entry, a count
  mismatch, or an unreadable book falls back to rules.
- PO import and repository: `gl_account` accepted in JSON and CSV, trimmed,
  blank is none, over 64 is a named row error; the PO detail route returns it;
  the scanned-PO payload has none.
- Migration round trip; the new revision is added to the RLS parametrize with
  zero created tables.
- e2e: import a PO with a GL account for a seeded held case, open it, and the
  GL column shows "PO line N".

## 6. Verification

All gates. On the manual stack: PUT the Cloudhaven PO with a GL account, retry
the held invoice's document, confirm "PO line N" in the browser. The benchmark
replay is unchanged by construction: the benchmark PO file has no lines, so
step 2.3 never pairs.

## Later (out of scope)

Reading GL codes off scanned POs; recoding open invoices when a PO is
imported; a per-PO-header default account; flagging rule/PO disagreements.
