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
OTHER_CUSTOMER = "other_customer"
OUTSIDE_VALIDITY = "outside_validity"
QUANTITY_EXCEEDED = "quantity_exceeded"
PRICE_MISMATCH = "price_mismatch"
CURRENCY_MISMATCH = "currency_mismatch"
PO_CLOSED = "po_closed"
OVER_BILLED = "over_billed"
EXCEEDS_REMAINING = "exceeds_remaining"
MATCHED = "matched"

BLOCKING = {
    NO_PO_ON_INVOICE,
    PO_NOT_FOUND,
    VENDOR_MISMATCH,
    OTHER_CUSTOMER,
    OUTSIDE_VALIDITY,
    QUANTITY_EXCEEDED,
    PRICE_MISMATCH,
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

    # Everything below is optional, and each is checked only when the export
    # carries it. An ERP that exports a number, a supplier and a total gets
    # exactly the match it got before; one that exports more gets more checked.
    customer_name: str = ""
    valid_from: str = ""
    valid_to: str = ""
    approved_quantity: float | None = None
    approved_unit_price: float | None = None
    hsn_sac: str = ""
    # Whether `total_amount` includes tax. The invoice is compared on the same
    # basis: a PO stated net of tax against an invoice total including it makes
    # every correctly-taxed invoice look like over-billing.
    amount_basis: str = "inc_tax"

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
            customer_name=str(data.get("customer_name") or ""),
            valid_from=str(data.get("valid_from") or ""),
            valid_to=str(data.get("valid_to") or ""),
            approved_quantity=_as_float(data.get("approved_quantity")),
            approved_unit_price=_as_float(data.get("approved_unit_price")),
            hsn_sac=str(data.get("hsn_sac") or ""),
            amount_basis=str(data.get("amount_basis") or "inc_tax").lower(),
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


# Legal forms, as whole words. Each maps to what it collapses to, so that
# "Pvt Ltd" and "Private Limited" -- the same company written two ways on the
# purchase order and the invoice -- reach the same key.
_LEGAL_FORMS = {
    "PTY": "",
    "PROPRIETARY": "",
    "LIMITED": "",
    "LTD": "",
    "PRIVATE": "PVT",
    "PVT": "PVT",
    "INCORPORATED": "",
    "INC": "",
    "LLC": "",
    "LLP": "LLP",
    "PLC": "",
    "GMBH": "",
    "CORPORATION": "",
    "CORP": "",
    "COMPANY": "",
    "CO": "",
}


def normalize_vendor(value: Any) -> str:
    """Drop the legal suffixes that differ between the PO and the invoice.

    Whole words, and only where they trail the name. This used to be a
    substring replacement over the whole string, which turned "Coca Cola
    Company" into "CA LA MPANY" and "Consulting" into "NSULTING" -- harmless
    while both sides were mangled identically, and not harmless at all once a
    vendor master is indexed by this key, where two different suppliers
    collapsing to the same string means one supplier's invoice is checked
    against another's tax ID.

    "Pvt" and "Private" survive as one token rather than being dropped: an
    Indian private limited company and its LLP namesake are different legal
    entities, and this is the only thing distinguishing them.
    """
    if value is None:
        return ""
    words = "".join(
        ch if ch.isalnum() else " " for ch in str(value).upper()
    ).split()
    while words and words[-1] in _LEGAL_FORMS:
        replacement = _LEGAL_FORMS[words.pop()]
        if replacement:
            words.append(replacement)
            break
    return " ".join(words)


def _as_date(value: Any):
    """Dates on a purchase order, in whatever format the export wrote them."""
    from .shared_libraries.acting.general_invoice_agent import (  # noqa: PLC0415
        parse_date,
    )

    return parse_date(str(value)) if value else None


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
    vendors=None,
) -> MatchResult:
    """Match one invoice against its purchase order.

    `required` reflects whether this tenant demands a PO on every invoice. When
    it does not, an invoice without one is simply not a PO invoice and passes
    -- a missing PO is only a failure where a PO was expected.

    `vendors` is the tenant's supplier list, used only to answer whether this
    supplier delivers goods. What a purchase order governs depends on the
    answer: for goods it is quantity and price, and for services it is a budget
    the work authorization prices. See the quantity check below.
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

    # The customer, where the export names one. A purchase order raised by
    # another company in the group is not this company's commitment.
    invoice_customer = normalize_vendor(invoice.get("customer_name"))
    po_customer = normalize_vendor(po.customer_name)
    if invoice_customer and po_customer and invoice_customer != po_customer:
        return MatchResult(
            outcome=OTHER_CUSTOMER,
            po_number=po_number,
            reason=(
                f"Purchase order {shown} was raised by {po.customer_name!r}, "
                f"not {invoice.get('customer_name')!r}."
            ),
            details={
                "po_customer": po.customer_name,
                "invoice_customer": invoice.get("customer_name"),
            },
        )

    invoice_date = _as_date(invoice.get("invoice_date"))
    valid_from, valid_to = _as_date(po.valid_from), _as_date(po.valid_to)
    if invoice_date and (
        (valid_from and invoice_date < valid_from)
        or (valid_to and invoice_date > valid_to)
    ):
        return MatchResult(
            outcome=OUTSIDE_VALIDITY,
            po_number=po_number,
            reason=(
                f"Invoice dated {invoice.get('invoice_date')} falls outside "
                f"purchase order {shown} ({po.valid_from} to {po.valid_to})."
            ),
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

    # Quantity and unit price, where the export states them and where they are
    # this order's business.
    #
    # For goods they are: a total within tolerance can still be the wrong goods
    # at the wrong price, since twice the quantity at half the price reconciles
    # perfectly. For services they are not. A service supplier's rate is set by
    # the work authorization, which step 4.3 checks against the authorized rate
    # and the authorized hours; the purchase order is the budget the work is
    # drawn against, and its unit price is one line of an estimate rather than
    # an agreed price list. Checking it here rejected eight legitimate service
    # invoices in the benchmark corpus, every one of them for billing a rate
    # its work authorization allowed.
    #
    # Unknown suppliers keep the stricter reading: an invoice from someone not
    # on the supplier list is not the place to relax a control.
    supplies_goods = True
    if vendors is not None and len(vendors):
        vendor = vendors.find(invoice.get("vendor_name"))
        if vendor is not None and vendor.kind == "service":
            supplies_goods = False

    first_line = (invoice.get("line_items") or [{}])[0]
    quantity = _as_float(first_line.get("quantity"))
    unit_price = _as_float(first_line.get("unit_price"))
    # What was actually compared, so the evidence says so. It used to read
    # "matched on quantity and unit price" for every service invoice -- where
    # neither is compared -- and for an invoice with no lines at all.
    compared = [
        name
        for name, ours, theirs in (
            ("quantity", quantity, po.approved_quantity),
            ("unit price", unit_price, po.approved_unit_price),
        )
        if supplies_goods and ours is not None and theirs is not None
    ]
    if (
        supplies_goods
        and po.approved_quantity is not None
        and quantity is not None
        and quantity > po.approved_quantity
    ):
        return MatchResult(
            outcome=QUANTITY_EXCEEDED,
            po_number=po_number,
            reason=(
                f"Invoice bills {quantity:g} against {po.approved_quantity:g} "
                f"ordered on purchase order {shown}."
            ),
            details={
                "invoiced_quantity": quantity,
                "approved_quantity": po.approved_quantity,
            },
        )

    if (
        supplies_goods
        and po.approved_unit_price is not None
        and unit_price is not None
        and abs(unit_price - po.approved_unit_price) > 0.01
    ):
        return MatchResult(
            outcome=PRICE_MISMATCH,
            po_number=po_number,
            reason=(
                f"Invoice prices this at {unit_price:,.2f}; purchase order "
                f"{shown} approved {po.approved_unit_price:,.2f}."
            ),
            details={
                "invoiced_unit_price": unit_price,
                "approved_unit_price": po.approved_unit_price,
            },
        )

    # What the order actually commits to decides what the amount is compared
    # against.
    #
    # An order stating an approved quantity and unit price is a commitment for
    # *that item*. The invoice quoting it routinely covers a dozen other lines
    # the order says nothing about, so measuring the whole invoice against one
    # item's value rejects every legitimate multi-line invoice -- 19 of them in
    # the benchmark corpus, every one expected to pass. The quantity and price
    # checks above are the controls for an order of that shape, and they have
    # already run.
    #
    # Where the order states a value and no line detail, it is a commitment for
    # the order as a whole and the invoice total is exactly the right
    # comparison. That is the case this check was written for and it is
    # unchanged.
    if po.approved_quantity is not None and po.approved_unit_price is not None:
        if not supplies_goods:
            on = (
                "as an open order for this supplier. A service is priced by "
                "its work authorization (step 4.3), not by the order's unit "
                "price, so neither quantity nor price was compared"
            )
        elif compared:
            on = "on " + " and ".join(compared)
        else:
            on = (
                "by reference only: the invoice states no quantity or unit "
                "price to compare"
            )
        return MatchResult(
            outcome=MATCHED,
            po_number=po_number,
            reason=(
                f"Matched purchase order {shown} {on}. The order commits to "
                "one item, so the invoice total was not compared against it."
            ),
            details={
                "approved_quantity": po.approved_quantity,
                "approved_unit_price": po.approved_unit_price,
            },
        )

    # On the basis the purchase order states. A PO held net of tax compared
    # against a tax-inclusive invoice total makes every correctly-taxed invoice
    # look like over-billing -- 18% of it, every time.
    if po.amount_basis == "ex_tax":
        amount = _as_float(invoice.get("invoice_total_ex_tax"))
    else:
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
    OTHER_CUSTOMER: "Purchase order belongs to a different customer",
    OUTSIDE_VALIDITY: "Purchase order was not valid on the invoice date",
    QUANTITY_EXCEEDED: "Invoice exceeds the quantity ordered",
    PRICE_MISMATCH: "Invoice unit price differs from the purchase order",
    CURRENCY_MISMATCH: "Purchase order currency mismatch",
    PO_CLOSED: "Purchase order is closed",
    OVER_BILLED: "Invoice exceeds purchase order value",
    EXCEEDS_REMAINING: "Invoice exceeds remaining purchase order balance",
}
