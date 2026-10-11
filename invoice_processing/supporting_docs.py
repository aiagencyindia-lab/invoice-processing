"""Scanned purchase orders and goods receipts, read for a person to confirm.

The extractor transcribes the page; nothing here decides anything. Each value
is checked against the page's own text so the review drawer can show which
ones the page confirms, and the confirmed result is handed to the same import
validation an ERP upload goes through. Pure apart from the injected model
call and reading the PDF's text.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from . import confidence, three_way
from .extraction_cleanup import fold_homoglyphs, read_as_printed

PURCHASE_ORDER = "purchase_order"
GOODS_RECEIPT = "goods_receipt"


class PoLineExtraction(BaseModel):
    line_number: int | None = None
    item_code: str | None = None
    description: str = ""
    quantity: float | None = None
    unit: str | None = None
    unit_price: float | None = None
    hsn_sac: str | None = None


class PurchaseOrderExtraction(BaseModel):
    po_number: str | None = None
    po_date: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    vendor_name: str | None = None
    vendor_tax_id: str | None = None
    customer_name: str | None = None
    currency: str | None = None
    total_amount: float | None = None
    amount_basis: str | None = None
    lines: list[PoLineExtraction] = []


class GrnLineExtraction(BaseModel):
    line_number: int | None = None
    item_code: str | None = None
    description: str = ""
    quantity_received: float | None = None
    unit: str | None = None
    remarks: str | None = None


class GoodsReceiptExtraction(BaseModel):
    grn_number: str | None = None
    received_on: str | None = None
    po_number: str | None = None
    vendor_name: str | None = None
    received_by: str | None = None
    lines: list[GrnLineExtraction] = []


_RULES = """
Transcribe values exactly as printed; do not compute, correct or infer them.
A quantity or total you calculated hides the discrepancy the three-way match
exists to find. Extract ALL lines. Use null for anything the page does not
state or that you cannot read -- a guessed value is worse than a blank one,
because a person confirms this document field by field.

Identifiers (document numbers, item codes) character by character as printed:
keep the letter O and the digit 0 apart, and do not drop separators.

Dates as YYYY-MM-DD."""

PO_PROMPT = (
    """Extract this PURCHASE ORDER: the buyer's order to a supplier.

