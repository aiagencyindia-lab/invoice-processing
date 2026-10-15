"""One read of the tenant's graph, shaped for the Leakage rules."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from ..findings.model import Citation
from .clauses import scrub

logger = logging.getLogger(__name__)
SOURCE = "leakage"
_KINDS = (
    "invoice",
    "invoice_line",
    "payment",
    "vendor_statement_line",
    "contract",
    "contract_clause",
)


def money(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def cite(node: dict, excerpt: str | None = None) -> Citation:
    return Citation(node["id"], scrub(excerpt) if excerpt else None)


class View:
    """Nodes by kind, the edges the rules walk, vendors made canonical.

    ponytail: a full tenant read per evaluation; index by vendor in the graph
    when one tenant passes ~100k invoices.
    """

    def __init__(self, ectx):
        tenant, graph = ectx.ctx.tenant_id, ectx.graph
        self.canonical = ectx.links.canonical_map(tenant)
        self.nodes: dict[str, dict] = {}
        self.of_kind: dict[str, list[dict]] = {}
        for kind in _KINDS:
            self.of_kind[kind] = graph.nodes(tenant, kind=kind)
            self.nodes.update((n["id"], n) for n in self.of_kind[kind])
        self.raw_vendor: dict[str, str] = {}
        for e in graph.edges(tenant, kind="issued_by"):
            self.raw_vendor[e["src"]] = e["dst"]
        self.lines: dict[str, list[dict]] = defaultdict(list)
        self.clauses: dict[str, list[dict]] = defaultdict(list)
        for e in graph.edges(tenant, kind="part_of"):
            src = self.nodes.get(e["src"])
            if src is None:
                continue
            if src["kind"] == "invoice_line":
                self.lines[e["dst"]].append(src)
            elif src["kind"] == "contract_clause":
                self.clauses[e["dst"]].append(src)
        for group in self.lines.values():
            group.sort(key=lambda n: n["attrs"].get("line_number") or 0)
        self.settled_by: dict[str, list[dict]] = defaultdict(list)
        for e in graph.edges(tenant, kind="settles"):
            if e["src"] in self.nodes:
                self.settled_by[e["dst"]].append(self.nodes[e["src"]])
        governs = {
            e["src"]: e["dst"] for e in graph.edges(tenant, kind="governs")
        }
        wanted = [
            c["attrs"]["vendor_id"]
            for c in self.of_kind["contract"]
            if c["id"] not in governs and c["attrs"].get("vendor_id")
        ]
        by_ref = graph.nodes_by_ref(tenant, "vendors", wanted)
        self.contracts_of: dict[str, list[dict]] = defaultdict(list)
        for c in self.of_kind["contract"]:
            ven = governs.get(c["id"]) or by_ref.get(
                c["attrs"].get("vendor_id")
            )
            if ven:
                self.contracts_of[self.canon(ven)].append(c)
        self.invoices = sorted(
            self.of_kind["invoice"],
            key=lambda n: (
                day(n["attrs"].get("invoice_date")) or date.min,
                n["natural_key"],
            ),
        )

    def canon(self, node_id: str | None) -> str | None:
        return self.canonical.get(node_id, node_id) if node_id else None

    def vendor(self, node_id: str) -> str | None:
        return self.canon(self.raw_vendor.get(node_id))
