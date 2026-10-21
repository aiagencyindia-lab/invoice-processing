"""Goods: receipts and delivered shipments on the invoice's PO vs the
quantity invoiced."""

from __future__ import annotations

from decimal import Decimal

from . import bands
from .classify import TIME_UNITS, dec, iso_date


def _received(r, grn: dict) -> Decimal:
    """A history receipt carries its quantity; a v1 receipt sums its lines."""
    for key in ("qty_received", "quantity"):
        value = dec(grn["attrs"].get(key))
        if value is not None:
            return value
    return sum(
        (
            dec(line["attrs"].get("qty_received")) or Decimal(0)
            for line in r.parts.get(grn["id"], [])
        ),
        Decimal(0),
    )


def _date(node: dict):
    a = node["attrs"]
    return iso_date(a.get("received_on") or a.get("invoice_date"))


def check(r, invoice: dict, lines: list[dict]) -> bands.Check | None:
    pairs = []
    for line in lines:
        a = line["attrs"]
        qty = dec(a.get("quantity"))
        if qty is None or str(a.get("unit") or "").lower() in TIME_UNITS:
            continue
        pairs.append((qty, dec(a.get("unit_price"))))
    expected = sum((q for q, _ in pairs), Decimal(0))
    if expected <= 0:
        return None
    c = bands.Check(
        "goods", "units", expected, unit_price=bands.unit_price(pairs)
    )
    po = r.po_of.get(invoice["id"])
    # ponytail: Σ over everything fulfilling the PO; allocate across the
    # PO's invoices if partial invoicing of one order becomes common.
    for ev in r.fulfils.get(po, []) if po else []:
        if ev["kind"] not in ("grn", "shipment"):
            continue
        c.present = True
        c.evidence.append(ev["id"])
        if r.late(invoice, _date(ev)):
            continue  # delivered too long after the invoice to count
        if ev["kind"] == "grn":
            c.found += _received(r, ev)
        elif str(ev["attrs"].get("status") or "").lower() == "delivered":
            c.found += dec(ev["attrs"].get("quantity")) or Decimal(0)
    if not c.present:
        a = invoice["attrs"]
        reference = a.get("po_number") or a.get("invoice_number") or ""
        r.ask(
            c, "logistics", reference, keys=("quantity", "delivered_quantity")
        )
    return c
