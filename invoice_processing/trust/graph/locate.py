"""Where a node's evidence lives and whose it is. Shared by the API, the
audit report and the Why engine, so none of them imports another."""

from __future__ import annotations


def source_of(graph, tenant_id: str, node: dict | None) -> dict | None:
    """Where the evidence viewer finds this node's source document."""
    if not node:
        return None
    kind, attrs = node["kind"], node.get("attrs") or {}
    if kind in ("invoice", "document_version"):
        case_id = attrs.get("case_id") or node.get("ref_id")
        if not case_id:
            return None
        return {
            "document_url": f"/api/invoices/{case_id}/document",
            "page": None,
        }
    if kind == "contract" and node.get("ref_id"):
        return {
            "document_url": f"/api/trust/contracts/{node['ref_id']}/file",
            "page": attrs.get("page"),
        }
    if kind == "contract_clause":
        for e in graph.edges(tenant_id, src=node["id"], kind="part_of"):
            contract = graph.node(tenant_id, e["dst"])
            if contract and contract.get("ref_id"):
                return {
                    "document_url": f"/api/trust/contracts/{contract['ref_id']}/file",
                    "page": attrs.get("page"),
                }
    return None


def tax_id(attrs: dict) -> str | None:
    """A vendor node's tax id: what tells apart two vendors with one name
    (a misread GSTIN resolves to a vendor of its own)."""
    return attrs.get("gstin") or attrs.get("abn")


def vendor_of(graph, tenant_id: str, node_id: str) -> dict | None:
    """The vendor a transaction (or contract) node belongs to, by its edge."""
    for kind in ("issued_by", "governs"):
        for e in graph.edges(tenant_id, src=node_id, kind=kind):
            vendor = graph.node(tenant_id, e["dst"])
            if vendor and vendor["kind"] == "vendor":
                return {
                    "node": vendor["id"],
                    "name": vendor["attrs"].get("name"),
                    "tax_id": tax_id(vendor["attrs"]),
                }
    return None
