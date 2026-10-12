"""Bank numbers as the API shows them to someone who may not pay anyone.

The last four digits are enough to recognise an account and not enough to
pay into it. One helper, used by every surface that serialises a bank
number, so the vendor queue, the invoice detail, the sheet and the exports
cannot drift apart.
"""

from __future__ import annotations

BANK_FIELDS = ("bank_account_number", "bank_iban")
MASK_PREFIX = "…"


def mask_bank(value) -> str | None:
    if value is None or value == "":
        return None
    digits = str(value).replace(" ", "")
    # Always prefixed: a short value returned verbatim both leaks it and
    # makes "is this masked?" unanswerable downstream.
    return f"{MASK_PREFIX}{digits[-4:]}"


def mask_row(row: dict) -> dict:
    masked = dict(row)
    for field in BANK_FIELDS:
        if field in masked:
            masked[field] = mask_bank(masked[field])
    return masked
