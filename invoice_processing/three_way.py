"""Step 2.3: invoice lines against PO lines and goods received.

Pure, like bank_check: no DB, no I/O. The caller hands in the PO book (lines,
received and billed per PO line) and gets back per-line results and signals.

Pairing is fixed-order (item code, then PO line number, then description) so
the same invoice always pairs the same way, and a PO line is claimed once. A
description is only trusted when it clearly beats the runner-up; a near tie is
left unpaired rather than guessed.

Every signal is a hold for review, never a rejection: a short delivery or a
late GRN is usually a timing problem, and a person can release the invoice
once the goods turn up.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .po_matching import _as_float

NOT_RECEIVED = "not_received"
SHORT_RECEIVED = "short_received"
PRICE_OVER_PO = "price_over_po"
OVER_ORDERED = "over_ordered"
UNPAIRED_LINE = "unpaired_line"
UNREADABLE_LINE = "unreadable_line"
UNCHECKED = "unchecked"  # the book could not be read; set by the caller
DEFAULT_QTY_TOL = 0.0

_UNITS = {
    "nos", "pcs", "pc", "kg", "kgs", "ltr", "l", "m", "mm", "cm",
    "ea", "each", "unit", "units", "set", "sets", "box", "pack", "no",
}  # fmt: skip


def _num(v: Any) -> float | None:
    # "nan"/"inf" parse as floats and slip past every comparison, so they count as unreadable
    x = _as_float(v)
    return x if x is not None and math.isfinite(x) else None


def _code(v: Any) -> str:
    return re.sub(r"[\s-]", "", str(v or "")).casefold()


def _words(text: Any) -> set[str]:
    return (
        set(re.sub(r"[^\w\s]|_", " ", str(text or "").casefold()).split())
        - _UNITS
    )


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


@dataclass(frozen=True)
class LineResult:
    line_number: int
    description: str
    po_line_number: int | None
    paired_by: str | None
    quantity: float | None
    unit_price: float | None
    billed: float | None
    received: float | None
    signals: tuple[str, ...]
    evidence: str

    def to_dict(self) -> dict:
        return {
            "line_number": self.line_number,
            "description": self.description,
            "po_line_number": self.po_line_number,
            "paired_by": self.paired_by,
            "quantity": self.quantity,
            "unit_price": self.unit_price,
            "billed": self.billed,
            "received": self.received,
            "signals": list(self.signals),
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ThreeWay:
    lines: tuple[LineResult, ...]
    signals: tuple[str, ...]
    evidence: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.signals


def _price_gap(
    price: float | None, po_line: dict, pct: float, abs_: float
) -> float:
    po_price = _num(po_line.get("unit_price"))
    if price is None or po_price is None:
        return math.inf
    d = abs(price - po_price)
    return d if d <= max(pct * po_price, abs_) else math.inf


def pair(
    invoice_lines: list[dict],
    po_lines: list[dict],
    *,
    price_tol_pct: float = 0.02,
    price_tol_abs: float = 50.0,
) -> list[tuple[int | None, str | None]]:
    out: list[tuple[int | None, str | None]] = [(None, None)] * len(
        invoice_lines
    )
    free = {p["line_number"]: p for p in po_lines}

    def claim(i: int, n: int, how: str) -> None:
        out[i] = (n, how)
        del free[n]

    for i, line in enumerate(invoice_lines):
        code = _code(line.get("item_code"))
        hit = next(
            (
                n
                for n, p in free.items()
                if code and _code(p.get("item_code")) == code
            ),
            None,
        )
        if hit is not None:
            claim(i, hit, "item_code")
    for i, line in enumerate(invoice_lines):
        n = line.get("po_line_number")
        # True == 1 and a list is unhashable: only a real int names a line
        if (
            out[i][0] is None
            and isinstance(n, int)
            and not isinstance(n, bool)
            and n in free
        ):
            claim(i, n, "line_number")
    for i, line in enumerate(invoice_lines):
        if out[i][0] is not None:
            continue
        words = _words(line.get("description"))
        scored = sorted(
            (_jaccard(words, _words(p.get("description"))), n)
            for n, p in free.items()
        )[::-1]
        if not scored or scored[0][0] < 0.6:
            continue
        best = scored[0][0]
        # different wording that scores within 0.2 of the best is a guess, so
        # stay unpaired; identical wording is one item on several PO lines
        tied = sorted(n for sc, n in scored if sc == best)
        if any(sc != best and best - sc < 0.2 for sc, _ in scored) or any(
            _words(free[n].get("description"))
            != _words(free[tied[0]].get("description"))
            for n in tied
        ):
            continue
        price = _num(line.get("unit_price"))
        gaps = {
            n: _price_gap(price, free[n], price_tol_pct, price_tol_abs)
            for n in tied
        }
        # min() keeps the first (lowest line number) among equal gaps
        claim(i, min(tied, key=gaps.__getitem__), "description")
    return out


def match(
    invoice_lines: list[dict],
    po_lines: list[dict],
    received: dict[int, float],
    billed: dict[int, float],
    *,
    vendor_kind: str | None,
    qty_tol: float = DEFAULT_QTY_TOL,
    price_tol_pct: float = 0.02,
    price_tol_abs: float = 50.0,
    grn_numbers: dict[int, list[str]] | None = None,
) -> ThreeWay:
    for name, tol in (
        ("qty_tol", qty_tol),
        ("price_tol_pct", price_tol_pct),
        ("price_tol_abs", price_tol_abs),
    ):
        # NaN fails every "over tolerance" comparison, so every line would pass
        if not math.isfinite(tol):
            raise ValueError(f"{name} must be a finite number, got {tol!r}")
    by_n = {p["line_number"]: p for p in po_lines}
    pairs = pair(
        invoice_lines,
        po_lines,
        price_tol_pct=price_tol_pct,
        price_tol_abs=price_tol_abs,
    )
    if not invoice_lines and po_lines:
        msg = "Invoice has no line items to match."
        return ThreeWay((), (UNREADABLE_LINE,), (msg,))
    results: list[LineResult] = []
    for idx, (line, (n, how)) in enumerate(
        zip(invoice_lines, pairs, strict=True), start=1
    ):
        desc = str(line.get("description") or "")
        q, p = _num(line.get("quantity")), _num(line.get("unit_price"))
        ordered, po_price = (
            (None, None)
            if n is None
            else (
                _num(by_n[n].get("qty_ordered")),
                _num(by_n[n].get("unit_price")),
            )
        )
        got, before = (
            (None, None)
            if n is None
            else (_num(received.get(n, 0)), _num(billed.get(n, 0)))
        )
        if (
            q is None
            or p is None
            or (n is not None and None in (ordered, po_price, got, before))
        ):
            msg = f"Line {idx} '{desc}': quantity or unit price could not be read."
            results.append(
                LineResult(
                    idx, desc, n, how, q, p, None, None, (UNREADABLE_LINE,), msg
                )
            )
            continue
        if n is None:
            msg = f"Invoice line {idx} '{desc}' matched no PO line."
            results.append(
                LineResult(
                    idx,
                    desc,
                    None,
                    None,
                    q,
                    p,
                    None,
                    None,
                    (UNPAIRED_LINE,),
                    msg,
                )
            )
            continue
        po_line = by_n[n]
        ordered = _num(po_line.get("qty_ordered")) or 0.0
        po_price = _num(po_line.get("unit_price")) or 0.0
        got, before = (
            float(received.get(n, 0) or 0),
            float(billed.get(n, 0) or 0),
        )
        required = po_line.get("receipt_required")
        if required is None:
            required = vendor_kind != "service"  # unknown vendor kind holds
        total = before + q
        signals: list[str] = []
        notes: list[str] = []
        if total > ordered:
            signals.append(OVER_ORDERED)
            notes.append(
                f"Line {idx} '{desc}': billed {before:g} + {q:g} exceeds {ordered:g} ordered"
            )
        elif required and total > got + qty_tol:
            signals.append(NOT_RECEIVED if got == 0 else SHORT_RECEIVED)
            grn = (
                f" ({', '.join(grn_numbers[n])})"
                if grn_numbers and grn_numbers.get(n)
                else ""
            )
            notes.append(
                f"Line {idx} '{desc}': billed {total:g}, received {got:g}{grn}; {total - got:g} still to arrive."
            )
        if p > po_price + max(price_tol_pct * po_price, price_tol_abs):
            signals.append(PRICE_OVER_PO)
            pct = (p - po_price) / po_price if po_price else 0.0
            notes.append(
                f"Line {idx} '{desc}': {p:.2f} against PO {po_price:.2f} (+{pct:.0%})"
            )
        results.append(
            LineResult(
                idx,
                desc,
                n,
                how,
                q,
                p,
                before,
                got,
                tuple(signals),
                " ".join(notes),
            )
        )
    signals_all = tuple(dict.fromkeys(s for r in results for s in r.signals))
    return ThreeWay(
        tuple(results),
        signals_all,
        tuple(r.evidence for r in results if r.signals),
    )
