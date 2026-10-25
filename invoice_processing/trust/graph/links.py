"""Entity links: reversible, audited, never deleting a node."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from ...db.models import EntityLink
from ...db.repositories import audit_event_row
from ...db.session import tenant_session
from ...ports import RunContext
from .resolve import band


def _now() -> datetime:
    return datetime.now(UTC)


def _dict(link: EntityLink) -> dict:
    return {
        "id": link.id,
        "node_a": link.node_a,
        "node_b": link.node_b,
        "score": float(link.score),
        "method": link.method,
        "status": link.status,
        "decided_by": link.decided_by,
        "reason": link.reason,
        "decided_at": link.decided_at.isoformat() if link.decided_at else None,
        "reversed_at": link.reversed_at.isoformat()
        if link.reversed_at
        else None,
    }


class LinkStore:
    def __init__(self, url: str | None = None):
        self.url = url

    def _get(self, s, tenant_id: str, link_id: str) -> EntityLink:
        row = s.scalars(
            select(EntityLink).where(
                EntityLink.tenant_id == tenant_id, EntityLink.id == link_id
            )
        ).first()
        if row is None:
            raise KeyError(link_id)
        return row

    def propose_or_link(
        self,
        ctx: RunContext,
        node_a: str,
        node_b: str,
        score: float,
        method: str,
        reason: str | None = None,
    ) -> dict:
        node_a, node_b = sorted((node_a, node_b))
        status = (
            EntityLink.AUTO if band(score) == "auto" else EntityLink.PROPOSED
        )
        with tenant_session(ctx.tenant_id, self.url) as s:
            open_row = s.scalars(
                select(EntityLink).where(
                    EntityLink.tenant_id == ctx.tenant_id,
                    EntityLink.node_a == node_a,
                    EntityLink.node_b == node_b,
                    EntityLink.reversed_at.is_(None),
                )
            ).first()
            if open_row is not None:
                return _dict(open_row)
            row = EntityLink(
                tenant_id=ctx.tenant_id,
                node_a=node_a,
                node_b=node_b,
                score=Decimal(str(score)).quantize(Decimal("0.0001")),
                method=method,
                status=status,
                reason=reason,
            )
            s.add(row)
            s.add(
                audit_event_row(
                    ctx,
                    f"entity_link_{status}",
                    {
                        "node_a": node_a,
                        "node_b": node_b,
                        "score": score,
                        "method": method,
                    },
                )
            )
            s.flush()
            return _dict(row)

    def decide(
        self,
        ctx: RunContext,
        link_id: str,
        *,
        accept: bool,
        actor: str,
        reason: str | None = None,
    ) -> dict:
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = self._get(s, ctx.tenant_id, link_id)
            if row.status != EntityLink.PROPOSED:
                raise ValueError(f"This link was already {row.status}.")
            row.status = EntityLink.ACCEPTED if accept else EntityLink.REJECTED
            row.decided_by, row.decided_at = actor, _now()
            if reason:
                row.reason = reason
            s.add(
                audit_event_row(
                    ctx,
                    f"entity_link_{row.status}",
                    {"link_id": link_id, "actor": actor, "reason": reason},
                )
            )
            s.flush()
            return _dict(row)

    def reverse(
        self, ctx: RunContext, link_id: str, *, actor: str, reason: str
    ) -> dict:
        if not (reason or "").strip():
            raise ValueError("A reason is required to reverse a link.")
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = self._get(s, ctx.tenant_id, link_id)
            row.reversed_at, row.decided_by, row.reason = _now(), actor, reason
            s.add(
                audit_event_row(
                    ctx,
                    "entity_link_reversed",
                    {"link_id": link_id, "actor": actor, "reason": reason},
                )
            )
            s.flush()
            return _dict(row)

    def open_links(
        self, tenant_id: str, *, status: str | None = None
    ) -> list[dict]:
        with tenant_session(tenant_id, self.url) as s:
            q = select(EntityLink).where(
                EntityLink.tenant_id == tenant_id,
                EntityLink.reversed_at.is_(None),
            )
            if status:
                q = q.where(EntityLink.status == status)
            return [
                _dict(r) for r in s.scalars(q.order_by(EntityLink.created_at))
            ]

    def pairs(self, tenant_id: str) -> set[tuple[str, str]]:
        """Every pair ever linked, proposed, rejected or reversed."""
        with tenant_session(tenant_id, self.url) as s:
            rows = s.execute(
                select(EntityLink.node_a, EntityLink.node_b).where(
                    EntityLink.tenant_id == tenant_id
                )
            )
            return {(a, b) for a, b in rows}

    def canonical_map(self, tenant_id: str) -> dict[str, str]:
        """node id -> the smallest id in its component of auto/accepted
        links, for every node that has one. One query, then union-find; a
        sync looks up thousands of nodes against it."""
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x

        for link in self.open_links(tenant_id):
            if link["status"] not in (EntityLink.AUTO, EntityLink.ACCEPTED):
                continue
            ra, rb = find(link["node_a"]), find(link["node_b"])
            if ra != rb:
                lo, hi = sorted((ra, rb))
                parent[hi] = lo
        return {node: find(node) for node in list(parent)}

    def resolved_id(self, tenant_id: str, node_id: str) -> str:
        """The canonical node for one id (see canonical_map)."""
        return self.canonical_map(tenant_id).get(node_id, node_id)
