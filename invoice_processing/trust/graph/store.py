"""Time-travel storage for graph nodes and edges.

A row is never updated. Writing a node whose content changed closes the open
row (valid_to) and inserts a new one; writing identical content returns the
open row. `as_of` filters `valid_from <= D < valid_to`, which answers "what did
we know on D" without reconstruction (PRD FR-19.5).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import or_, select, update

from ... import masking
from ...db.models import EntityLink, GraphEdge, GraphNode, TrustFinding
from ...db.session import tenant_session
from ...ports import RunContext

PROVENANCE = ("document", "system_record", "human_assertion", "model_inference")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _node_dict(n: GraphNode) -> dict:
    return {
        "id": n.id,
        "kind": n.kind,
        "natural_key": n.natural_key,
        "ref_table": n.ref_table,
        "ref_id": n.ref_id,
        "attrs": dict(n.attrs or {}),
        "natural_keys": dict(n.natural_keys or {}),
        "valid_from": _iso(n.valid_from),
        "valid_to": _iso(n.valid_to),
    }


def _edge_dict(e: GraphEdge) -> dict:
    return {
        "id": e.id,
        "src": e.src,
        "dst": e.dst,
        "kind": e.kind,
        "source_system": e.source_system,
        "observed_at": _iso(e.observed_at),
        "provenance": e.provenance,
        "confidence": float(e.confidence),
        "attrs": dict(e.attrs or {}),
        "valid_from": _iso(e.valid_from),
        "valid_to": _iso(e.valid_to),
    }


def _as_of(model, query, as_of: datetime | None):
    if as_of is None:
        return query.where(model.valid_to.is_(None))
    return query.where(model.valid_from <= as_of).where(
        (model.valid_to.is_(None)) | (model.valid_to > as_of)
    )


class GraphStore:
    def __init__(self, url: str | None = None):
        self.url = url

    # -- nodes -------------------------------------------------------------

    def upsert_node(
        self,
        ctx: RunContext,
        *,
        kind: str,
        natural_key: str,
        ref_table: str | None = None,
        ref_id: str | None = None,
        attrs: dict | None = None,
        natural_keys: dict | None = None,
        at: datetime | None = None,
    ) -> str:
        at = at or _now()
        attrs = masking.mask_row(dict(attrs or {}))
        natural_keys = dict(natural_keys or {})
        with tenant_session(ctx.tenant_id, self.url) as s:
            open_row = s.scalars(
                select(GraphNode).where(
                    GraphNode.tenant_id == ctx.tenant_id,
                    GraphNode.kind == kind,
                    GraphNode.natural_key == natural_key,
                    GraphNode.valid_to.is_(None),
                )
            ).first()
            if open_row is not None:
                same = (
                    (open_row.attrs or {}) == attrs
                    and (open_row.natural_keys or {}) == natural_keys
                    and open_row.ref_table == ref_table
                    and open_row.ref_id == ref_id
                )
                if same:
                    return open_row.id
                open_row.valid_to = at
                s.flush()
            row = GraphNode(
                tenant_id=ctx.tenant_id,
                kind=kind,
                natural_key=natural_key,
                ref_table=ref_table,
                ref_id=ref_id,
                attrs=attrs,
                natural_keys=natural_keys,
                valid_from=at,
            )
            s.add(row)
            s.flush()
            if open_row is not None:
                self._retire(s, ctx.tenant_id, open_row.id, row.id, at)
            return row.id

    @staticmethod
    def _retire(
        s, tenant_id: str, old_id: str, new_id: str, at: datetime
    ) -> None:
        """A closed node takes its open edges with it (the next sync rebuilds
        them against the new row) and hands its entity links and findings to
        the new row, so a human's accepted or reversed link survives a
        re-version and a hold stays where the gate looks for it. Citations
        keep the old id: they record what the finding saw."""
        s.execute(
            update(GraphEdge)
            .where(
                GraphEdge.tenant_id == tenant_id,
                GraphEdge.valid_to.is_(None),
                or_(GraphEdge.src == old_id, GraphEdge.dst == old_id),
            )
            .values(valid_to=at)
        )
        for link in s.scalars(
            select(EntityLink).where(
                EntityLink.tenant_id == tenant_id,
                or_(EntityLink.node_a == old_id, EntityLink.node_b == old_id),
            )
        ):
            other = link.node_b if link.node_a == old_id else link.node_a
            link.node_a, link.node_b = sorted((new_id, other))
        s.execute(
            update(TrustFinding)
            .where(
                TrustFinding.tenant_id == tenant_id,
                TrustFinding.transaction_node == old_id,
            )
            .values(transaction_node=new_id)
        )
        s.flush()

    def node(self, tenant_id: str, node_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = s.get(GraphNode, node_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return _node_dict(row)

    def open_node(
        self, tenant_id: str, kind: str, natural_key: str
    ) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = s.scalars(
                select(GraphNode).where(
                    GraphNode.tenant_id == tenant_id,
                    GraphNode.kind == kind,
                    GraphNode.natural_key == natural_key,
                    GraphNode.valid_to.is_(None),
                )
            ).first()
            return _node_dict(row) if row else None

    def nodes(
        self,
        tenant_id: str,
        *,
        kind: str | None = None,
        as_of: datetime | None = None,
    ) -> list[dict]:
        with tenant_session(tenant_id, self.url) as s:
            q = select(GraphNode).where(GraphNode.tenant_id == tenant_id)
            if kind:
                q = q.where(GraphNode.kind == kind)
            q = _as_of(GraphNode, q, as_of).order_by(
                GraphNode.kind, GraphNode.natural_key
            )
            return [_node_dict(n) for n in s.scalars(q)]

    def nodes_by_ref(
        self, tenant_id: str, ref_table: str, ref_ids: list[str]
    ) -> dict[str, str]:
        if not ref_ids:
            return {}
        with tenant_session(tenant_id, self.url) as s:
            q = select(GraphNode.ref_id, GraphNode.id).where(
                GraphNode.tenant_id == tenant_id,
                GraphNode.ref_table == ref_table,
                GraphNode.ref_id.in_(ref_ids),
                GraphNode.valid_to.is_(None),
            )
            return dict(s.execute(q).all())

    # -- edges -------------------------------------------------------------

    def upsert_edge(
        self,
        ctx: RunContext,
        *,
        src: str,
        dst: str,
        kind: str,
        source_system: str,
        provenance: str,
        confidence: float,
        observed_at: datetime,
        attrs: dict | None = None,
        at: datetime | None = None,
    ) -> str:
        if provenance not in PROVENANCE:
            raise ValueError(
                f"provenance must be one of {', '.join(PROVENANCE)}"
            )
        if not 0 <= float(confidence) <= 1:
            raise ValueError("confidence must be between 0 and 1")
        at = at or _now()
        attrs = masking.mask_row(dict(attrs or {}))
        conf = Decimal(str(confidence)).quantize(Decimal("0.0001"))
        with tenant_session(ctx.tenant_id, self.url) as s:
            open_row = s.scalars(
                select(GraphEdge).where(
                    GraphEdge.tenant_id == ctx.tenant_id,
                    GraphEdge.src == src,
                    GraphEdge.dst == dst,
                    GraphEdge.kind == kind,
                    GraphEdge.source_system == source_system,
                    GraphEdge.valid_to.is_(None),
                )
            ).first()
            if open_row is not None:
                same = (
                    open_row.provenance == provenance
                    and Decimal(open_row.confidence) == conf
                    and (open_row.attrs or {}) == attrs
                )
                if same:
                    return open_row.id
                open_row.valid_to = at
                s.flush()
            row = GraphEdge(
                tenant_id=ctx.tenant_id,
                src=src,
                dst=dst,
                kind=kind,
                source_system=source_system,
                observed_at=observed_at,
                provenance=provenance,
                confidence=conf,
                attrs=attrs,
                valid_from=at,
            )
            s.add(row)
            s.flush()
            return row.id

    def edges(
        self,
        tenant_id: str,
        *,
        src: str | None = None,
        dst: str | None = None,
        kind: str | None = None,
        as_of: datetime | None = None,
    ) -> list[dict]:
        with tenant_session(tenant_id, self.url) as s:
            q = select(GraphEdge).where(GraphEdge.tenant_id == tenant_id)
            if src:
                q = q.where(GraphEdge.src == src)
            if dst:
                q = q.where(GraphEdge.dst == dst)
            if kind:
                q = q.where(GraphEdge.kind == kind)
            q = _as_of(GraphEdge, q, as_of).order_by(
                GraphEdge.kind, GraphEdge.src, GraphEdge.dst
            )
            return [_edge_dict(e) for e in s.scalars(q)]
