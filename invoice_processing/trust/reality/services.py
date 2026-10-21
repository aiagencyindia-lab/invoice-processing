"""Services: timesheet and ticket hours vs the hours invoiced
(`wa_matching.invoiced_hours`' rule: lines priced by the hour; a day is 8)."""

from __future__ import annotations

from decimal import Decimal

from . import bands
from .classify import dec, hours_of, iso_date

KINDS = ("timesheet", "ticket")


def check(r, invoice: dict, lines: list[dict]) -> bands.Check | None:
    pairs = []
    for line in lines:
        hours = hours_of(line["attrs"])
        if hours is None:
            continue
        price = dec(line["attrs"].get("unit_price"))
        qty = dec(line["attrs"].get("quantity"))
        # The rate per hour: a day-priced line's price spread over its hours.
        rate = (
            price * qty / hours if price is not None and qty and hours else None
        )
        pairs.append((hours, rate))
    expected = sum((h for h, _ in pairs), Decimal(0))
    if expected <= 0:
        return None
    c = bands.Check(
        "services", "hours", expected, unit_price=bands.unit_price(pairs)
    )
    period = r.period(invoice)
    po = r.po_of.get(invoice["id"])
    if po:
        # Work on the PO counts when it falls in the period; undated work
        # cannot be placed, so it counts.
        work = [
            n
            for n in r.fulfils.get(po, [])
            if n["kind"] in KINDS
            and (
                period is None
                or (d := iso_date(n["attrs"].get("invoice_date"))) is None
                or period[0] <= d <= period[1]
            )
        ]
    else:
        vendor = r.vendor_of.get(invoice["id"])
        work = [
            n
            for kind in KINDS
            for n in r.by_vendor.get((vendor, kind), [])
            if period
            and (d := iso_date(n["attrs"].get("invoice_date"))) is not None
            and period[0] <= d <= period[1]
        ]
    for n in work:
        c.present = True
        c.evidence.append(n["id"])
        c.found += dec(n["attrs"].get("hours")) or Decimal(0)
    if not c.present and period:
        name = r.vendor_name(invoice)
        span = f"{period[0].isoformat()}/{period[1].isoformat()}"
        r.ask(c, "timesheets", name, span, keys=("hours",))
        if not c.present:
            r.ask(c, "tickets", f"{name} {span}", keys=("hours",))
    return c
