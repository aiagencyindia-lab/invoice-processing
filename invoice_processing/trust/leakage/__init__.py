"""Leakage (FR-20): money a contract, a duplicate or a statement says we lost.

Contract-vs-invoice compliance, duplicates beyond v1, supplier-statement
reconciliation, missed early-payment discounts and a renewal radar. Every
finding cites the nodes it rests on; every rupee is Decimal arithmetic on
graph attrs, never a model's number. An insight engine: no hold types.
"""

from __future__ import annotations

import logging

from ..findings.model import FindingSpec, register_type
from . import clauses, contracts, dups
from .view import View

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


def _scope(ectx, view):
    """Which invoices this run may draw edges for and report on: the one
    invoice, the contract's vendor's invoices, or (batch) all of them."""
    if ectx.invoice_node:
        return lambda node: node == ectx.invoice_node
    if ectx.document is None:
        return lambda node: True
    contract = ectx.graph.nodes_by_ref(
        ectx.ctx.tenant_id, "documents", [ectx.document["id"]]
    ).get(ectx.document["id"])
    vendors = {
        ven
        for ven, nodes in view.contracts_of.items()
        if any(n["id"] == contract for n in nodes)
    }
    return lambda node: view.vendor(node) in vendors


def run(ectx) -> list[FindingSpec]:
    if ectx.document and ectx.document.get("kind") == "contract":
        clauses.extract(ectx)
    elif ectx.document is not None and ectx.invoice_node is None:
        return []  # a document that is neither a contract nor an invoice
    view = View(ectx)
    in_scope = _scope(ectx, view)
    specs = [
        *contracts.findings(ectx, view, in_scope),
        *dups.findings(ectx, view),
    ]
    kept = [s for s in specs if in_scope(s.transaction_node)]
    logger.info(
        "leakage_run", extra={"found": len(specs), "reported": len(kept)}
    )
    return kept
