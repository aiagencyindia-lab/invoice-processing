"""Where the pipeline reads purchase orders from: a tenant's file or the database.

`find` returns the header the two-way match (step 2.2) has always used. `book`
adds what the three-way match (step 2.3) needs: the order's lines, how much of
each has been received and billed, and which goods receipts cover each line.
`None` from `book` means there are no lines to match, so 2.3 falls back to the
header match alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from . import po_matching
from .paths import PURCHASE_ORDER_FILE


class PurchaseOrderBook(Protocol):
    def find(self, po_number) -> dict | None: ...

    def book(self, po_number) -> dict | None: ...


class FileBook:
    """Purchase orders read from a JSON file next to the case data.

    The local stand-in for an ERP query, exactly as `_load_seen_invoices` is
    the local stand-in for an indexed duplicate lookup. Purchase orders
    originate in the customer's ERP; a pilot loads an export of the open ones
    here, and the connector replaces this without the matching logic changing.

    Missing file means no purchase orders are known, which surfaces as
    "purchase order not found" rather than as a silent pass -- a PO check that
    cannot see any POs must not report success.

    A record may carry optional `lines`, each with `qty_received` and
    `qty_invoiced`; without them the record is header-only.
    """

    def __init__(self, path=None):
        # The pipeline passes the tenant's file; the shared legacy path is only
        # for a direct construction with no tenant in play.
        self.path = Path(path or PURCHASE_ORDER_FILE)
        self._index: dict[str, dict] | None = None

    def _load(self) -> dict[str, dict]:
        if self._index is not None:
            return self._index
        self._index = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                print(
                    f"   WARNING: {self.path.name} is not valid JSON; no POs loaded."
                )
                return self._index
            records = (
                raw.get("purchase_orders", raw)
                if isinstance(raw, dict)
                else raw
            )
            if isinstance(records, dict):
                records = list(records.values())
            for record in records or []:
                key = po_matching.normalize_po_number(record.get("po_number"))
                if key:
                    self._index[key] = record
        return self._index

    def find(self, po_number: str) -> dict | None:
        return self._load().get(po_matching.normalize_po_number(po_number))

    def book(self, po_number) -> dict | None:
        record = self.find(po_number)
        lines = (record or {}).get("lines")
        if not lines:
            return None
        return {
            "po": record,
            "lines": lines,
            "received": {
                ln["line_number"]: ln.get("qty_received", 0) for ln in lines
            },
            "billed": {
                ln["line_number"]: ln.get("qty_invoiced", 0) for ln in lines
            },
            # ponytail: a file has no goods-receipt numbers; the DB book does.
            "grn_numbers": {},
        }


class DbBook:
    """One tenant's purchase orders and goods receipts in the database."""

    def __init__(self, repo, tenant_id: str):
        self.repo, self.tenant_id = repo, tenant_id

    def find(self, po_number) -> dict | None:
        book = self.repo.book(self.tenant_id, po_number)
        return book["po"] if book else None

    def book(self, po_number) -> dict | None:
        book = self.repo.book(self.tenant_id, po_number)
        if book is None:
            return None
        grn_numbers: dict[int, list[str]] = {}
        for receipt in self.repo.list_receipts(self.tenant_id, po_number):
            for line in receipt["lines"]:
                grn_numbers.setdefault(line["line_number"], []).append(
                    receipt["grn_number"]
                )
        return {**book, "grn_numbers": grn_numbers}
