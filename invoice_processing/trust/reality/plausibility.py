"""Plausibility (FR-23.4): is the price or the volume out of line with the
vendor's own history, and was the invoice for a time the business was shut?

A price jump is explained, and not raised, when a contract governing the
invoice or its vendor has a rate or escalator clause: that contract owns the
price, and leakage judges the invoice against it. When the clause was
uploaded says nothing about the price, so it is not asked.
"""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from functools import cached_property
from statistics import median

from ..payment.signals import robust_z
from .classify import dec, iso_date

CENT = Decimal("0.01")
PRICE_JUMP = "REAL-PRICE-JUMP"
VOLUME_SPIKE = "REAL-VOLUME-SPIKE"
SHUTDOWN = "REAL-SHUTDOWN"


def norm(description) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(description or "").lower()))


def _z(value: float) -> str:
    return "inf" if value == float("inf") else str(round(value, 2))


class History:
    """(canonical vendor, normalised description) -> every priced line:
    (invoice id, invoice date, unit price, quantity)."""

    def __init__(self, r):
        self.r = r

    @cached_property
    def lines(self) -> dict[tuple, list[tuple]]:
        r = self.r
        out: dict[tuple, list[tuple]] = defaultdict(list)
        for invoice in r.ectx.graph.nodes(r.tid, kind="invoice"):
            vendor = r.vendor_of.get(invoice["id"])
            if vendor is None:
                continue
            day = iso_date(invoice["attrs"].get("invoice_date"))
            for line in r.lines(invoice):
                a = line["attrs"]
                price, qty = dec(a.get("unit_price")), dec(a.get("quantity"))
                if price is not None and qty is not None:
                    key = (vendor, norm(a.get("description")))
                    out[key].append((invoice["id"], day, price, qty))
        return out

    @cached_property
    def priced(self) -> set[str]:
        """Contracts with a rate or escalator clause."""
        r = self.r
        clauses = {
            n["id"]
            for n in r.ectx.graph.nodes(r.tid, kind="contract_clause")
            if n["attrs"].get("type") in ("rate", "escalator")
        }
        return {e["dst"] for e in r._edges("part_of") if e["src"] in clauses}

    @cached_property
    def contracts(self) -> tuple[dict[str, set], dict[str, set]]:
        r = self.r
        by_invoice: dict[str, set] = defaultdict(set)
        by_vendor: dict[str, set] = defaultdict(set)
        for e in r._edges("governed_by"):
            by_invoice[e["src"]].add(e["dst"])
        for e in r._edges("governs"):
            by_vendor[r.canon(e["dst"])].add(e["src"])
        return by_invoice, by_vendor

    def explained(self, invoice: dict) -> bool:
        by_invoice, by_vendor = self.contracts
        vendor = self.r.vendor_of.get(invoice["id"])
        contracts = by_invoice.get(invoice["id"], set()) | by_vendor.get(
            vendor, set()
        )
        return bool(contracts & self.priced)


def findings(r, invoice: dict, history: History) -> list:
    from . import spec  # noqa: PLC0415 - the package imports this module

    out = []
    vendor = r.vendor_of.get(invoice["id"])
    need = r.ectx.threshold("reality.min_history")
    price_z = r.ectx.threshold("reality.price_jump_zscore")
    volume_z = r.ectx.threshold("reality.volume_zscore")
    for position, line in enumerate(r.lines(invoice) if vendor else [], 1):
        a = line["attrs"]
        price, qty = dec(a.get("unit_price")), dec(a.get("quantity"))
        if price is None or qty is None:
            continue
        others = [
            h
            for h in history.lines.get((vendor, norm(a.get("description"))), [])
            if h[0] != invoice["id"]
        ]
        if len(others) < need:
            continue
        number = a.get("line_number") or position
        cites = [invoice["id"], line["id"]]
        prices = [h[2] for h in others]
        z = robust_z(price, prices)
        mid = median(prices)
        if z > price_z and price > mid and not history.explained(invoice):
            out.append(
                spec(
                    PRICE_JUMP,
                    invoice,
                    f":{number}",
                    f"Line {number} is priced {price.quantize(CENT)} against "
                    f"the vendor's usual {mid.quantize(CENT)}.",
                    cites,
                    impact=((price - mid) * qty).quantize(CENT),
                    confidence=0.6,
                    line_number=number,
                    unit_price=str(price.quantize(CENT)),
                    median=str(mid.quantize(CENT)),
                    quantity=str(qty),
                    z=_z(z),
                    history=len(others),
                )
            )
        qtys = [h[3] for h in others]
        z = robust_z(qty, qtys)
        mid = median(qtys)
        if z > volume_z and qty > mid:
            out.append(
                spec(
                    VOLUME_SPIKE,
                    invoice,
                    f":{number}",
                    f"Line {number} bills a quantity of {qty.normalize():f} "
                    f"against the vendor's usual {mid.normalize():f}.",
                    cites,
                    impact=((qty - mid) * price).quantize(CENT),
                    confidence=0.6,
                    line_number=number,
                    quantity=f"{qty.normalize():f}",
                    median=f"{mid.normalize():f}",
                    unit_price=str(price.quantize(CENT)),
                    z=_z(z),
                    history=len(others),
                )
            )
    return out


def shutdown(r, invoice: dict) -> list:
    from . import spec  # noqa: PLC0415

    a = invoice["attrs"]
    day = iso_date(a.get("invoice_date"))
    start = iso_date(a.get("service_period_start")) or day
    end = iso_date(a.get("service_period_end")) or day
    if start is None or end is None:
        return []
    for pair in r.ectx.settings.get("trust_shutdown_periods") or []:
        lo, hi = (iso_date(p) for p in pair)
        if lo and hi and start <= hi and lo <= end:
            amount = dec(a.get("amount"))
            return [
                spec(
                    SHUTDOWN,
                    invoice,
                    "",
                    f"Billed for {start.isoformat()} to {end.isoformat()}, "
                    f"which overlaps a shutdown ({lo.isoformat()} to "
                    f"{hi.isoformat()}).",
                    [invoice["id"]],
                    impact=None if amount is None else amount.quantize(CENT),
                    confidence=0.9,
                    shutdown=[lo.isoformat(), hi.isoformat()],
                    billed=[start.isoformat(), end.isoformat()],
                )
            ]
    return []
