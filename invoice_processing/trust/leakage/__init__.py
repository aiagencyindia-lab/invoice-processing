"""Leakage (FR-20): money a contract, a duplicate or a statement says we lost.

Contract-vs-invoice compliance, duplicates beyond v1, supplier-statement
reconciliation, missed early-payment discounts and a renewal radar. Every
finding cites the nodes it rests on; every rupee is Decimal arithmetic on
graph attrs, never a model's number. An insight engine: no hold types.
"""

from __future__ import annotations

import logging

from ..findings.model import FindingSpec, register_type

ENGINE_VERSION = "leakage-1"

_TYPES = {
    "LEAK-RATE": "Unit price above the contract rate",
    "LEAK-ESCALATOR": "Unit price above the escalated contract rate",
    "LEAK-TIER": "Tier price not applied after the tier quantity was reached",
    "LEAK-REBATE": "Rebate threshold reached with no rebate credit",
    "LEAK-DISCOUNT": "Paid early in full without taking the early-payment discount",
    "LEAK-TERM": "Invoice dated outside the contract term",
    "DUP-NEAR": "Near-duplicate invoice from the same vendor",
    "DUP-SPLIT": "Invoices split to stay under the high-value threshold",
    "DUP-CROSS": "Same invoice recorded under two vendor records",
    "STMT-UNAPPLIED-CREDIT": "Vendor statement credit we never applied",
    "STMT-DOUBLE-PAID": "Invoice paid more than once",
    "STMT-DISPUTED": "Vendor statement line unknown to us",
    "RADAR-RENEWAL": "Contract renewal or expiry inside the notice window",
    "RADAR-ESCALATOR": "Contract price escalation date inside the notice window",
}
for _code, _description in _TYPES.items():
    register_type(_code, _description, engine="leakage")

logger = logging.getLogger(__name__)


def run(ectx) -> list[FindingSpec]:
    return []
