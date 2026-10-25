"""Subscriptions: the latest seat count dated by the period's end vs the
seats invoiced."""

from __future__ import annotations

from decimal import Decimal

from . import bands
from .classify import dec, is_seat, iso_date


def check(r, invoice: dict, lines: list[dict]) -> bands.Check | None:
    pairs = [
        (q, dec(line["attrs"].get("unit_price")))
        for line in lines
        if is_seat(line["attrs"])
        and (q := dec(line["attrs"].get("quantity"))) is not None
    ]
    expected = sum((q for q, _ in pairs), Decimal(0))
    if expected <= 0:
        return None
    c = bands.Check(
        "subscription", "seats", expected, unit_price=bands.unit_price(pairs)
    )
    period = r.period(invoice)
    if period is None:
        return c
    counts = [
        (d, n["natural_key"], n)
        for n in r.by_vendor.get(
            (r.vendor_of.get(invoice["id"]), "seat_count"), []
        )
        if (d := iso_date(n["attrs"].get("invoice_date"))) and d <= period[1]
    ]
    if counts:
        _d, _k, latest = max(counts, key=lambda t: (t[0], t[1]))
        c.present = True
        c.evidence.append(latest["id"])
        c.found = dec(latest["attrs"].get("quantity")) or Decimal(0)
    else:
        span = f"{period[0].isoformat()}/{period[1].isoformat()}"
        r.ask(
            c, "usage", r.vendor_name(invoice), span, keys=("seats", "quantity")
        )
    return c
