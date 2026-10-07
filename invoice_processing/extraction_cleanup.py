"""Deterministic repairs to what the extractor returns, before anything reads it.

Each of these fixes a way the model's output differs from what the document
prints, where the difference is mechanical and the correct value is not in
doubt. None of them guesses: a field that is merely uncertain is left alone,
because a repair that is sometimes wrong is worse than a reading that is
honestly unsure.

* **Homoglyphs in identifiers.** The extractor returns Greek capitals that are
  pixel-identical to Latin ones -- Greek Tau, Rho, Alpha where the document
  prints `TPA/26/0200`.
  They are different characters, so the invoice number matches nothing: the
  duplicate check misses a resubmission, and a purchase-order or
  work-authorization reference is "not found". Six clean, text-layer invoices
  in the benchmark came back this way.
* **A letter read as a digit.** The extractor returns `240XMPF...` where the
  page's text layer prints `24OXMPF...`. Where the page has a text layer, the
  identifier is taken as printed -- see `read_as_printed`. Scans are left
  alone: there is nothing printed to read, and folding O into 0 blindly would
  turn a forged tax ID into a valid one.
* **A charge returned twice.** Freight and packing are printed outside the line
  table, and the extractor repeats them as line items often enough that every
  downstream reader -- the workbench, the exports, the reconciliation -- sees a
  line that is not on the document. Removed here once, rather than
  compensated for in each reader. Where the extractor returns the charge only
  as a table line, it is moved to the other charges by the same test.
"""

from __future__ import annotations

import re

# Greek and Cyrillic capitals that are drawn identically to a Latin capital.
# Only the unambiguous look-alikes: a character that merely resembles another
# is left alone rather than guessed at. Written as code points because the
# whole problem is that nobody can tell these apart by looking -- including
# whoever reviews this table.
_CONFUSABLES = str.maketrans(
    {
        "\u0391": "A",  # GREEK CAPITAL LETTER ALPHA
        "\u0392": "B",  # GREEK CAPITAL LETTER BETA
        "\u0395": "E",  # GREEK CAPITAL LETTER EPSILON
        "\u0396": "Z",  # GREEK CAPITAL LETTER ZETA
        "\u0397": "H",  # GREEK CAPITAL LETTER ETA
        "\u0399": "I",  # GREEK CAPITAL LETTER IOTA
        "\u039a": "K",  # GREEK CAPITAL LETTER KAPPA
        "\u039c": "M",  # GREEK CAPITAL LETTER MU
        "\u039d": "N",  # GREEK CAPITAL LETTER NU
        "\u039f": "O",  # GREEK CAPITAL LETTER OMICRON
        "\u03a1": "P",  # GREEK CAPITAL LETTER RHO
        "\u03a4": "T",  # GREEK CAPITAL LETTER TAU
        "\u03a5": "Y",  # GREEK CAPITAL LETTER UPSILON
        "\u03a7": "X",  # GREEK CAPITAL LETTER CHI
        "\u0410": "A",  # CYRILLIC CAPITAL LETTER A
        "\u0412": "B",  # CYRILLIC CAPITAL LETTER VE
        "\u0415": "E",  # CYRILLIC CAPITAL LETTER IE
        "\u041a": "K",  # CYRILLIC CAPITAL LETTER KA
        "\u041c": "M",  # CYRILLIC CAPITAL LETTER EM
        "\u041d": "H",  # CYRILLIC CAPITAL LETTER EN
        "\u041e": "O",  # CYRILLIC CAPITAL LETTER O
        "\u0420": "P",  # CYRILLIC CAPITAL LETTER ER
        "\u0421": "C",  # CYRILLIC CAPITAL LETTER ES
        "\u0422": "T",  # CYRILLIC CAPITAL LETTER TE
        "\u0425": "X",  # CYRILLIC CAPITAL LETTER HA
        "\u0423": "Y",  # CYRILLIC CAPITAL LETTER U
        "\u0405": "S",  # CYRILLIC CAPITAL LETTER DZE
        "\u0406": "I",  # CYRILLIC CAPITAL LETTER BYELORUSSIAN-UKRAINIAN I
        "\u0408": "J",  # CYRILLIC CAPITAL LETTER JE
    }
)

