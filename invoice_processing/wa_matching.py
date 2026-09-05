"""Was this work authorised, and is the invoice within what was authorised?

Check 1.4 asked whether a Work Authorization *form* was attached to the
submission -- `waf_count > 0`, counted by the classifier across the PDFs in the
case folder. Two things were wrong with that. The worker materialises only the
invoice, so the count is always zero and the check could never pass. And most
customers do not attach the form: the invoice quotes a reference, and the
authorisation itself lives in their own records.

So this is the same shape as `po_matching`: the customer loads what they have
authorised, and the invoice's reference is looked up against it. Check 1.4 asks
whether the authorisation exists and belongs here; check 4.3 asks whether the
invoice stays inside it -- hours, rate, project and period.

The form-based path is kept. A tenant who does attach work authorizations, and
has no master loaded, sees exactly the behaviour they see today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from . import master_files, po_matching

RECORD_KEY = "work_authorizations"

# 1.4 -- does the authorisation exist and belong here?
NOT_APPLICABLE = "not_applicable"
NO_REFERENCE = "no_reference"
NOT_FOUND = "not_found"
OTHER_VENDOR = "other_vendor"
EXPIRED = "expired"
MATCHED = "matched"
NOT_CONFIGURED = "not_configured"

# 4.3 -- does the invoice stay inside it?
HOURS_EXCEEDED = "hours_exceeded"
RATE_MISMATCH = "rate_mismatch"
PROJECT_MISMATCH = "project_mismatch"
OUTSIDE_PERIOD = "outside_period"
WITHIN_AUTHORIZATION = "within_authorization"

BLOCKING_1_4 = {NO_REFERENCE, NOT_FOUND, OTHER_VENDOR, EXPIRED}
BLOCKING_4_3 = {HOURS_EXCEEDED, RATE_MISMATCH, PROJECT_MISMATCH, OUTSIDE_PERIOD}

ACTIVE_STATUSES = {"active", "open", "approved"}

# Units that mean an hour of somebody's time. A line billed by the hour is what
# an authorisation caps; a line billed per licence or per delivery is not.
HOUR_UNITS = {"hour", "hours", "hr", "hrs", "man-hour", "manhour"}


@dataclass(frozen=True)
class WorkAuthorization:
    reference: str
    vendor_name: str = ""
    customer_name: str = ""
    project: str = ""
    approved_hours: float | None = None
    hourly_rate: float | None = None
    period_start: str = ""
    period_end: str = ""
    status: str = "active"

    @classmethod
    def from_dict(cls, data: dict) -> WorkAuthorization:
        return cls(
            reference=normalize_reference(
                data.get("reference") or data.get("work_authorization_id")
            ),
            vendor_name=str(data.get("vendor_name") or ""),
            customer_name=str(data.get("customer_name") or ""),
            project=str(data.get("project") or ""),
            approved_hours=_as_float(data.get("approved_hours")),
            hourly_rate=_as_float(data.get("hourly_rate")),
            period_start=str(data.get("period_start") or ""),
            period_end=str(data.get("period_end") or ""),
            status=str(data.get("status") or "active").lower(),
        )

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


@dataclass
class WaMatch:
    outcome: str
    reference: str = ""
    authorization: WorkAuthorization | None = None
    evidence: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome not in BLOCKING_1_4 | BLOCKING_4_3


def normalize_reference(value: Any) -> str:
    """`WA-A-2026-003`, `wa a 2026 003` and `WAA2026003` are one reference."""
    return "".join(ch for ch in str(value or "") if ch.isalnum()).upper()


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> date | None:
    from .shared_libraries.acting.general_invoice_agent import (  # noqa: PLC0415
        parse_date,
    )

    return parse_date(str(value)) if value else None


class WorkAuthorizations:
    """What this customer has authorised, indexed by reference."""

    def __init__(self, path=None, records: list[dict] | None = None):
        self.path = path
        self._records = records
        self._index: dict[str, WorkAuthorization] | None = None

    def _load(self) -> dict[str, WorkAuthorization]:
        if self._index is None:
            records = (
                self._records
                if self._records is not None
                else master_files.read_records(self.path, RECORD_KEY)
            )
            self._index = {}
            for record in records:
                authorization = WorkAuthorization.from_dict(record)
                if authorization.reference:
                    self._index[authorization.reference] = authorization
        return self._index

    def __len__(self) -> int:
        return len(self._load())

    def find(self, reference: Any) -> WorkAuthorization | None:
        return self._load().get(normalize_reference(reference))


def match_invoice_to_wa(
    invoice: dict,
    authorizations: WorkAuthorizations,
    *,
    vendors=None,
) -> WaMatch:
    """Step 1.4: does the quoted authorisation exist, and is it this vendor's?

    `vendors` is the tenant's supplier list, used only to answer whether this
    supplier bills for work at all. A supplier of goods has nothing to
    authorise, and demanding an authorisation from them would reject every
    delivery they ever invoice.
    """
    if not len(authorizations):
        return WaMatch(NOT_CONFIGURED, evidence="No work authorizations loaded")

    if vendors is not None and len(vendors):
        vendor = vendors.find(invoice.get("vendor_name"))
        if vendor is not None and vendor.kind == "goods":
            return WaMatch(
                NOT_APPLICABLE,
                evidence=f"{vendor.name} supplies goods; no work to authorise",
            )

    quoted = invoice.get("work_authorization_reference")
    reference = normalize_reference(quoted)
    if not reference:
        return WaMatch(
            NO_REFERENCE,
            evidence="No work authorization quoted on the invoice",
        )

    authorization = authorizations.find(reference)
    if authorization is None:
        return WaMatch(
            NOT_FOUND,
            reference=str(quoted),
            evidence=f"Work authorization {quoted} is not one of ours",
        )
    if po_matching.normalize_vendor(
        authorization.vendor_name
    ) != po_matching.normalize_vendor(invoice.get("vendor_name")):
        # The one that matters most: an authorisation quoted by a supplier it
        # was never issued to. The amount can look entirely reasonable.
        return WaMatch(
            OTHER_VENDOR,
            reference=str(quoted),
            authorization=authorization,
            evidence=(
                f"Work authorization {quoted} was issued to "
                f"{authorization.vendor_name}, not {invoice.get('vendor_name')}"
            ),
        )
    if not authorization.active:
        return WaMatch(
            EXPIRED,
            reference=str(quoted),
            authorization=authorization,
            evidence=f"Work authorization {quoted} is {authorization.status}",
        )
    return WaMatch(
        MATCHED,
        reference=str(quoted),
        authorization=authorization,
        evidence=f"Work authorization {quoted} is active for this supplier",
    )


def invoiced_hours(
    invoice: dict, labour_keywords: tuple[str, ...] = ()
) -> float:
    """Hours billed: lines priced by the hour, or described as labour."""
    total = 0.0
    for line in invoice.get("line_items") or []:
        unit = str(line.get("unit") or "").strip().lower()
        description = str(line.get("description") or "").lower()
        by_unit = unit in HOUR_UNITS
        by_description = any(word in description for word in labour_keywords)
        if by_unit or (not unit and by_description):
            total += _as_float(line.get("quantity")) or 0.0
    return total


def hours_check(
    invoice: dict,
    authorization: WorkAuthorization,
    *,
    hours_tolerance: float = 0.5,
    rate_tolerance: float = 0.01,
    labour_keywords: tuple[str, ...] = (),
) -> WaMatch:
    """Step 4.3: is the invoice inside what was authorised?

    Four ways to be outside it, and each is checked separately so the evidence
    names the one that happened. Billing 200 hours against 40, and billing 40
    hours at twice the agreed rate, cost the same amount and are different
    conversations with the supplier.
    """
    details: dict[str, Any] = {"reference": authorization.reference}

    project = str(invoice.get("project") or "")
    if authorization.project and project:
        if po_matching.normalize_vendor(
            project
        ) != po_matching.normalize_vendor(authorization.project):
            return WaMatch(
                PROJECT_MISMATCH,
                authorization=authorization,
                evidence=(
                    f"Invoice bills '{project}'; the authorization covers "
                    f"'{authorization.project}'"
                ),
                details=details,
            )

    start = _as_date(invoice.get("service_period_start"))
    end = _as_date(invoice.get("service_period_end"))
    period_start = _as_date(authorization.period_start)
    period_end = _as_date(authorization.period_end)
    if period_start and period_end:
        outside = (start and start < period_start) or (end and end > period_end)
        if outside:
            return WaMatch(
                OUTSIDE_PERIOD,
                authorization=authorization,
                evidence=(
                    f"Service period {invoice.get('service_period_start')} to "
                    f"{invoice.get('service_period_end')} falls outside the "
                    f"authorization ({authorization.period_start} to "
                    f"{authorization.period_end})"
                ),
                details=details,
            )

    hours = invoiced_hours(invoice, labour_keywords)
    if authorization.approved_hours is not None and hours > 0:
        details["invoiced_hours"] = hours
        details["approved_hours"] = authorization.approved_hours
        if hours > authorization.approved_hours + hours_tolerance:
            return WaMatch(
                HOURS_EXCEEDED,
                authorization=authorization,
                evidence=(
                    f"{hours:g} hours invoiced against {authorization.approved_hours:g} "
                    "authorized"
                ),
                details=details,
            )

    if authorization.hourly_rate is not None:
        for line in invoice.get("line_items") or []:
            unit = str(line.get("unit") or "").strip().lower()
            if unit not in HOUR_UNITS:
                continue
            rate = _as_float(line.get("unit_price"))
            if rate is None:
                continue
            if abs(rate - authorization.hourly_rate) > rate_tolerance:
                return WaMatch(
                    RATE_MISMATCH,
                    authorization=authorization,
                    evidence=(
                        f"Billed at {rate:,.2f} an hour; the authorization is "
                        f"{authorization.hourly_rate:,.2f}"
                    ),
                    details={**details, "billed_rate": rate},
                )

    return WaMatch(
        WITHIN_AUTHORIZATION,
        authorization=authorization,
        evidence=(
            f"{hours:g} hours within {authorization.approved_hours:g} authorized"
            if authorization.approved_hours is not None and hours
            else f"Within work authorization {authorization.reference}"
        ),
        details=details,
    )
