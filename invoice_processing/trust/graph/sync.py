"""Project v1 tables and imported history into the graph. Idempotent.

Engines never read v1 or history tables: everything they need is copied as
scalars into node attrs here, masked, and linked by edges whose provenance
and confidence say how we know.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from ... import po_matching
from ...db.models import (
    ApprovalStep,
    GoodsReceipt,
    GrnLine,
    HistoryRecord,
    Invoice,
    InvoiceLine,
    PoLine,
    PurchaseOrder,
    TrustHistoryImport,
    Vendor,
    VendorBankChange,
)
from ...db.session import tenant_session
from ...ports import RunContext
from ..history.importer import parse_date
from . import resolve

SOURCE_V1 = "v1"
# Parties and orders before the documents that point at them.
_HISTORY_ORDER = {"vendor": 0, "po": 1, "grn": 2, "ap_invoice": 3, "payment": 4}
_HISTORY_KIND = {"ap_invoice": "invoice"}


def _money(v) -> str | None:
    return None if v is None else str(Decimal(v).quantize(Decimal("0.0001")))


def _date(v) -> str | None:
    if not v:
        return None
    try:
        return parse_date(str(v)).isoformat()
    except ValueError:
        return None


def _edge(graph, ctx, src, dst, kind, source, at, confidence=1.0):
    graph.upsert_edge(
        ctx,
        src=src,
        dst=dst,
        kind=kind,
        source_system=source,
        provenance="system_record",
        confidence=confidence,
        observed_at=at,
        at=at,
    )


class _Vendors:
    """Vendor nodes keyed by tax id and normalised name, through entity links."""

    def __init__(self, ctx, graph, links):
        self.ctx, self.graph, self.links = ctx, graph, links
        self.by_key: dict[tuple[str, str], str] = {}
        for n in graph.nodes(ctx.tenant_id, kind="vendor"):
            self.remember(n["id"], n.get("natural_keys") or {})

    def remember(self, node_id: str, keys: dict) -> None:
        for k, v in keys.items():
            self.by_key.setdefault((k, v), node_id)

    def node_for(
        self, *, name: str | None, tax_id: str | None, source: str, at
    ) -> tuple[str, float]:
        keys = resolve.natural_keys({"name": name, "gstin": tax_id})
        for k in ("gstin", "pan", "name"):
            if k in keys and (k, keys[k]) in self.by_key:
                node = self.links.resolved_id(
                    self.ctx.tenant_id, self.by_key[(k, keys[k])]
                )
                return node, 1.0 if k != "name" else 0.9
        # Unknown vendor: a node from the record itself, so the edge exists.
        label = keys.get("gstin") or keys.get("name") or name
        nid = self.graph.upsert_node(
            self.ctx,
            kind="vendor",
            natural_key=f"{source}:{label}",
            attrs={"name": name, "gstin": tax_id},
            natural_keys=keys,
            at=at,
        )
        self.remember(nid, keys)
        return nid, 0.9


def _sync_v1_masters(ctx, s, graph, vendors, at, bump) -> dict[str, str]:
    """Vendors, purchase orders, receipts and bank changes. Returns PO nodes by number."""
    for v in s.scalars(select(Vendor).where(Vendor.tenant_id == ctx.tenant_id)):
        attrs = {
            "name": v.name,
            "gstin": v.tax_id,
            "email_domain": v.email_domain,
            "bank_account_number": v.bank_account_number,
            "bank_code": v.bank_code,
        }
        nid = graph.upsert_node(
            ctx,
            kind="vendor",
            natural_key=f"vendors:{v.id}",
            ref_table="vendors",
            ref_id=v.id,
            attrs=attrs,
            natural_keys=resolve.natural_keys(attrs),
            at=at,
        )
        vendors.remember(nid, resolve.natural_keys(attrs))
        bump("vendor")

    po_nodes: dict[str, str] = {}
    for po in s.scalars(
        select(PurchaseOrder).where(PurchaseOrder.tenant_id == ctx.tenant_id)
    ):
        po_id = graph.upsert_node(
            ctx,
            kind="po",
            natural_key=f"purchase_orders:{po.id}",
            ref_table="purchase_orders",
            ref_id=po.id,
            attrs={
                "po_number": po.po_number,
                "vendor_name": po.vendor_name,
                "amount": _money(po.total_amount),
                "currency": po.currency,
                "status": po.status,
            },
            at=at,
        )
        po_nodes[po.po_number] = po_id
        bump("po")
        ven, conf = vendors.node_for(
            name=po.vendor_name, tax_id=None, source=SOURCE_V1, at=at
        )
        _edge(graph, ctx, po_id, ven, "issued_by", SOURCE_V1, at, conf)
        for line in s.scalars(
            select(PoLine).where(PoLine.purchase_order_id == po.id)
        ):
            lid = graph.upsert_node(
                ctx,
                kind="po_line",
                natural_key=f"po_lines:{line.id}",
                ref_table="po_lines",
                ref_id=line.id,
                attrs={
                    "line_number": line.line_number,
                    "qty_ordered": _money(line.qty_ordered),
                    "unit_price": _money(line.unit_price),
                },
                at=at,
            )
            _edge(graph, ctx, lid, po_id, "part_of", SOURCE_V1, at)
            bump("po_line")

    line_ids = [
        r[0]
        for r in s.execute(
            select(PoLine.id).where(PoLine.tenant_id == ctx.tenant_id)
        )
    ]
    po_line_nodes = graph.nodes_by_ref(ctx.tenant_id, "po_lines", line_ids)
    for grn in s.scalars(
        select(GoodsReceipt).where(GoodsReceipt.tenant_id == ctx.tenant_id)
    ):
        gid = graph.upsert_node(
            ctx,
            kind="grn",
            natural_key=f"goods_receipts:{grn.id}",
            ref_table="goods_receipts",
            ref_id=grn.id,
            attrs={
                "grn_number": grn.grn_number,
                "received_on": _date(grn.received_on),
            },
            at=at,
        )
        bump("grn")
        po_node = graph.nodes_by_ref(
            ctx.tenant_id, "purchase_orders", [grn.purchase_order_id]
        ).get(grn.purchase_order_id)
        if po_node:
            _edge(graph, ctx, gid, po_node, "fulfils", SOURCE_V1, at)
        for gl in s.scalars(
            select(GrnLine).where(GrnLine.goods_receipt_id == grn.id)
        ):
            glid = graph.upsert_node(
                ctx,
                kind="grn_line",
                natural_key=f"grn_lines:{gl.id}",
                ref_table="grn_lines",
                ref_id=gl.id,
                attrs={"qty_received": _money(gl.qty_received)},
                at=at,
            )
            _edge(graph, ctx, glid, gid, "part_of", SOURCE_V1, at)
            if gl.po_line_id in po_line_nodes:
                _edge(
                    graph,
                    ctx,
                    glid,
                    po_line_nodes[gl.po_line_id],
                    "receives",
                    SOURCE_V1,
                    at,
                )
            bump("grn_line")

    for ch in s.scalars(
        select(VendorBankChange).where(
            VendorBankChange.tenant_id == ctx.tenant_id
        )
    ):
        cid = graph.upsert_node(
            ctx,
            kind="bank_change",
            natural_key=f"vendor_bank_changes:{ch.id}",
            ref_table="vendor_bank_changes",
            ref_id=ch.id,
            attrs={
                "status": ch.status,
                "proposed_by": ch.proposed_by,
                "bank_account_number": ch.proposed_account_number,
            },
            at=at,
        )
        ven = graph.nodes_by_ref(ctx.tenant_id, "vendors", [ch.vendor_id]).get(
            ch.vendor_id
        )
        if ven:
            _edge(graph, ctx, cid, ven, "changes", SOURCE_V1, at)
        bump("bank_change")
    return po_nodes


def _sync_v1_invoices(ctx, s, graph, vendors, po_nodes, at, bump, document_id):
    inv_q = select(Invoice).where(Invoice.tenant_id == ctx.tenant_id)
    if document_id is not None:
        inv_q = inv_q.where(Invoice.document_id == document_id)
    for inv in s.scalars(inv_q):
        attrs = {
            "invoice_number": inv.invoice_number,
            "invoice_date": _date(inv.invoice_date),
            "po_number": inv.po_number,
            "amount": _money(inv.total_inc_tax),
            "currency": inv.currency,
            "vendor_name": inv.vendor_name,
            "vendor_tax_id": inv.vendor_tax_id,
            "decision": inv.decision,
            "case_id": inv.case_id,
        }
        iid = graph.upsert_node(
            ctx,
            kind="invoice",
            natural_key=f"invoices:{inv.id}",
            ref_table="invoices",
            ref_id=inv.id,
            attrs=attrs,
            at=at,
        )
        bump("invoice")
        ven, conf = vendors.node_for(
            name=inv.vendor_name,
            tax_id=inv.vendor_tax_id,
            source=SOURCE_V1,
            at=at,
        )
        _edge(graph, ctx, iid, ven, "issued_by", SOURCE_V1, at, conf)
        po_key = (
            po_matching.normalize_po_number(inv.po_number)
            if inv.po_number
            else None
        )
        if po_key and po_key in po_nodes:
            _edge(
                graph, ctx, iid, po_nodes[po_key], "references", SOURCE_V1, at
            )
        for line in s.scalars(
            select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
        ):
            lid = graph.upsert_node(
                ctx,
                kind="invoice_line",
                natural_key=f"invoice_lines:{line.id}",
                ref_table="invoice_lines",
                ref_id=line.id,
                attrs={
                    "line_number": line.line_number,
                    "description": line.description,
                    "quantity": _money(line.quantity),
                    "unit_price": _money(line.unit_price),
                    "amount": _money(line.amount_ex_tax),
                },
                at=at,
            )
            _edge(graph, ctx, lid, iid, "part_of", SOURCE_V1, at)
            bump("invoice_line")
        for step in s.scalars(
            select(ApprovalStep).where(
                ApprovalStep.tenant_id == ctx.tenant_id,
                ApprovalStep.case_id == inv.case_id,
            )
        ):
            aid = graph.upsert_node(
                ctx,
                kind="approval",
                natural_key=f"approval_steps:{step.id}",
                ref_table="approval_steps",
                ref_id=step.id,
                attrs={
                    "position": step.position,
                    "status": step.status,
                    "decided_by": step.decided_by,
                },
                at=at,
            )
            _edge(graph, ctx, aid, iid, "approves", SOURCE_V1, at)
            bump("approval")


def _sync_history(ctx, s, graph, vendors, po_nodes, at, bump):
    sources = {
        r.id: r.source_name
        for r in s.scalars(
            select(TrustHistoryImport).where(
                TrustHistoryImport.tenant_id == ctx.tenant_id
            )
        )
    }
    records = sorted(
        s.scalars(
            select(HistoryRecord).where(
                HistoryRecord.tenant_id == ctx.tenant_id
            )
        ),
        key=lambda r: (_HISTORY_ORDER.get(r.kind, 9), r.row_number),
    )
    hist_invoices: dict[str, str] = {}
    for rec in records:
        source = f"history:{sources.get(rec.import_id, '?')}"
        f = rec.fields or {}
        if rec.kind == "vendor":
            keys = resolve.natural_keys(
                {
                    "name": rec.vendor_key,
                    "gstin": f.get("gstin"),
                    "pan": f.get("pan"),
                    "abn": f.get("abn"),
                    "email_domain": f.get("email_domain"),
                }
            )
            nid = graph.upsert_node(
                ctx,
                kind="vendor",
                natural_key=f"history_records:{rec.id}",
                ref_table="history_records",
                ref_id=rec.id,
                attrs={
                    "name": rec.vendor_key,
                    **{
                        k: f.get(k)
                        for k in ("gstin", "pan", "abn", "email_domain")
                    },
                },
                natural_keys=keys,
                at=at,
            )
            vendors.remember(nid, keys)
            bump("vendor")
            continue
        kind = _HISTORY_KIND.get(rec.kind, rec.kind)
        po_number = f.get("po_number") or None
        attrs = {
            "doc_number": rec.doc_number,
            "invoice_number": rec.doc_number,
            "invoice_date": rec.record_date.isoformat()
            if rec.record_date
            else None,
            "amount": _money(rec.amount),
            "currency": rec.currency,
            "vendor_name": rec.vendor_key,
            "external_id": rec.external_id,
            "po_number": po_number,
        }
        nid = graph.upsert_node(
            ctx,
            kind=kind,
            natural_key=f"history_records:{rec.id}",
            ref_table="history_records",
            ref_id=rec.id,
            attrs=attrs,
            at=at,
        )
        bump(kind)
        if rec.vendor_key:
            ven, conf = vendors.node_for(
                name=rec.vendor_key, tax_id=f.get("gstin"), source=source, at=at
            )
            _edge(graph, ctx, nid, ven, "issued_by", source, at, conf)
        po_key = (
            po_matching.normalize_po_number(po_number) if po_number else None
        )
        if kind == "po" and rec.doc_number:
            po_nodes[po_matching.normalize_po_number(rec.doc_number)] = nid
        elif kind == "grn" and po_key in po_nodes:
            _edge(graph, ctx, nid, po_nodes[po_key], "fulfils", source, at, 0.9)
        elif kind == "invoice":
            if rec.doc_number:
                hist_invoices[rec.doc_number] = nid
            if po_key in po_nodes:
                _edge(
                    graph,
                    ctx,
                    nid,
                    po_nodes[po_key],
                    "references",
                    source,
                    at,
                    0.9,
                )
        elif kind == "payment" and rec.doc_number in hist_invoices:
            _edge(
                graph,
                ctx,
                nid,
                hist_invoices[rec.doc_number],
                "settles",
                source,
                at,
                0.9,
            )


def sync_tenant(
    ctx: RunContext,
    graph,
    links,
    *,
    url: str | None = None,
    document_id: str | None = None,
    decider=None,
    at: datetime | None = None,
) -> dict:
    at = at or datetime.now(timezone.utc)
    counts: dict[str, int] = {}

    def bump(kind):
        counts[kind] = counts.get(kind, 0) + 1

    with tenant_session(ctx.tenant_id, url) as s:
        vendors = _Vendors(ctx, graph, links)
        if document_id is None:
            po_nodes = _sync_v1_masters(ctx, s, graph, vendors, at, bump)
        else:
            po_nodes = {
                n["attrs"].get("po_number"): n["id"]
                for n in graph.nodes(ctx.tenant_id, kind="po")
            }
        _sync_v1_invoices(
            ctx, s, graph, vendors, po_nodes, at, bump, document_id
        )
        if document_id is None:
            _sync_history(ctx, s, graph, vendors, po_nodes, at, bump)

    if document_id is None:
        counts["links"] = resolve.run(ctx, graph, links, decider=decider)
    return counts
