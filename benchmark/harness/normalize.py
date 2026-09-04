"""Comparing what the app extracted with what the dataset printed.

The dataset renders one value seven ways (₹2,64,656.00 / Rs. 2,64,656.00 /
INR 264,656.00; 09-Aug-2026 / 09/08/2026 / 2026-08-09 ...). A comparison that
does not normalise first measures the renderer, not the extractor.
"""

from __future__ import annotations

import re
from datetime import date, datetime

DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%Y/%m/%d",
    "%d-%b-%Y",
    "%d.%m.%Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%d %b %Y",
)


def amount(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    negative = text.startswith("-") or text.startswith("(") or "-" in text[:4]
    digits = re.sub(r"[^\d.]", "", text)
    if not digits or digits == ".":
        return None
    try:
        number = float(digits)
    except ValueError:
        return None
    return -number if negative else number


def iso_date(value) -> str | None:
    if not value:
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def ident(value) -> str:
    """Identifiers: PO numbers, invoice numbers, tax ids, WA references."""
    return "".join(ch for ch in str(value or "") if ch.isalnum()).upper()


def fold(value) -> str:
    # Same folding as general_invoice_agent._fold_name, restated here so the
    # harness does not import the Vertex stack to compare two strings.
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def amounts_equal(a, b, tolerance: float = 0.01) -> bool | None:
    """None when either side is missing -- absence is scored separately."""
    x, y = amount(a), amount(b)
    if x is None or y is None:
        return None
    return abs(x - y) <= tolerance