Return ONLY valid JSON:
{
  "po_number": "string or null (the purchase order number, not a quotation or requisition reference)",
  "po_date": "YYYY-MM-DD or null",
  "valid_from": "YYYY-MM-DD or null",
  "valid_to": "YYYY-MM-DD or null",
  "vendor_name": "string or null (the supplier the order is placed with)",
  "vendor_tax_id": "string or null (the supplier's tax registration)",
  "customer_name": "string or null (the buyer placing the order)",
  "currency": "string or null (as printed)",
  "total_amount": decimal or null (the order total as printed),
  "amount_basis": "inc_tax" or "ex_tax" or null (whether that total includes tax, only when the page says so),
  "lines": [
    {
      "line_number": integer or null (the line's own number as printed),
      "item_code": "string or null",
      "description": "string",
      "quantity": decimal or null (the quantity ordered),
      "unit": "string or null (nos, kg, box)",
      "unit_price": decimal or null,
      "hsn_sac": "string or null (HSN or SAC classification code)"
    }
  ]
}
"""
    + _RULES
)

GRN_PROMPT = (
    """Extract this GOODS RECEIPT NOTE: the buyer's record of goods delivered.

Return ONLY valid JSON:
{
  "grn_number": "string or null (the receipt's own number)",
  "received_on": "YYYY-MM-DD or null (the date the goods were received)",
  "po_number": "string or null (the purchase order the goods were received against)",
  "vendor_name": "string or null (the supplier who delivered)",
  "received_by": "string or null",
  "lines": [
    {
      "line_number": integer or null (the line's own number as printed),
      "item_code": "string or null",
      "description": "string",
      "quantity_received": decimal or null,
      "unit": "string or null",
      "remarks": "string or null"
    }
  ]
}

quantity_received is the accepted/received quantity, not the ordered or rejected
quantity. Where the note shows several quantity columns (ordered,
received, rejected, accepted), report the quantity accepted into stock; when
there is no accepted column, the received quantity.
"""
    + _RULES
)

_KINDS = {
    PURCHASE_ORDER: (PurchaseOrderExtraction, PO_PROMPT, "quantity"),
    GOODS_RECEIPT: (GoodsReceiptExtraction, GRN_PROMPT, "quantity_received"),
}
_IDENTIFIERS = ("po_number", "grn_number")
# Classifications rather than transcriptions: the page prints "Rs." or nothing,
# so a search for "INR" or "inc_tax" proves nothing either way.
_NOT_GROUNDED = {"currency", "amount_basis"}
_OCR_MISREAD = {"po_number", "grn_number", "vendor_tax_id"}


def _read_pages(pdf_path) -> tuple[list[str], list[str], bool]:
    """(text layer, pages to ground against, has a text layer)."""
    pages = confidence.extract_page_text(pdf_path)
    if confidence.has_readable_text(pages):
        return pages, pages, True
    scanned = confidence.ocr_page_text(pdf_path)
    return (
        pages,
        scanned if confidence.has_readable_text(scanned) else [],
        False,
    )


def _repair(record: dict, key: str, where: str, pages, repairs) -> None:
    before = record.get(key)
    folded = fold_homoglyphs(before)
    if folded != before:
        repairs.append(
            f"{where}: look-alike characters replaced ({before!r} -> {folded!r})"
        )
    # OCR confuses O and 0 exactly as the extractor does, so only a real text
    # layer may correct an identifier; OCR pages are for grounding only.
    printed = read_as_printed(folded, pages)
    if printed != folded:
        repairs.append(
            f"{where}: read as the page prints it ({folded!r} -> {printed!r})"
        )
    record[key] = printed


def _evidence(value, pages, by_ocr: bool, misread: bool) -> dict:
    grounded, page = confidence.locate(value, pages, misread=by_ocr and misread)
    return {
        "value": value,
        "grounded": grounded,
        "page": page,
        "confidence": confidence.score(grounded, None),
    }


def extract(
    pdf_path: Path | str, kind: str, *, call: Callable | None = None
) -> dict:
    """The document's fields and lines, cleaned and checked against the page.

    `call` is `call_gemini_with_pdf`'s signature; its exceptions propagate so
    the worker records the failure rather than storing an empty document.
    """
    if kind not in _KINDS:
        raise ValueError(f"not a supporting document kind: {kind!r}")
    schema, prompt, qty_key = _KINDS[kind]
    if call is None:
        # ponytail: lazy, the agent module pulls in the whole ADK stack
        from .shared_libraries.acting import general_invoice_agent as agent  # noqa: PLC0415, I001

        call = agent.call_gemini_with_pdf
    result, _ = call(pdf_path, prompt, None, schema)  # None: the Pro model

    data = result.model_dump()
    lines = data.pop("lines") or []
    fields = data
    text_layer, pages, has_text_layer = _read_pages(pdf_path)
    by_ocr = bool(pages) and not has_text_layer

    repairs: list[str] = []
    for key in _IDENTIFIERS:
        if key in fields:
            _repair(fields, key, key, text_layer, repairs)
    for i, line in enumerate(lines, start=1):
        if line.get("line_number") is None:
            line["line_number"] = i
        _repair(line, "item_code", f"line {i} item_code", text_layer, repairs)

    evidence: dict = {}
    for key, value in fields.items():
        if key in _NOT_GROUNDED or value is None or str(value).strip() == "":
            continue
        evidence[key] = _evidence(value, pages, by_ocr, key in _OCR_MISREAD)
    for i, line in enumerate(lines):
        for key in (qty_key, "description"):
            value = line.get(key)
            if value is not None and str(value).strip():
                evidence[f"lines[{i}].{key}"] = _evidence(
                    value, pages, by_ocr, False
                )

    floor = min(
        (e["confidence"] for e in evidence.values()), default=confidence.LOW
    )
    evidence["_summary"] = {
        "min_confidence": floor,
        "fields_needing_review": sorted(
            k for k, e in evidence.items() if e["confidence"] < confidence.HIGH
        ),
        "has_text_layer": has_text_layer,
    }
    return {
        "kind": kind,
        "fields": fields,
        "lines": lines,
        "_evidence": evidence,
        "_repairs": repairs,
    }


def pair_grn_lines(lines: list[dict], po_lines: list[dict]) -> list[int | None]:
    """The PO line each GRN line received against, or None when unpaired.

    By item code, then description. Never by the GRN's own line number: a
    partial receipt renumbers its lines, so GRN #2 can be PO line 3.
    """
    probes = [
        {"description": ln.get("description"), "item_code": ln.get("item_code")}
        for ln in lines
    ]
    return [n for n, _ in three_way.pair(probes, po_lines)]


def to_import_payload(kind: str, fields: dict, lines: list[dict]) -> dict:
    """The confirmed document as one record of a PO or GRN import.

    A GRN line's `line_number` on import names the PO line, so it comes from
    the pairing (`po_line_number`), never the GRN's own printed numbering; an
    unpaired line with no item code fails validation and must be fixed.
    """
    if kind == PURCHASE_ORDER:
        return {
            "po_number": fields.get("po_number"),
            "vendor_name": fields.get("vendor_name"),
            "total_amount": fields.get("total_amount"),
            "currency": fields.get("currency"),
            "status": "open",
            "valid_from": fields.get("valid_from"),
            "valid_to": fields.get("valid_to"),
            "amount_basis": fields.get("amount_basis"),
            "lines": [
                {
                    "line_number": ln.get("line_number") or i,
                    "item_code": ln.get("item_code"),
                    "description": ln.get("description") or "",
                    "qty_ordered": ln.get("quantity"),
                    "unit_price": ln.get("unit_price"),
                    "uom": ln.get("unit"),
                    "receipt_required": None,
                }
                for i, ln in enumerate(lines, start=1)
            ],
        }
    if kind == GOODS_RECEIPT:
        return {
            "grn_number": fields.get("grn_number"),
            "po_number": fields.get("po_number"),
            "received_on": fields.get("received_on"),
            "reason": fields.get("reason"),
            "lines": [
                {
                    "line_number": ln.get("po_line_number"),
                    "item_code": ln.get("item_code"),
                    "qty_received": ln.get("quantity_received"),
                }
                for ln in lines
            ],
        }
    raise ValueError(f"not a supporting document kind: {kind!r}")
