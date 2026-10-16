"""The only writer of trust_findings, its citations and its events."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select, update

from ...db.models import (
    GraphEdge,
    TrustFinding,
    TrustFindingCitation,
    TrustFindingEvent,
)
from ...db.repositories import audit_event_row
from ...db.session import tenant_session
from ...ports import RunContext, StatusChanged
from .model import (
    DECIDED,
    REASON_REQUIRED,
    FindingSpec,
    IllegalTransition,
    can_transition,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _money(value) -> str | None:
    if value is None:
        return None
    return str(Decimal(value).quantize(Decimal("0.0001")))


def _finding_dict(f: TrustFinding, citations=(), events=()) -> dict:
    return {
        "id": f.id,
        "type": f.type,
        "transaction_node": f.transaction_node,
        "status": f.status,
        "impact": _money(f.impact),
        "currency": f.currency,
        "confidence": None if f.confidence is None else float(f.confidence),
        "owner_id": f.owner_id,
        "engine_version": f.engine_version,
        "rule_version": f.rule_version,
        "model_version": f.model_version,
        "dedupe_key": f.dedupe_key,
        "summary": f.summary,
        "details": dict(f.details or {}),
        "decided_at": f.decided_at.isoformat() if f.decided_at else None,
        "citations": [
            {"node_id": c.node_id, "excerpt": c.excerpt} for c in citations
        ],
        "events": [
            {
                "from_status": e.from_status,
                "to_status": e.to_status,
                "amount": _money(e.amount),
                "actor": e.actor,
                "reason": e.reason,
                "at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ],
    }


class FindingStore:
    def __init__(self, url: str | None = None):
        self.url = url

    def upsert(self, ctx: RunContext, spec: FindingSpec) -> tuple[str, str]:
        """Create or refresh by dedupe_key. A refresh updates the money,
        confidence, versions and citations in place and writes no event: the
        ledger records status changes (spec §4.4), and the audit of a
        changed amount is the engine's own run log."""
        if not spec.citations:
            raise ValueError("A finding needs at least one citation.")
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = s.scalars(
                select(TrustFinding).where(
                    TrustFinding.tenant_id == ctx.tenant_id,
                    TrustFinding.dedupe_key == spec.dedupe_key,
                )
            ).first()
            if row is not None and row.status in DECIDED:
                return row.id, "untouched"
            created = row is None
            if created:
                row = TrustFinding(
                    tenant_id=ctx.tenant_id,
                    type=spec.type,
                    transaction_node=spec.transaction_node,
                    dedupe_key=spec.dedupe_key,
                    status="detected",
                )
                s.add(row)
                s.flush()
            row.impact, row.currency = spec.impact, spec.currency
            row.confidence = spec.confidence
            row.engine_version = spec.engine_version
            row.rule_version = spec.rule_version
            row.model_version = spec.model_version
            row.summary, row.details = spec.summary, dict(spec.details)
            s.execute(
                delete(TrustFindingCitation).where(
                    TrustFindingCitation.finding_id == row.id
                )
            )
            for c in spec.citations:
                s.add(
                    TrustFindingCitation(
                        tenant_id=ctx.tenant_id,
                        finding_id=row.id,
                        node_id=c.node_id,
                        excerpt=c.excerpt,
                    )
                )
            if created:
                s.add(
                    TrustFindingEvent(
                        tenant_id=ctx.tenant_id,
                        finding_id=row.id,
                        from_status=None,
                        to_status="detected",
                        amount=spec.impact,
                        actor=ctx.actor or "engine",
                    )
                )
            return row.id, "created" if created else "refreshed"

    def transition(
        self,
        ctx: RunContext,
        finding_id: str,
        to_status: str,
        *,
        actor: str,
        reason: str | None = None,
        amount: Decimal | None = None,
        expect: str | None = None,
    ) -> dict:
        if to_status in REASON_REQUIRED and not (reason or "").strip():
            raise ValueError(
                f"A reason is required to mark a finding {to_status}."
            )
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = s.scalars(
                select(TrustFinding).where(
                    TrustFinding.tenant_id == ctx.tenant_id,
                    TrustFinding.id == finding_id,
                )
            ).first()
            if row is None:
                raise KeyError(finding_id)
            was = row.status
            if expect is not None and was != expect:
                raise StatusChanged(was)
            if not can_transition(was, to_status):
                raise IllegalTransition(
                    f"A finding cannot go from {was} to {to_status}."
                )
            values = {"status": to_status, "updated_at": _now()}
            if to_status in DECIDED:
                values["decided_at"] = _now()
                values["owner_id"] = actor
            changed = s.execute(
                update(TrustFinding)
                .where(
                    TrustFinding.tenant_id == ctx.tenant_id,
                    TrustFinding.id == finding_id,
                    TrustFinding.status == was,
                )
                .values(**values)
            ).rowcount
            if changed != 1:  # moved between our read and our write
                raise StatusChanged(was)
            s.add(
                TrustFindingEvent(
                    tenant_id=ctx.tenant_id,
                    finding_id=finding_id,
                    from_status=was,
                    to_status=to_status,
                    amount=amount,
                    actor=actor,
                    reason=reason,
                )
            )
            s.add(
                audit_event_row(
                    ctx.child(case_id=finding_id),
                    f"trust_finding_{to_status}",
                    {
                        "finding_id": finding_id,
                        "from": was,
                        "to": to_status,
                        "amount": _money(amount),
                        "reason": reason,
                    },
                )
            )
            s.flush()
            s.refresh(row)
            return {**_finding_dict(row), "previous": was}

    def get(self, tenant_id: str, finding_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = s.scalars(
                select(TrustFinding).where(
                    TrustFinding.tenant_id == tenant_id,
                    TrustFinding.id == finding_id,
                )
            ).first()
            if row is None:
                return None
            cites = s.scalars(
                select(TrustFindingCitation).where(
                    TrustFindingCitation.finding_id == row.id
                )
            ).all()
            events = s.scalars(
                select(TrustFindingEvent)
                .where(TrustFindingEvent.finding_id == row.id)
                .order_by(TrustFindingEvent.created_at)
            ).all()
            return _finding_dict(row, cites, events)

    def _filtered(
        self,
        tenant_id: str,
        *,
        status=None,
        statuses=None,
        type=None,
        types=None,
        transaction_node=None,
        vendor_node=None,
        since=None,
        until=None,
    ):
        q = select(TrustFinding).where(TrustFinding.tenant_id == tenant_id)
        wanted = set(statuses or ()) | ({status} if status else set())
        if wanted:
            q = q.where(TrustFinding.status.in_(sorted(wanted)))
        kinds = set(types or ()) | ({type} if type else set())
        if kinds:
            q = q.where(TrustFinding.type.in_(sorted(kinds)))
        if transaction_node:
            q = q.where(TrustFinding.transaction_node == transaction_node)
        if vendor_node:
            # The open issued_by edge of the finding's transaction node; a
            # re-versioned invoice carries its findings to the new node.
            issued = (
                select(GraphEdge.src)
                .where(
                    GraphEdge.tenant_id == tenant_id,
                    GraphEdge.kind == "issued_by",
                    GraphEdge.dst == vendor_node,
                    GraphEdge.valid_to.is_(None),
                )
                .scalar_subquery()
            )
            q = q.where(TrustFinding.transaction_node.in_(issued))
        if since is not None:
            q = q.where(TrustFinding.created_at >= since)
        if until is not None:
            q = q.where(TrustFinding.created_at < until)
        return q

    def list(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        statuses=None,
        type: str | None = None,
        types=None,
        transaction_node: str | None = None,
        vendor_node: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        q = self._filtered(
            tenant_id,
            status=status,
            statuses=statuses,
            type=type,
            types=types,
            transaction_node=transaction_node,
            vendor_node=vendor_node,
            since=since,
            until=until,
        ).order_by(TrustFinding.created_at.desc(), TrustFinding.id)
        if offset:
            q = q.offset(offset)
        if limit is not None:
            q = q.limit(limit)
        with tenant_session(tenant_id, self.url) as s:
            return [_finding_dict(f) for f in s.scalars(q)]

    def count(self, tenant_id: str, **filters) -> int:
        q = self._filtered(tenant_id, **filters)
        with tenant_session(tenant_id, self.url) as s:
            return int(
                s.scalar(select(func.count()).select_from(q.subquery())) or 0
            )

    def summary(self, tenant_id: str, *, top_vendors: int = 50) -> dict:
        """Counts by status and type, and the vendors with the most findings."""
        with tenant_session(tenant_id, self.url) as s:
            by_status = dict(
                s.execute(
                    select(TrustFinding.status, func.count())
                    .where(TrustFinding.tenant_id == tenant_id)
                    .group_by(TrustFinding.status)
                ).all()
            )
            by_type = dict(
                s.execute(
                    select(TrustFinding.type, func.count())
                    .where(TrustFinding.tenant_id == tenant_id)
                    .group_by(TrustFinding.type)
                ).all()
            )
            vendors = s.execute(
                select(GraphEdge.dst, func.count())
                .join(
                    TrustFinding,
                    TrustFinding.transaction_node == GraphEdge.src,
                )
                .where(
                    TrustFinding.tenant_id == tenant_id,
                    GraphEdge.tenant_id == tenant_id,
                    GraphEdge.kind == "issued_by",
                    GraphEdge.valid_to.is_(None),
                )
                .group_by(GraphEdge.dst)
                .order_by(func.count().desc(), GraphEdge.dst)
                .limit(top_vendors)
            ).all()
        return {
            "by_status": {k: int(v) for k, v in by_status.items()},
            "by_type": {k: int(v) for k, v in by_type.items()},
            "vendors": [{"node": n, "count": int(c)} for n, c in vendors],
        }

    def events(
        self,
        tenant_id: str,
        *,
        to_status: tuple[str, ...] = (),
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict]:
        """The raw ledger: every status change with the money at that step,
        carrying its finding's type, node and impact."""
        q = (
            select(TrustFindingEvent, TrustFinding)
            .join(TrustFinding, TrustFinding.id == TrustFindingEvent.finding_id)
            .where(TrustFindingEvent.tenant_id == tenant_id)
            .order_by(TrustFindingEvent.created_at, TrustFindingEvent.id)
        )
        if to_status:
            q = q.where(TrustFindingEvent.to_status.in_(to_status))
        if since is not None:
            q = q.where(TrustFindingEvent.created_at >= since)
        if until is not None:
            q = q.where(TrustFindingEvent.created_at < until)
        with tenant_session(tenant_id, self.url) as s:
            return [
                {
                    "finding_id": f.id,
                    "type": f.type,
                    "transaction_node": f.transaction_node,
                    "impact": _money(f.impact),
                    "currency": f.currency,
                    "from_status": e.from_status,
                    "to_status": e.to_status,
                    "amount": _money(e.amount),
                    "actor": e.actor,
                    "reason": e.reason,
                    "at": e.created_at.isoformat() if e.created_at else None,
                }
                for e, f in s.execute(q).all()
            ]
