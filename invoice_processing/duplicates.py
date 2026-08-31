"""Duplicate invoice detection.

Step 3.1 previously reported `"passed": True, "evidence": "Duplicate check not
implemented"` -- a control that announced success without running. Paying the
same invoice twice is one of the most common and most expensive AP failures, and
it is the single cheapest thing to catch, because it needs no model at all.

Three tiers, weakest evidence last:

* **Exact** -- same vendor, same invoice number. Near-certain.
* **Normalised** -- same vendor, same number ignoring case, punctuation and
  leading zeros. "INV-0042", "inv 42" and "INV42" are one invoice re-sent.
* **Fuzzy** -- same vendor, amount within tolerance, date within a window, but a
  *different* number. This is the resubmission-under-a-new-number case, and it
  is a suspicion rather than a fact, so it is reported at lower confidence and
  never blocks on its own without review.

The matching is a pure function over a list of previously-seen invoices, so the
store is swappable: a JSON index locally today, the `invoice` table in Phase 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

# A resubmission usually keeps the amount identical; the tolerance covers
# rounding and currency conversion drift, not genuinely different invoices.
DEFAULT_AMOUNT_TOLERANCE_PCT = 0.5
DEFAULT_DATE_WINDOW_DAYS = 7

EXACT = "exact"
NORMALIZED = "normalized"
FUZZY = "fuzzy"

# Fuzzy matches are a prompt to look, not a verdict.
CONFIDENCE = {EXACT: 1.0, NORMALIZED: 0.95, FUZZY: 0.6}


@dataclass(frozen=True)
class InvoiceKey:
    """The minimum needed to compare two invoices."""

    case_id: str
    vendor_name: str = ""
    vendor_tax_id: str = ""
    invoice_number: str = ""
    invoice_date: str = ""
    total: float | None = None
    file_sha256: str = ""

    @classmethod
    def from_invoice(cls, case_id: str, invoice: dict, file_sha256: str = ""):
        return cls(
            case_id=case_id,
            vendor_name=str(invoice.get("vendor_name") or ""),
            vendor_tax_id=str(invoice.get("vendor_tax_id") or ""),
            invoice_number=str(invoice.get("invoice_number") or ""),
            invoice_date=str(invoice.get("invoice_date") or ""),
            total=_as_float(invoice.get("invoice_total_inc_tax")),
            file_sha256=file_sha256,
        )


@dataclass
class DuplicateMatch:
    """A prior invoice this one may repeat."""

    case_id: str
    match_type: str
    confidence: float
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


class SeenInvoices(Protocol):
    """Source of previously-processed invoices. Postgres replaces this."""

    def all_keys(self, exclude_case_id: str = "") -> list[InvoiceKey]: ...


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_number(value: Any) -> str:
    """Reduce an invoice number to its comparable core.

    Strips case, punctuation and leading zeros, so "INV-0042", "inv 42" and
    "INV42" collapse together. Keeps the alphabetic prefix, because "INV-42" and
    "CN-42" are a different invoice and a credit note.
    """
    raw = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    if not raw:
        return ""
    match = re.fullmatch(r"([A-Z]*)0*(\d+)([A-Z]*)", raw)
    if match:
        prefix, digits, suffix = match.groups()
        return f"{prefix}{int(digits)}{suffix}"
    return raw


def normalize_vendor(value: Any) -> str:
    """Vendor name without legal suffixes or punctuation."""
    raw = re.sub(r"[^A-Za-z0-9 ]", " ", str(value or "")).upper()
    raw = re.sub(r"\s+", " ", raw).strip()
    for suffix in (
        " PTY LTD",
        " PTY LIMITED",
        " LIMITED",
        " LTD",
        " INC",
        " LLC",
        " PL",
    ):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)].strip()
    return raw


def same_vendor(left: InvoiceKey, right: InvoiceKey) -> bool:
    """Tax ID is authoritative when both have one; otherwise compare names.

    Two vendors can share a trading name, but a tax ID identifies an entity --
    so disagreeing tax IDs mean different vendors even if the names match.
    """
    left_id = re.sub(r"[^A-Za-z0-9]", "", left.vendor_tax_id or "")
    right_id = re.sub(r"[^A-Za-z0-9]", "", right.vendor_tax_id or "")
    if left_id and right_id:
        return left_id == right_id
    return bool(normalize_vendor(left.vendor_name)) and normalize_vendor(
        left.vendor_name
    ) == normalize_vendor(right.vendor_name)


def _as_float(value: Any) -> float | None:
    try:
        return float(re.sub(r"[^\d.\-]", "", str(value)))
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> date | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def find_duplicates(
    candidate: InvoiceKey,
    seen: list[InvoiceKey],
    amount_tolerance_pct: float = DEFAULT_AMOUNT_TOLERANCE_PCT,
    date_window_days: int = DEFAULT_DATE_WINDOW_DAYS,
) -> list[DuplicateMatch]:
    """Return prior invoices this one may repeat, strongest evidence first."""
    matches: list[DuplicateMatch] = []

    for prior in seen:
        if prior.case_id == candidate.case_id:
            continue

        # Same file, byte for byte: the strongest possible signal, and it does
        # not depend on extraction being correct.
        if (
            candidate.file_sha256
            and prior.file_sha256
            and candidate.file_sha256 == prior.file_sha256
        ):
            matches.append(
                DuplicateMatch(
                    prior.case_id,
                    EXACT,
                    CONFIDENCE[EXACT],
                    f"Identical file already processed as {prior.case_id}",
                    {"sha256": candidate.file_sha256},
                )
            )
            continue

        if not same_vendor(candidate, prior):
            continue

        if (
            candidate.invoice_number
            and candidate.invoice_number == prior.invoice_number
        ):
            matches.append(
                DuplicateMatch(
                    prior.case_id,
                    EXACT,
                    CONFIDENCE[EXACT],
                    f"Same vendor and invoice number as {prior.case_id} "
                    f"({candidate.invoice_number})",
                    {"invoice_number": candidate.invoice_number},
                )
            )
            continue

        normalized = normalize_number(candidate.invoice_number)
        if normalized and normalized == normalize_number(prior.invoice_number):
            matches.append(
                DuplicateMatch(
                    prior.case_id,
                    NORMALIZED,
                    CONFIDENCE[NORMALIZED],
                    f"Invoice number matches {prior.case_id} after normalization "
                    f"({candidate.invoice_number!r} vs {prior.invoice_number!r})",
                    {"normalized": normalized},
                )
            )
            continue

        fuzzy = _fuzzy_match(
            candidate, prior, amount_tolerance_pct, date_window_days
        )
        if fuzzy:
            matches.append(fuzzy)

    matches.sort(key=lambda m: m.confidence, reverse=True)
    return matches


def _fuzzy_match(
    candidate: InvoiceKey,
    prior: InvoiceKey,
    amount_tolerance_pct: float,
    date_window_days: int,
) -> DuplicateMatch | None:
    """Same vendor and amount, close in time, different number."""
    if candidate.total is None or prior.total is None or prior.total == 0:
        return None

    delta_pct = abs(candidate.total - prior.total) / abs(prior.total) * 100
    if delta_pct > amount_tolerance_pct:
        return None

    candidate_date, prior_date = (
        _as_date(candidate.invoice_date),
        _as_date(prior.invoice_date),
    )
    if candidate_date is None or prior_date is None:
        return None

    day_gap = abs((candidate_date - prior_date).days)
    if day_gap > date_window_days:
        return None

    return DuplicateMatch(
        prior.case_id,
        FUZZY,
        CONFIDENCE[FUZZY],
        f"Same vendor and amount as {prior.case_id} "
        f"({candidate.total} vs {prior.total}, {day_gap} day(s) apart) under a "
        f"different invoice number -- possible resubmission",
        {
            "amount_delta_pct": round(delta_pct, 3),
            "day_gap": day_gap,
            "candidate_number": candidate.invoice_number,
            "prior_number": prior.invoice_number,
        },
    )


def to_validation(matches: list[DuplicateMatch]) -> dict:
    """Render matches as a Phase 3 Step 3.1 validation entry.

    A fuzzy-only match does not fail the step: it is a suspicion, and rejecting
    on it would block legitimate repeat billing (retainers, subscriptions).
    """
    if not matches:
        return {
            "step": "3.1",
            "rule": "Not a duplicate invoice",
            "passed": True,
            "implemented": True,
            "evidence": "No prior invoice matched",
        }

    strongest = matches[0]
    blocking = strongest.match_type in (EXACT, NORMALIZED)
    return {
        "step": "3.1",
        "rule": "Not a duplicate invoice",
        "passed": not blocking,
        "implemented": True,
        "match_type": strongest.match_type,
        "confidence": strongest.confidence,
        "matched_cases": [m.case_id for m in matches],
        "evidence": strongest.reason
        + ("" if blocking else " (review recommended, not blocking)"),
        "rejection_template": "Invoice already processed" if blocking else None,
    }
