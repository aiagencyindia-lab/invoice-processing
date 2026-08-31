"""Per-field extraction confidence, computed rather than asked for.

Confidence is the mechanism that turns extraction into a touchless rate: fields
above the threshold post automatically, fields below route to a human. Without
it every invoice needs review and there is no product.

The pipeline already had three `confidence` fields, all of them LLM
self-reports, and all dead code. That is the right thing to have deleted:
self-reported confidence is uncalibrated, and this codebase has already measured
the model contradicting itself ~40% of the time at temperature=0 on a
classification task. Routing money on that number would be a coin flip wearing a
lab coat.

Two deterministic signals instead, in order of strength:

1. **Text-layer grounding.** Every extracted scalar is searched for in the PDF's
   own text layer. A value the document does not contain is the strongest
   hallucination signal available, and locating it also yields the page and
   bounding box the reviewer UI needs to highlight the field.

2. **Arithmetic consistency.** Lines sum to subtotal, ex + tax = inc, quantity x
   unit price = line total. These are the errors that cost money, and precisely
   the ones a model's self-report never catches -- it will report 0.95 on a
   number it transposed.

Both are free, instant and reproducible. Scanned pages with no text layer return
`grounded=None`, which is honest: "could not check" routes to review rather than
inventing either a pass or a failure.

Deliberately deferred: dual-pass agreement (doubles extraction cost for a signal
largely redundant with grounding) and vendor-history matching (needs a vendor
master, which is post-pilot).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Coarse buckets on purpose. A three-valued signal you can explain to a design
# partner beats a float nobody can defend.
HIGH, MEDIUM, LOW = 1.0, 0.6, 0.3

# Fields worth grounding. Line items are handled separately.
GROUNDED_FIELDS = (
    "invoice_number",
    "invoice_date",
    "vendor_name",
    "vendor_tax_id",
    "invoice_total_inc_tax",
    "invoice_total_ex_tax",
    "tax_amount",
    "po_number",
    "due_date",
)

_AMOUNT_TOLERANCE = 0.02


@dataclass
class FieldEvidence:
    """Where a value came from, and how much to trust it."""

    value: Any = None
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    grounded: bool | None = None  # None = document has no text layer
    checks: dict[str, bool] = field(default_factory=dict)
    confidence: float = LOW

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _candidates(value: Any) -> list[str]:
    """Renderings of a value that might appear in the document.

    An invoice showing "$1,069.32" must match an extracted 1069.32, and a date
    stored as 2026-02-11 must match "11/02/2026". Without this, every correct
    number gets flagged and the signal is worthless.
    """
    if value is None:
        return []
    raw = str(value).strip()
    if not raw:
        return []

    out = {raw, raw.lower()}

    number = _as_number(raw)
    if number is not None:
        out.update(
            {
                f"{number:,.2f}",
                f"{number:.2f}",
                f"{number:,.0f}" if number == int(number) else f"{number:,.2f}",
                str(number),
                f"{number:.2f}".rstrip("0").rstrip("."),
            }
        )

    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if iso:
        year, month, day = iso.groups()
        out.update(
            {
                f"{day}/{month}/{year}",
                f"{month}/{day}/{year}",
                f"{day}-{month}-{year}",
                f"{int(day)}/{int(month)}/{year}",
            }
        )

    # Tax IDs and reference numbers are often spaced or hyphenated.
    compact = re.sub(r"[\s\-]", "", raw)
    if compact != raw and compact:
        out.add(compact)

    return [c for c in out if c]


def _as_number(value: Any) -> float | None:
    try:
        return float(re.sub(r"[^\d.\-]", "", str(value)))
    except (TypeError, ValueError):
        return None


def _normalize_page(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").lower()


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


def extract_page_text(pdf_path: Path) -> list[str]:
    """Return per-page text. Empty list when the PDF has no text layer."""
    try:
        import pdfplumber  # noqa: PLC0415

        with pdfplumber.open(str(pdf_path)) as pdf:
            return [page.extract_text() or "" for page in pdf.pages]
    except Exception:
        return []


def locate(value: Any, pages: list[str]) -> tuple[bool | None, int | None]:
    """Find a value in the document text.

    Returns (grounded, page_number). `grounded` is None when there is no text
    layer to search -- a scanned image is not evidence that the value is wrong.
    """
    if not pages or not any(p.strip() for p in pages):
        return None, None

    options = [_normalize_page(c) for c in _candidates(value)]
    if not options:
        return None, None

    # Separators differ between the extracted value and the page: an ABN stored
    # as "51824753556" is printed "51 824 753 556". Matching only on the page as
    # written would flag a correct value as ungrounded, which is exactly the
    # false alarm that makes a confidence signal useless.
    compact_options = {_compact(o) for o in options if _compact(o)}

    for index, page_text in enumerate(pages, start=1):
        haystack = _normalize_page(page_text)
        if any(option and option in haystack for option in options):
            return True, index
        compact_haystack = _compact(haystack)
        if compact_haystack and any(
            option in compact_haystack for option in compact_options
        ):
            return True, index
    return False, None


def _compact(text: str) -> str:
    """Alphanumerics only, for separator-insensitive comparison."""
    return "".join(ch for ch in str(text) if ch.isalnum())


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------


def arithmetic_checks(invoice: dict) -> dict[str, bool]:
    """Cross-field consistency. These are the errors that cost money."""
    checks: dict[str, bool] = {}

    ex = _as_number(invoice.get("invoice_total_ex_tax"))
    tax = _as_number(invoice.get("tax_amount"))
    inc = _as_number(invoice.get("invoice_total_inc_tax"))

    if None not in (ex, tax, inc):
        checks["totals_reconcile"] = abs((ex + tax) - inc) <= _AMOUNT_TOLERANCE

    lines = invoice.get("line_items") or []
    if lines and ex is not None:
        line_sum = sum(_as_number(li.get("amount_ex_tax")) or 0 for li in lines)
        checks["lines_sum_to_subtotal"] = abs(line_sum - ex) <= 1.00

    if lines:
        consistent = True
        for line in lines:
            qty = _as_number(line.get("quantity"))
            price = _as_number(line.get("unit_price"))
            amount = _as_number(line.get("amount_ex_tax"))
            if None not in (qty, price, amount):
                consistent &= abs(qty * price - amount) <= _AMOUNT_TOLERANCE
        checks["line_maths_consistent"] = consistent

    return checks


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score(grounded: bool | None, arithmetic_ok: bool | None) -> float:
    """Combine the signals into a defensible bucket.

    No text layer means the strongest signal is unavailable, so the field is
    capped at MEDIUM even when the arithmetic agrees -- it routes to review
    rather than claiming a confidence nothing supports.
    """
    if grounded is None:
        return MEDIUM if arithmetic_ok else LOW
    if grounded and arithmetic_ok is not False:
        return HIGH
    if grounded or arithmetic_ok:
        return MEDIUM
    return LOW


def build_evidence(invoice: dict, pdf_path: Path | None) -> dict[str, dict]:
    """Per-field evidence for an extracted invoice.

    Returned as plain dicts so it serializes straight into the extraction
    artifact, alongside the existing `_tax_id_validation` convention.
    """
    pages = extract_page_text(pdf_path) if pdf_path else []
    checks = arithmetic_checks(invoice)

    # Arithmetic verdicts are invoice-wide; attach the relevant ones per field.
    totals_ok = checks.get("totals_reconcile")
    relevant = {
        "invoice_total_inc_tax": totals_ok,
        "invoice_total_ex_tax": totals_ok,
        "tax_amount": totals_ok,
    }

    evidence: dict[str, dict] = {}
    for name in GROUNDED_FIELDS:
        if name not in invoice:
            continue
        value = invoice.get(name)
        # An optional field the invoice does not carry (no PO number, no due
        # date) has nothing to ground. Scoring it low would push every invoice
        # to review the moment the schema gained an optional field -- absence of
        # a value is not low-confidence extraction of one.
        if value is None or str(value).strip() == "":
            continue
        grounded, page = locate(value, pages)
        arithmetic_ok = relevant.get(name)
        evidence[name] = FieldEvidence(
            value=value,
            page=page,
            bbox=None,  # page-level for now; word boxes are a UI refinement
            grounded=grounded,
            checks=dict(checks) if arithmetic_ok is not None else {},
            confidence=score(grounded, arithmetic_ok),
        ).to_dict()

    evidence["_summary"] = {
        "has_text_layer": bool(pages and any(p.strip() for p in pages)),
        "arithmetic": checks,
        "min_confidence": min(
            (
                e["confidence"]
                for e in evidence.values()
                if isinstance(e, dict) and "confidence" in e
            ),
            default=LOW,
        ),
        "fields_needing_review": sorted(
            name
            for name, e in evidence.items()
            if isinstance(e, dict) and e.get("confidence", LOW) < HIGH
        ),
    }
    return evidence


def routing_decision(evidence: dict, threshold: float = HIGH) -> str:
    """AUTO or REVIEW, from the weakest field.

    A single low-confidence field is enough to require review: an invoice is
    only as trustworthy as its worst number.
    """
    summary = evidence.get("_summary", {})
    return (
        "AUTO" if summary.get("min_confidence", LOW) >= threshold else "REVIEW"
    )
