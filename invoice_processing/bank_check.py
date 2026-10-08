"""Step 3.4: the bank details printed on an invoice against the ones verified
for the vendor.

Payment redirection works by changing an account number. The extractor has
read bank details since Phase 1 and the grounder has checked they appear on
the page; this is the first thing that asks whether they are the *vendor's*.

Every signal is a hold for review, never a rejection: a rejection letter
tells the sender the control exists, and a hold lets a person phone the
vendor on a number they already had.

Comparison is deliberately literal. No O/0 folding: tolerating it on
identifiers once confirmed 24 wrong values, and a changed bank code is the
very thing this check exists to catch. OCR noise therefore lands in review,
which is the right place for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import po_matching
from .vendor_master import Vendor

BANK_MISMATCH = "bank_mismatch"
BANK_UNVERIFIED = "bank_unverified"
PAYEE_NAME_DIFFERS = "payee_name_differs"
RECENT_BANK_CHANGE = "recent_bank_change"
SENDER_DOMAIN_MISMATCH = "sender_domain_mismatch"

# Most severe first; the order evidence is listed in.
SEVERITY = (
    BANK_MISMATCH,
    BANK_UNVERIFIED,
    PAYEE_NAME_DIFFERS,
    RECENT_BANK_CHANGE,
    SENDER_DOMAIN_MISMATCH,
)


@dataclass
class BankCheck:
    signals: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    compared: bool = False

    @property
    def passed(self) -> bool:
        return not self.signals


def norm_account(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def norm_code(value) -> str:
    return re.sub(r"\s", "", str(value or "")).upper()


def domain_of(address) -> str:
    text = str(address or "")
    if "<" in text and ">" in text:
        text = text[text.rindex("<") + 1 : text.rindex(">")]
    return text.rsplit("@", 1)[1].strip().lower() if "@" in text else ""


def _tail(value: str) -> str:
    return f"…{value[-4:]}" if len(value) > 4 else value


def _recent(stamp: str | None, reference: date, days: int) -> bool:
    if not stamp:
        return False
    try:
        when = datetime.fromisoformat(stamp).date()
    except ValueError:
        return False
    return reference - when <= timedelta(days=days)


def compare(
    invoice: dict,
    vendor: Vendor | None,
    *,
    sender: str | None,
    reference_date: date,
    recent_days: int,
) -> BankCheck:
    result = BankCheck()
    if vendor is None:
        return result  # unknown vendor is step 3.2's finding

    account = norm_account(invoice.get("bank_account_number"))
    iban = norm_code(invoice.get("bank_iban"))
    code = norm_code(invoice.get("bank_code"))
    payee = str(invoice.get("bank_account_name") or "")
    printed = bool(account or iban)
    if not printed:
        return result

    result.compared = True
    found: dict[str, str] = {}

    if vendor.has_bank:
        same_account = (
            account and account == norm_account(vendor.bank_account_number)
        ) or (iban and iban == norm_code(vendor.bank_iban))
        same_code = (
            not code
            or not vendor.bank_code
            or code == norm_code(vendor.bank_code)
        )
        if not (same_account and same_code):
            found[BANK_MISMATCH] = (
                f"Account {_tail(account or iban)} / {code or '(no code)'} on the invoice; "
                f"{vendor.name} verified as {_tail(norm_account(vendor.bank_account_number) or norm_code(vendor.bank_iban))} "
                f"/ {norm_code(vendor.bank_code) or '(no code)'}"
            )
    else:
        found[BANK_UNVERIFIED] = (
            f"No verified bank details for {vendor.name}; invoice shows "
            f"{_tail(account or iban)} / {code or '(no code)'}"
        )

    if payee and po_matching.normalize_vendor(
        payee
    ) != po_matching.normalize_vendor(vendor.name):
        found[PAYEE_NAME_DIFFERS] = (
            f"Payee '{payee}' is not the vendor '{vendor.name}'"
        )

    if vendor.pending_change:
        found[RECENT_BANK_CHANGE] = (
            f"A bank change for {vendor.name} is awaiting approval"
        )
    elif _recent(vendor.last_bank_change_at, reference_date, recent_days):
        found[RECENT_BANK_CHANGE] = (
            f"Bank details for {vendor.name} changed on {vendor.last_bank_change_at[:10]}; "
            f"within {recent_days} days of this invoice"
        )

    sender_domain = domain_of(sender)
    if (
        sender_domain
        and vendor.email_domain
        and sender_domain != vendor.email_domain
    ):
        found[SENDER_DOMAIN_MISMATCH] = (
            f"Sent from {sender_domain}; vendor domain is {vendor.email_domain}"
        )

    result.signals = [s for s in SEVERITY if s in found]
    result.evidence = [found[s] for s in result.signals] or [
        "Bank details match vendor record"
    ]
    return result
