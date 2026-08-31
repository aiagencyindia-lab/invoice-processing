"""Two-way match: invoice against purchase order.

Step 2.2 was a stub that reported `passed: True` while checking nothing. The
stated reason -- no PO number in the extraction schema -- stopped being true
when the schema was extended, but the deeper gap remained: there was nothing to
match *against*.

This is a **header-level two-way match**: does the PO exist, is it this vendor's,
is the money right, and is there room left on it. Three-way matching against a
goods receipt is deliberately out of scope -- there is no receipt data in the
system, and a "3-way match" that silently skips the receipt leg is worse than an
honest 2-way one.

The checks are ordered by what they catch, not by cost:

* **Vendor mismatch** is the one that matters most. An invoice quoting someone
  else's PO number is either a clerical error or the cheapest invoice fraud
  there is, and the amount can look perfectly reasonable while it happens.
* **Cumulative over-billing** is the one a per-invoice check misses. Each of
  five invoices can sit under the PO total and still take the sum well past it,
  so the PO's consumed amount has to be part of the comparison.
* **Tolerance is asymmetric.** Billing *under* a PO is normal -- partial
  delivery, staged work -- so only over-billing beyond tolerance fails. Treating
  under-billing as a mismatch would flag most legitimate partial invoices.

Every failure names what was compared and what was found, because a rejection an
AP clerk cannot act on just becomes an email to the vendor asking what happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# Outcomes, most severe first.
NO_PO_ON_INVOICE = "no_po_on_invoice"
PO_NOT_FOUND = "po_not_found"
VENDOR_MISMATCH = "vendor_mismatch"
CURRENCY_MISMATCH = "currency_mismatch"
PO_CLOSED = "po_closed"
OVER_BILLED = "over_billed"
EXCEEDS_REMAINING = "exceeds_remaining"
MATCHED = "matched"

BLOCKING = {
    NO_PO_ON_INVOICE,
    PO_NOT_FOUND,
    VENDOR_MISMATCH,
    CURRENCY_MISMATCH,
    PO_CLOSED,
    OVER_BILLED,
    EXCEEDS_REMAINING,
}

# 2% or $50, whichever is larger. A pure percentage is unhelpful on small POs,
# where rounding and freight routinely exceed it; a pure absolute is unhelpful
# on large ones, where it is noise.
DEFAULT_TOLERANCE_PCT = 0.02
DEFAULT_TOLERANCE_ABS = 50.0

OPEN_STATUSES = {"open", "approved", "partially_invoiced", "partially_received"}


@dataclass(frozen=True)
class PurchaseOrder:
    """A purchase order as the ERP holds it."""

    po_number: str
    vendor_name: str
    total_amount: float
    currency: str = "AUD"
    status: str = "open"
    # What earlier invoices have already consumed. Without it, cumulative
    # over-billing across several invoices is invisible.
    amount_invoiced: float = 0.0

    @property
    def remaining(self) -> float:
        return self.total_amount - self.amount_invoiced

    @classmethod
    def from_dict(cls, data: dict) -> PurchaseOrder:
        return cls(
            po_number=normalize_po_number(data.get("po_number")),
            vendor_name=str(data.get("vendor_name") or ""),
            total_amount=_as_float(data.get("total_amount")) or 0.0,
            currency=str(data.get("currency") or "AUD").upper(),
            status=str(data.get("status") or "open").lower(),
            amount_invoiced=_as_float(data.get("amount_invoiced")) or 0.0,
        )


@dataclass
class MatchResult:
    outcome: str
    po_number: str = ""
    reason: str = ""
    invoice_amount: float | None = None
    po_amount: float | None = None
    variance: float | None = None
    tolerance: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def matched(self) -> bool:
        return self.outcome == MATCHED

    @property
    def blocking(self) -> bool:
        return self.outcome in BLOCKING


@runtime_checkable
class PurchaseOrders(Protocol):
    """Where purchase orders come from. An ERP query in production."""

    def find(self, po_number: str) -> dict | None: ...


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def normalize_po_number(value: Any) -> str:
    """Strip case, spaces and punctuation.

    `PO-4471`, `po 4471` and `PO4471` are the same purchase order to everyone
    except a string comparison, and vendors type all three.
    """
    if value is None:
        return ""
    return "".join(ch for ch in str(value).upper() if ch.isalnum())


def normalize_vendor(value: Any) -> str:
    """Drop the legal suffixes that differ between the PO and the invoice."""
    if value is None:
        return ""
    text = str(value).upper()
    for suffix in (
        "PTY LTD",
        "PTY. LTD.",
        "PROPRIETARY LIMITED",
        "LIMITED",
        "LTD",
        "INCORPORATED",
        "INC",
        "LLC",
        "PLC",
        "GMBH",
        "CORPORATION",
        "CORP",
        "CO",
    ):
        text = text.replace(suffix, " ")
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text).split())


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def tolerance_for(
    amount: float,
    pct: float = DEFAULT_TOLERANCE_PCT,
    absolute: float = DEFAULT_TOLERANCE_ABS,
) -> float:
    return max(abs(amount) * pct, absolute)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def match_invoice_to_po(
    invoice: dict,
    orders: PurchaseOrders,
    *,
    required: bool = True,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
    tolerance_abs: float = DEFAULT_TOLERANCE_ABS,
) -> MatchResult:
    """Match one invoice against its purchase order.

    `required` reflects whether this tenant demands a PO on every invoice. When
    it does not, an invoice without one is simply not a PO invoice and passes
    -- a missing PO is only a failure where a PO was expected.
    """
    po_number = normalize_po_number(invoice.get("po_number"))
    # Normalised for lookup, but every message quotes what the vendor actually
    # wrote: an AP clerk matching "PO4471" against paperwork that says "PO-4471"
    # has been given a puzzle instead of an answer.
    shown = str(invoice.get("po_number") or "").strip() or po_number
    if not po_number:
        if required:
            return MatchResult(
                outcome=NO_PO_ON_INVOICE,
                reason=(
                    "No purchase order number on the invoice, and this account "
                    "requires one."
                ),
            )
        return MatchResult(
            outcome=MATCHED,
            reason="No purchase order required for this invoice.",
        )

    raw = orders.find(po_number)
    if raw is None:
        return MatchResult(
            outcome=PO_NOT_FOUND,
            po_number=po_number,
            reason=f"Purchase order {shown} was not found.",
        )

    po = raw if isinstance(raw, PurchaseOrder) else PurchaseOrder.from_dict(raw)
    invoice_vendor = normalize_vendor(invoice.get("vendor_name"))
    po_vendor = normalize_vendor(po.vendor_name)

    # Vendor first: an invoice quoting another supplier's PO is the failure with
    # the worst consequences, and every later check would pass while it happens.
    if invoice_vendor and po_vendor and invoice_vendor != po_vendor:
        return MatchResult(
            outcome=VENDOR_MISMATCH,
            po_number=po_number,
            reason=(
                f"Purchase order {shown} belongs to {po.vendor_name!r}, "
                f"but the invoice is from {invoice.get('vendor_name')!r}."
            ),
            details={
                "po_vendor": po.vendor_name,
                "invoice_vendor": invoice.get("vendor_name"),
            },
        )

    invoice_currency = str(invoice.get("currency") or "").upper()
    if invoice_currency and invoice_currency != po.currency:
        return MatchResult(
            outcome=CURRENCY_MISMATCH,
            po_number=po_number,
            reason=(
                f"Purchase order {shown} is in {po.currency}, "
                f"but the invoice is in {invoice_currency}."
            ),
        )

    if po.status not in OPEN_STATUSES:
        return MatchResult(
            outcome=PO_CLOSED,
            po_number=po_number,
            reason=f"Purchase order {shown} is {po.status}, not open for invoicing.",
        )

    amount = _as_float(invoice.get("invoice_total_inc_tax"))
    if amount is None:
        return MatchResult(
            outcome=MATCHED,
            po_number=po_number,
            reason=(
                f"Matched purchase order {shown}; invoice total was not "
                "readable, so no amount comparison was made."
            ),
        )

    tolerance = tolerance_for(po.total_amount, tolerance_pct, tolerance_abs)

    # Cumulative first: five invoices can each sit under the PO total and still
    # take the sum past it, which a per-invoice check never sees.
    #
    # Only when something has actually been invoiced before, though. With
    # nothing consumed the two checks describe the same event, and "exceeds the
    # remaining balance ... after 0.00 already invoiced" is a confusing way to
    # say the invoice is simply larger than the purchase order.
    consumed = po.amount_invoiced + amount
    if po.amount_invoiced > 0 and consumed > po.total_amount + tolerance:
        return MatchResult(
            outcome=EXCEEDS_REMAINING,
            po_number=po_number,
            reason=(
                f"Invoice of {amount:,.2f} exceeds the remaining balance on "
                f"{shown}: {po.remaining:,.2f} of {po.total_amount:,.2f} "
                f"is left after {po.amount_invoiced:,.2f} already invoiced."
            ),
            invoice_amount=amount,
            po_amount=po.total_amount,
            variance=round(consumed - po.total_amount, 2),
            tolerance=round(tolerance, 2),
        )

    if amount > po.total_amount + tolerance:
        return MatchResult(
            outcome=OVER_BILLED,
            po_number=po_number,
            reason=(
                f"Invoice of {amount:,.2f} exceeds purchase order {shown} "
                f"({po.total_amount:,.2f}) by {amount - po.total_amount:,.2f}, "
                f"outside the tolerance of {tolerance:,.2f}."
            ),
            invoice_amount=amount,
            po_amount=po.total_amount,
            variance=round(amount - po.total_amount, 2),
            tolerance=round(tolerance, 2),
        )

    # Under the PO total is normal: partial delivery, staged work, retentions.
    return MatchResult(
        outcome=MATCHED,
        po_number=po_number,
        reason=(
            f"Matched purchase order {shown}: {amount:,.2f} against "
            f"{po.total_amount:,.2f} ({po.remaining:,.2f} remaining)."
        ),
        invoice_amount=amount,
        po_amount=po.total_amount,
        variance=round(amount - po.total_amount, 2),
        tolerance=round(tolerance, 2),
    )


def to_validation(result: MatchResult) -> dict:
    """Render the match as a Phase 2 Step 2.2 validation entry."""
    entry = {
        "step": "2.2",
        "rule": "Valid purchase order",
        "passed": not result.blocking,
        "implemented": True,
        "outcome": result.outcome,
        "evidence": result.reason,
    }
    if result.po_number:
        entry["po_number"] = result.po_number
    if result.variance is not None:
        entry["variance"] = result.variance
        entry["tolerance"] = result.tolerance
    if result.blocking:
        entry["rejection_template"] = _REJECTION_TEMPLATES[result.outcome]
    if result.details:
        entry["details"] = result.details
    return entry


# What the vendor is told. Specific enough to act on: "PO mismatch" sends them
# back to ask which part, which is a second email nobody needed.
_REJECTION_TEMPLATES = {
    NO_PO_ON_INVOICE: "Purchase order number required",
    PO_NOT_FOUND: "Purchase order not recognised",
    VENDOR_MISMATCH: "Purchase order belongs to a different supplier",
    CURRENCY_MISMATCH: "Purchase order currency mismatch",
    PO_CLOSED: "Purchase order is closed",
    OVER_BILLED: "Invoice exceeds purchase order value",
    EXCEEDS_REMAINING: "Invoice exceeds remaining purchase order balance",
}