# Fields that are identifiers: compared character by character, never read as
# prose, so a homoglyph in one is always an error.
IDENTIFIER_FIELDS = (
    "invoice_number",
    "po_number",
    "work_authorization_reference",
    "vendor_tax_id",
    "customer_tax_id",
)


def fold_homoglyphs(value):
    """Latin in place of look-alike Greek and Cyrillic capitals."""
    if not isinstance(value, str):
        return value
    return value.translate(_CONFUSABLES)


# Characters a reader confuses, letter for digit. Only the pairs seen: the
# extractor read GSTIN 24OXMPF... as 240XMPF... on a page whose text layer
# prints the letter, and rejected a clean invoice for an invalid tax ID.
# ponytail: O/0 and I/1 only; add S/5 or B/8 when an extraction shows one.
_MISREAD = {"O": "[O0]", "0": "[O0]", "I": "[I1]", "1": "[I1]"}


def _alnum(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


def read_as_printed(value, pages: list[str]):
    """The page's own spelling of an identifier the extractor misread.

    Not a repair: nothing is folded or guessed. When the value is not on the
    page and exactly one string on the page differs from it only by O/0 or I/1,
    the printed string is what the document says -- a forged ID printed on the
    page is taken as printed, so check 3.2 still sees the forgery. Anything
    else, including a scan with no text layer, leaves the value alone.
    """
    if not isinstance(value, str) or not pages:
        return value
    target = _alnum(value)
    if not target:
        return value
    page = _alnum(" ".join(pages))
    if target.upper() in page.upper():
        return value
    pattern = "".join(_MISREAD.get(ch.upper(), re.escape(ch)) for ch in target)
    found = set(re.findall(pattern, page, flags=re.IGNORECASE))
    if len(found) != 1:
        return value
    printed = iter(found.pop())
    # Keep the value's own separators ("CH/2026-27/0569"); swap only the
    # alphanumerics for the page's.
    return "".join(next(printed) if ch.isalnum() else ch for ch in value)


def clean(invoice: dict, *, charges_and_lines=None, pages=None) -> dict:
    """Apply every repair, returning the invoice and a note of what changed.

    `charges_and_lines` is the pipeline's own duplicate-charge test, passed in
    so there is exactly one definition of "the same charge read twice" -- the
    one the reconciliation checks already use. `pages` is the document's text
    layer, when it has one.
    """
    repairs: list[str] = []

    for field in IDENTIFIER_FIELDS:
        before = invoice.get(field)
        after = fold_homoglyphs(before)
        if after != before:
            invoice[field] = after
            repairs.append(
                f"{field}: look-alike characters replaced ({before!r} -> {after!r})"
            )
        printed = read_as_printed(after, pages or [])
        if printed != after:
            invoice[field] = printed
            repairs.append(
                f"{field}: read as the page prints it ({after!r} -> {printed!r})"
            )

    for line in invoice.get("line_items") or []:
        before = line.get("hsn_sac")
        after = fold_homoglyphs(before)
        if after != before:
            line["hsn_sac"] = after

    if charges_and_lines is not None:
        had_charges = bool(invoice.get("other_charges"))
        lines, charges = charges_and_lines(invoice)
        dropped = len(invoice.get("line_items") or []) - len(lines)
        if dropped:
            invoice["line_items"] = lines
            invoice["other_charges"] = charges
            repairs.append(
                f"{dropped} line item(s) removed that repeated an other charge"
                if had_charges
                else f"{dropped} line item(s) moved to other charges: the "
                "printed subtotal leaves them out"
            )

    if repairs:
        invoice["_repairs"] = repairs
    return invoice
