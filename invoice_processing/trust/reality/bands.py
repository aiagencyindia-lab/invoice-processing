"""Σ evidence vs Σ invoiced, and the band the gap falls in (FR-23.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

CORROBORATED = "CORROBORATED"
PARTIAL = "PARTIAL"
CONTRADICTED = "CONTRADICTED"
NO_EVIDENCE = "NO_EVIDENCE"


@dataclass
class Check:
    kind: str  # goods | services | subscription
    unit: str  # units | hours | seats
    expected: Decimal
    found: Decimal = Decimal(0)
    # Evidence node ids. `present` is True once any evidence exists, even
    # when none of it counts (a late delivery, an undelivered shipment).
    evidence: list[str] = field(default_factory=list)
    present: bool = False
    unit_price: Decimal | None = None


def band(expected: Decimal, found: Decimal, present: bool, tol: Decimal) -> str:
    """`tol` is a fraction (0.02 = 2 %). Over-delivery corroborates."""
    if not present:
        return NO_EVIDENCE
    if found >= expected * (1 - tol):
        return CORROBORATED
    if found > 0 and expected - found <= expected * 3 * tol:
        return PARTIAL
    return CONTRADICTED


def unit_price(pairs: list[tuple[Decimal, Decimal | None]]) -> Decimal | None:
    """Σ(qty x price) / Σ qty over the priced lines; None when none is."""
    priced = [(q, p) for q, p in pairs if p is not None and q > 0]
    total = sum((q for q, _ in priced), Decimal(0))
    if not total:
        return None
    return sum((q * p for q, p in priced), Decimal(0)) / total
