"""What kind of supply an invoice is, deterministically (FR-23.1)."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from ...wa_matching import HOUR_UNITS

DAY_UNITS = frozenset({"day", "days", "md", "man-day", "manday"})
TIME_UNITS = frozenset(HOUR_UNITS) | DAY_UNITS
HOURS_PER_DAY = Decimal(8)
_SEAT = re.compile(r"\b(seat|user|licen[cs]e|subscription)s?\b", re.I)


def dec(value) -> Decimal | None:
    """A finite Decimal, or None for absent or unparseable input."""
    if value in (None, ""):
        return None
    try:
        out = Decimal(str(value))
    except InvalidOperation:
        return None
    return out if out.is_finite() else None


def iso_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _unit(attrs: dict) -> str:
    return str(attrs.get("unit") or "").strip().lower()


def hours_of(attrs: dict) -> Decimal | None:
    """A time-priced line's hours (a day is 8); None for any other line."""
    unit, qty = _unit(attrs), dec(attrs.get("quantity"))
    if unit not in TIME_UNITS or qty is None:
        return None
    return qty * HOURS_PER_DAY if unit in DAY_UNITS else qty


def is_seat(attrs: dict) -> bool:
    return bool(_SEAT.search(str(attrs.get("description") or "")))


def is_goods(attrs: dict) -> bool:
    qty = dec(attrs.get("quantity"))
    return qty is not None and qty > 1 and _unit(attrs) not in TIME_UNITS


def classify(invoice: dict, lines: list[dict]) -> str:
    """services > subscription > goods > other: a timesheet-billed job on a
    PO is still services, and a seat line is not a pallet of goods."""
    a = invoice.get("attrs") or {}
    attrs = [ln.get("attrs") or {} for ln in lines]
    if a.get("work_authorization_reference") or any(
        hours_of(x) is not None for x in attrs
    ):
        return "services"
    if any(is_seat(x) for x in attrs):
        return "subscription"
    if a.get("po_number") or any(is_goods(x) for x in attrs):
        return "goods"
    return "other"
