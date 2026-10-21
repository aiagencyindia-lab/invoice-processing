"""Contract -> PO -> GRN -> Invoice -> Approval -> Payment -> GL, with the gaps
named (PRD FR-19.6).

# ponytail: a bounded walk over the store's edge queries; a recursive CTE if
# lineage p95 passes 2 s at 5M edges per tenant (ADR-12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

CHAIN = ("contract", "po", "grn", "invoice", "approval", "payment", "gl")
Status = Literal["found", "no_evidence", "contradicts"]


@dataclass(frozen=True)
class Link:
    stage: str
    status: Status
    node_id: str | None
    note: str


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except ArithmeticError:
        return None


def lineage(
    tenant_id: str,
    node_id: str,
    *,
    graph,
    links=None,
    as_of: datetime | None = None,
) -> list[Link]:
    inv = graph.node(tenant_id, node_id)
    if inv is None:
        raise KeyError(node_id)

    def out(kind, src):
        return [
            e["dst"]
            for e in graph.edges(tenant_id, src=src, kind=kind, as_of=as_of)
        ]

    def into(kind, dst):
        return [
            e["src"]
            for e in graph.edges(tenant_id, dst=dst, kind=kind, as_of=as_of)
        ]

    def same(a, b):
        if links is None:
            return a == b
        return links.resolved_id(tenant_id, a) == links.resolved_id(
            tenant_id, b
        )

    vendors = out("issued_by", node_id)
    contracts = out("governed_by", node_id)
    if not contracts:
        result = [
            Link(
                "contract",
                "no_evidence",
                None,
                "No contract governs this invoice.",
            )
        ]
    else:
        contract_vendors = out("governs", contracts[0])
        if (
            vendors
            and contract_vendors
            and not same(vendors[0], contract_vendors[0])
        ):
            result = [
                Link(
                    "contract",
                    "contradicts",
                    contracts[0],
                    "The governing contract is with a different vendor.",
                )
            ]
        else:
            result = [
                Link("contract", "found", contracts[0], "Governed by contract.")
            ]
    pos = out("references", node_id)
    po_id = pos[0] if pos else None
    if po_id is None:
        result.append(
            Link("po", "no_evidence", None, "No purchase order referenced.")
        )
    else:
        po_vendors = out("issued_by", po_id)
        if vendors and po_vendors and not same(vendors[0], po_vendors[0]):
            result.append(
                Link(
                    "po",
                    "contradicts",
                    po_id,
                    "The purchase order was issued to a different vendor.",
                )
            )
        else:
            result.append(
                Link("po", "found", po_id, "Purchase order referenced.")
            )

    def quantity(
        node_id: str, line_kind: str, attrs: tuple[str, ...]
    ) -> Decimal | None:
        """Sum of the lines' first present attr; the node's own when it has
        no lines (a v1 GRN says qty_received, an imported one quantity)."""

        def first(node: dict) -> Decimal | None:
            for attr in attrs:
                value = _dec(node["attrs"].get(attr))
                if value is not None:
                    return value
            return None

        lines = [graph.node(tenant_id, n) for n in into("part_of", node_id)]
        values = [first(n) for n in lines if n and n["kind"] == line_kind]
        values = [v for v in values if v is not None]
        if values:
            return sum(values, Decimal(0))
        return first(graph.node(tenant_id, node_id))

    grns = into("fulfils", po_id) if po_id else []
    if not grns:
        result.append(
            Link(
                "grn",
                "no_evidence",
                None,
                "No goods receipt against the purchase order.",
            )
        )
    else:
        received = sum(
            (
                quantity(g, "grn_line", ("qty_received", "quantity"))
                or Decimal(0)
            )
            for g in grns
        )
        wanted = quantity(node_id, "invoice_line", ("quantity",))
        if wanted is not None and received < wanted:
            result.append(
                Link(
                    "grn",
                    "contradicts",
                    grns[0],
                    f"Received {received} against {wanted} invoiced.",
                )
            )
        else:
            result.append(
                Link("grn", "found", grns[0], f"{len(grns)} receipt(s) found.")
            )

    result.append(Link("invoice", "found", node_id, "This invoice."))
    approvals = into("approves", node_id)
    result.append(
        Link(
            "approval",
            "found",
            approvals[0],
            f"{len(approvals)} approval step(s).",
        )
        if approvals
        else Link("approval", "no_evidence", None, "No approval recorded.")
    )
    payments = into("settles", node_id)
    result.append(
        Link("payment", "found", payments[0], f"{len(payments)} payment(s).")
        if payments
        else Link("payment", "no_evidence", None, "No payment matched.")
    )
    result.append(
        Link(
            "gl",
            "no_evidence",
            None,
            "GL postings are not projected yet.",
        )
    )
    return result
