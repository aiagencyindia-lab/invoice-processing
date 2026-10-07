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
#
# The bank details are here for the reason the whole module exists. Payment
# redirection works by changing an account number, and an account number that
# does not appear verbatim in the document it was supposedly read from is the
# strongest cheap signal there is. They were the only fields the extractor
# populated and the grounder ignored -- so the highest-risk values on the
# invoice arrived in the workbench marked "not corroborated", which reads as
# "we did not look" and was exactly right.
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
    "bank_account_name",
    "bank_account_number",
    "bank_code",
    "bank_iban",
    "vendor_address",
)

# Deliberately not grounded: `payment_terms` is paraphrased by the extractor --
# "payment due within 30 days" on the page becomes "Net 30" in the field -- so
# a verbatim search flags a correct value. A signal that fires on every clean
# invoice teaches reviewers to ignore it, which costs more than not having it.
# `tax_breakdown` is a list and is checked arithmetically instead.

_AMOUNT_TOLERANCE = 0.02

_MONTHS = (
    ("jan", "january"),
    ("feb", "february"),
    ("mar", "march"),
    ("apr", "april"),
    ("may", "may"),
    ("jun", "june"),
    ("jul", "july"),
    ("aug", "august"),
    ("sep", "september"),
    ("oct", "october"),
    ("nov", "november"),
    ("dec", "december"),
)


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
        # Indian invoices print the month as a word -- "13-May-2026",
        # "13 May 2026" -- and none of the numeric renderings above match, so
        # every correct date was flagged. A fixed table, not strftime: %b
        # follows the process locale.
        short, full = _MONTHS[int(month) - 1]
        for name in (short, full):
            out.update(
                {
                    f"{day}-{name}-{year}",
                    f"{day} {name} {year}",
                    f"{int(day)} {name} {year}",
                    f"{name} {int(day)}, {year}",
                    f"{name} {day}, {year}",
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


# A page printed from a browser carries a header and footer -- a timestamp, the
# source URL, "1/1" -- even when the page itself is a single image. That is
# text, so "does this PDF have any text?" answers yes and every field is then
# reported as *not found in the document*, which reads as an accusation that
# the extractor invented them. The truth is that nothing could be checked.
#
# A real invoice's text layer is never this thin: vendor, number, date, a line
# or two, and three totals already run past thirty words. Below that the
# document is treated as an image, which caps every field at review rather
# than claiming a verdict the page cannot support.
MIN_TEXT_LAYER_WORDS = 30


def has_readable_text(pages: list[str]) -> bool:
    """Whether there is enough text on the page to check a value against."""
    return (
        sum(len(page.split()) for page in pages or []) >= MIN_TEXT_LAYER_WORDS
    )


def locate(value: Any, pages: list[str]) -> tuple[bool | None, int | None]:
    """Find a value in the document text.

    Returns (grounded, page_number). `grounded` is None when there is no text
    layer to search -- a scanned image is not evidence that the value is wrong.
    """
    if not has_readable_text(pages):
        return None, None

    options = [_normalize_page(c) for c in _candidates(value)]
    if not options:
        return None, None

    # Separators differ between the extracted value and the page: an ABN stored
    # as "51824753556" is printed "51 824 753 556", and an Indian amount groups
    # its digits "13,41,792.00". So anything that is not a letter or digit may
    # sit between the characters. What may not happen is the value being part
    # of something longer: a substring search found the placeholder 0.00 inside
    # 10.00 and a truncated GSTIN inside the printed one, confirming values the
    # page never states.
    patterns = [
        re.compile(
            _NOT_AFTER_TOKEN
            + _ANY_SEPARATOR.join(map(re.escape, option))
            + _NOT_BEFORE_TOKEN
        )
        for option in {_compact(o) for o in options} - {""}
    ]

    for index, page_text in enumerate(pages, start=1):
        haystack = _normalize_page(page_text)
        if any(pattern.search(haystack) for pattern in patterns):
            return True, index
    return False, None


# A token boundary: no letter or digit against the value, and no digit run
# continuing it through a decimal point or digit-group comma ("1" + "0.00").
_NOT_AFTER_TOKEN = r"(?<![0-9a-z])(?<![0-9][.,])"
_NOT_BEFORE_TOKEN = r"(?![0-9a-z])(?![.,][0-9])"
_ANY_SEPARATOR = r"[^0-9a-z]{0,3}"


def _compact(text: str) -> str:
    """Alphanumerics only, for separator-insensitive comparison."""
    return "".join(ch for ch in str(text) if ch.isalnum())


def _locate_taxable_by_parts(
    invoice: dict, total: Any, pages: list[str]
) -> tuple[bool, int | None]:
    """A taxable total the page never prints, grounded through its parts.

    GST layouts print "Sub Total" and "Other Charges" and no line for their
    sum, so the extracted taxable total was found nowhere on five clean
    invoices. It is grounded when the subtotal and every charge are on the page
    and subtotal - discount + charges comes to it.
    """
    subtotal = invoice.get("subtotal")
    total = _as_number(total)
    if subtotal is None or total is None:
        return False, None
    charges = [c.get("amount") for c in invoice.get("other_charges") or []]
    discount = invoice.get("discount") or 0
    parts = [
        subtotal,
        *[c for c in charges if c],
        *([discount] if discount else []),
    ]
    computed = (
        (_as_number(subtotal) or 0)
        - (_as_number(discount) or 0)
        + sum(_as_number(c) or 0 for c in charges)
    )
    if abs(computed - total) > _AMOUNT_TOLERANCE:
        return False, None
    found = [locate(part, pages) for part in parts]
    if all(grounded for grounded, _ in found):
        return True, found[0][1]
    return False, None


def _locate_by_segments(
    value: Any, pages: list[str]
) -> tuple[bool, int | None]:
    """An address grounded line by line.

    An address wraps, and a logo or the other party's block can sit on the same
    text row: the page read "Ludhiana 668868, BF" / "Punjab", with the logo's
    letters between, so the whole string was never found although every part of
    it is printed. Each comma-separated segment must be on the page.
    """
    segments = [_compact(s).lower() for s in str(value or "").split(",")]
    segments = [s for s in segments if s]
    if len(segments) < 2 or not has_readable_text(pages):
        return False, None
    # Text, not `locate`: it reads any string with digits as a number, so
    # "Amritsar 668868" was found as 668868 alone.
    for index, page in enumerate(pages, start=1):
        haystack = _compact(page).lower()
        if all(segment in haystack for segment in segments):
            return True, index
    return False, None


def _locate_tax_by_components(
    invoice: dict, total: Any, pages: list[str]
) -> tuple[bool, int | None]:
    """A tax total the page never prints, grounded through the parts it does.

    A GST invoice prints CGST and SGST (or IGST) and often no total-tax line,
    so the extracted total is found nowhere and was flagged on every such
    invoice. It is grounded when every component is on the page and they add
    up to it -- a stronger claim than finding the total would have been.
    """
    parts = [
        invoice.get(key)
        for key in ("cgst_amount", "sgst_amount", "igst_amount")
        if invoice.get(key)
    ]
    total = _as_number(total)
    if not parts or total is None:
        return False, None
    if abs(sum(_as_number(p) or 0 for p in parts) - total) > _AMOUNT_TOLERANCE:
        return False, None
    found = [locate(part, pages) for part in parts]
    if all(grounded for grounded, _ in found):
        return True, found[0][1]
    return False, None


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------


def line_discount(
    line: dict, quantity: float, unit_price: float
) -> tuple[float, bool]:
    """What this line's discount takes off, and whether we could read it.

    Invoices print the discount column as a percentage far more often than as
    an amount, and the extractor returns whichever it sees -- so a value below
    one is a rate rather than a sum of money. A discount that is shown but not
    readable returns False, and the caller lets the line pass: an unreadable
    column is our problem and not the supplier's.
    """
    rate = line.get("line_discount_pct")
    amount = line.get("line_discount")
    if rate is None and amount is not None and 0 < float(amount) < 1:
        # "10%" read as 0.1. A real discount of under one unit of currency is
        # not worth the false rejections the other reading causes.
        rate, amount = float(amount), None
    discount = 0.0
    if rate is not None:
        discount += quantity * unit_price * float(rate)
    if amount is not None:
        discount += float(amount)
    if rate is None and amount is None and line.get("has_discount"):
        return 0.0, False
    return discount, True


def charges_and_lines(invoice: dict) -> tuple[list[dict], list[dict]]:
    """The invoice's line items and its other charges, each counted once.

    Freight, packing and travel are printed outside the line table, and the
    extractor frequently returns them in both places -- as a line *and* as an
    other-charge, sometimes describing the charge only as "Other Charges" so
    the two descriptions do not even match. Counting both makes the lines
    overshoot the subtotal and the expected tax overshoot by the charge's tax:
    on one invoice by 455.94, which is 2,533.00 of packing at 18% to the cent.

    The test is arithmetic rather than textual, because the arithmetic is what
    went wrong: a charge is a duplicate when dropping it brings the line sum
    back to the subtotal the invoice prints. Where the sums do not say so, both
    are kept -- two things costing the same are not the same thing.

    The same charge can also arrive *only* as a line: the document prints it in
    the item table and again below a subtotal that leaves it out, and the
    extractor returns the table alone. When the lines then add up to the
    taxable total the invoice prints, and exactly one line is the amount the
    subtotal leaves out, that line is the charge (MFG-INV-042, packing 2,209).
    """
    lines = list(invoice.get("line_items") or [])
    charges = list(invoice.get("other_charges") or [])
    if not charges and lines:
        return _charge_printed_as_line(invoice, lines)
    if not charges or not lines:
        return lines, charges

    printed_base = invoice.get("subtotal")
    if printed_base is None:
        printed_base = invoice.get("invoice_total_ex_tax")
    if printed_base is None:
        return lines, charges

    line_sum = sum((line.get("amount_ex_tax") or 0) for line in lines)
    overshoot = line_sum - printed_base
    if overshoot <= 0.01:
        return lines, charges

    kept, dropped = [], 0.0
    remaining = list(charges)
    for line in lines:
        amount = line.get("amount_ex_tax")
        twin = next(
            (
                c
                for c in remaining
                if amount is not None
                and c.get("amount") is not None
                and abs(float(c["amount"]) - float(amount)) <= 0.01
            ),
            None,
        )
        if (
            twin is not None
            and dropped + float(twin["amount"]) <= overshoot + 0.01
        ):
            remaining.remove(twin)
            dropped += float(twin["amount"])
            continue
        kept.append(line)
    # Only if removing them actually reconciles the lines with the subtotal.
    if abs(dropped - overshoot) <= 0.01:
        return kept, charges
    return lines, charges


def _charge_printed_as_line(
    invoice: dict, lines: list[dict]
) -> tuple[list[dict], list[dict]]:
    """The one line a printed subtotal leaves out, moved to the charges.

    Two figures the invoice prints must agree before anything moves: the lines
    overshoot the subtotal by exactly one line's amount, and they equal the
    taxable total. A wrong subtotal satisfies the first only by coincidence and
    the second not at all, so a line-sum fault still fails 4.2.
    """
    subtotal = invoice.get("subtotal")
    taxable = invoice.get("invoice_total_ex_tax")
    if subtotal is None or taxable is None:
        return lines, []
    line_sum = sum((line.get("amount_ex_tax") or 0) for line in lines)
    overshoot = line_sum - subtotal
    if overshoot <= 0.01 or abs(line_sum - taxable) > 0.01:
        return lines, []
    # ponytail: one line only; a subtotal leaving out two charges is not
    # repaired -- add a subset search if the benchmark ever shows one.
    matches = [
        line
        for line in lines
        if line.get("amount_ex_tax") is not None
        and abs(float(line["amount_ex_tax"]) - overshoot) <= 0.01
    ]
    if len(matches) != 1:
        return lines, []
    charge = matches[0]
    return [line for line in lines if line is not charge], [
        {
            "description": charge.get("description"),
            "amount": charge["amount_ex_tax"],
            "tax_rate": charge.get("tax_rate"),
        }
    ]


def arithmetic_checks(invoice: dict) -> dict[str, bool]:
    """Cross-field consistency. These are the errors that cost money."""
    checks: dict[str, bool] = {}

    ex = _as_number(invoice.get("invoice_total_ex_tax"))
    tax = _as_number(invoice.get("tax_amount"))
    inc = _as_number(invoice.get("invoice_total_inc_tax"))

    if None not in (ex, tax, inc):
        # The round-off line is printed and charged; leaving it out flagged
        # every invoice rounded to whole rupees, which check 4.1 already counts.
        round_off = _as_number(invoice.get("round_off")) or 0
        checks["totals_reconcile"] = (
            abs((ex + tax + round_off) - inc) <= _AMOUNT_TOLERANCE
        )

    # Against the printed subtotal with charges netted out, as check 4.2 does.
    # Against the taxable total, every clean invoice that prints a freight or
    # packing charge failed: the charge is in that total and not in the lines.
    lines, _ = charges_and_lines(invoice)
    base = _as_number(invoice.get("subtotal"))
    base = base if base is not None else ex
    if lines and base is not None:
        line_sum = sum(_as_number(li.get("amount_ex_tax")) or 0 for li in lines)
        checks["lines_sum_to_subtotal"] = abs(line_sum - base) <= 1.00

    if lines:
        consistent = True
        for line in lines:
            qty = _as_number(line.get("quantity"))
            price = _as_number(line.get("unit_price"))
            amount = _as_number(line.get("amount_ex_tax"))
            if None not in (qty, price, amount):
                discount, readable = line_discount(line, qty, price)
                if readable:
                    consistent &= (
                        abs(qty * price - discount - amount)
                        <= _AMOUNT_TOLERANCE
                    )
        checks["line_maths_consistent"] = consistent

    # The per-rate breakdown was extracted and then never used. It is the one
    # place a wrong rate shows up as an internal contradiction rather than a
    # plausible number: rates that sum to something other than the stated tax
    # mean one of the two is wrong, and neither is obviously the wrong one.
    breakdown = invoice.get("tax_breakdown") or []
    if breakdown and tax is not None:
        parts = [_as_number(line.get("tax_amount")) for line in breakdown]
        if all(part is not None for part in parts):
            checks["tax_breakdown_sums_to_tax"] = (
                abs(sum(parts) - tax) <= _AMOUNT_TOLERANCE
            )

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
        if name == "tax_amount" and grounded is False:
            grounded, page = _locate_tax_by_components(invoice, value, pages)
        if name == "invoice_total_ex_tax" and grounded is False:
            grounded, page = _locate_taxable_by_parts(invoice, value, pages)
        if name == "vendor_address" and grounded is False:
            grounded, page = _locate_by_segments(value, pages)
        arithmetic_ok = relevant.get(name)
        evidence[name] = FieldEvidence(
            value=value,
            page=page,
            bbox=None,  # page-level for now; word boxes are a UI refinement
            grounded=grounded,
            checks=dict(checks) if arithmetic_ok is not None else {},
            confidence=score(grounded, arithmetic_ok),
        ).to_dict()

    # An invoice whose own arithmetic contradicts itself is not trustworthy
    # however well each field is grounded. The line checks were computed and
    # then ignored for routing; a contradiction now caps the invoice at review.
    contradictions = sorted(name for name, ok in checks.items() if ok is False)
    field_floor = min(
        (
            e["confidence"]
            for e in evidence.values()
            if isinstance(e, dict) and "confidence" in e
        ),
        default=LOW,
    )
    evidence["_summary"] = {
        "has_text_layer": has_readable_text(pages),
        "arithmetic": checks,
        "min_confidence": min(field_floor, MEDIUM)
        if contradictions
        else field_floor,
        "fields_needing_review": sorted(
            name
            for name, e in evidence.items()
            if isinstance(e, dict) and e.get("confidence", LOW) < HIGH
        )
        + [f"arithmetic: {name}" for name in contradictions],
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
