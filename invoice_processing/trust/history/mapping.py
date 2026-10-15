"""Which column means what, per kind of history file.

Synonyms cover the common exports (Tally, Zoho, bank CSVs); anything left
over is one `decide()` question per header, and a human confirms the whole
mapping before any row is stored. The mapping is saved per tenant per source
name, so a Tally export is a saved mapping, not code (spec §4.1).
"""

from __future__ import annotations

import re

from ..decide import Question, decide

KINDS = (
    "vendor",
    "ap_invoice",
    "payment",
    "po",
    "grn",
    "bank_line",
    "vendor_statement_line",
)

FIELDS: dict[str, tuple[str, ...]] = {
    "vendor": (
        "external_id",
        "vendor_key",
        "gstin",
        "pan",
        "abn",
        "bank_account",
        "bank_code",
        "email_domain",
    ),
    "ap_invoice": (
        "external_id",
        "doc_number",
        "vendor_key",
        "amount",
        "currency",
        "record_date",
        "po_number",
        "tax_amount",
    ),
    "payment": (
        "external_id",
        "doc_number",
        "vendor_key",
        "amount",
        "currency",
        "record_date",
        "bank_reference",
    ),
    "po": (
        "external_id",
        "doc_number",
        "vendor_key",
        "amount",
        "currency",
        "record_date",
        "line_number",
        "quantity",
        "unit_price",
    ),
    "grn": (
        "external_id",
        "doc_number",
        "po_number",
        "record_date",
        "line_number",
        "quantity",
    ),
    "bank_line": (
        "external_id",
        "record_date",
        "amount",
        "currency",
        "counterparty",
        "bank_reference",
    ),
    "vendor_statement_line": (
        "external_id",
        "vendor_key",
        "doc_number",
        "amount",
        "currency",
        "record_date",
    ),
}

REQUIRED: dict[str, tuple[str, ...]] = {
    "vendor": ("vendor_key",),
    "ap_invoice": ("doc_number", "vendor_key", "amount"),
    "payment": ("amount", "record_date"),
    "po": ("doc_number", "vendor_key"),
    "grn": ("doc_number", "po_number"),
    "bank_line": ("amount", "record_date"),
    "vendor_statement_line": ("vendor_key", "doc_number", "amount"),
}

_SYNONYMS: dict[str, tuple[str, ...]] = {
    "doc_number": (
        "bill no",
        "bill number",
        "invoice no",
        "invoice number",
        "inv no",
        "voucher no",
        "document no",
        "po no",
        "po number",
        "grn no",
        "statement ref",
    ),
    "external_id": ("id", "record id", "guid", "master id"),
    "vendor_key": (
        "supplier",
        "vendor",
        "party",
        "party name",
        "vendor name",
        "supplier name",
        "ledger",
    ),
    "amount": (
        "amount",
        "total",
        "gross",
        "value",
        "net amount",
        "invoice amount",
        "debit",
        "credit",
    ),
    "currency": ("currency", "ccy", "cur"),
    "tax_amount": ("tax", "tax amount", "gst", "gst amount", "total tax"),
    "record_date": (
        "date",
        "invoice date",
        "bill date",
        "voucher date",
        "posting date",
        "payment date",
        "txn date",
        "value date",
    ),
    "po_number": ("po", "po ref", "purchase order", "order no", "order number"),
    "gstin": ("gstin", "gst no", "gst number"),
    "pan": ("pan",),
    "abn": ("abn",),
    "bank_account": ("account no", "account number", "bank account", "a/c no"),
    "bank_code": ("ifsc", "bsb", "swift", "bank code"),
    "email_domain": ("email", "email domain", "domain"),
    "bank_reference": ("utr", "reference", "ref no", "transaction id"),
    "counterparty": ("counterparty", "narration", "description", "beneficiary"),
    "line_number": ("line", "line no", "sr no", "s no"),
    "quantity": ("qty", "quantity"),
    "unit_price": ("rate", "unit price", "price"),
}


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", header.lower()).strip()


def _by_synonym(kind: str, header: str) -> str:
    h = _norm(header).replace("_", " ")
    # A header that *is* a field name wins before any synonym: "po_number"
    # must not land on doc_number just because "po number" is one of its
    # synonyms elsewhere.
    for field in FIELDS[kind]:
        if h == field.replace("_", " "):
            return field
    for field in FIELDS[kind]:
        if h in _SYNONYMS.get(field, ()):
            return field
    return ""


def propose(kind: str, headers: list[str], decider=None) -> dict[str, str]:
    if kind not in KINDS:
        raise ValueError(
            f"No such history kind: {kind}. Kinds: {', '.join(KINDS)}."
        )
    proposed = {h: _by_synonym(kind, h) for h in headers}
    taken = {f for f in proposed.values() if f}
    unknown = [h for h, f in proposed.items() if not f]
    if unknown:
        options = (*(f for f in FIELDS[kind] if f not in taken), "")
        questions = [
            Question(
                h,
                "choice",
                f"Which {kind} field is the column {h!r}? Answer '' if none.",
                fallback="",
                options=options,
            )
            for h in unknown
        ]
        for answer in decide(
            {"kind": kind, "headers": headers}, questions, decider
        ):
            proposed[answer.id] = answer.value
    return proposed


def validate(
    kind: str, mapping: dict[str, str], headers: list[str]
) -> list[str]:
    problems = []
    if kind not in KINDS:
        return [f"No such history kind: {kind}."]
    for header, field in mapping.items():
        if header not in headers:
            problems.append(f"mapped column {header!r} is not in the file")
        if field and field not in FIELDS[kind]:
            problems.append(f"{field!r} is not a {kind} field")
    used = [f for f in mapping.values() if f]
    for field in sorted(set(used)):
        if used.count(field) > 1:
            problems.append(f"{field} is mapped from more than one column")
    for field in REQUIRED[kind]:
        if field not in used:
            problems.append(f"required field {field} is not mapped")
    return problems
