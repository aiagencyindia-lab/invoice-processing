"""The only data access for agent control: agents, mandate versions and the
decision ledger (`agent_actions`)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select

from ...db.models import Agent, AgentAction, AgentMandate
from ...db.repositories import SelfApproval
from ...db.session import tenant_session

__all__ = [
    "AgentStore",
    "MandateConflict",
    "SelfApproval",
    "canonical",
    "digest",
]

# The fields a mandate's digest covers: what was authorised, for whom, when,
# by whom, and the version before it.
SIGNED = (
    "tenant_id",
    "agent_id",
    "version",
    "scope",
    "valid_from",
    "valid_to",
    "proposed_by",
    "approved_by",
    "approved_at",
    "prev_digest",
)


OUTCOMES = ("executed", "failed")


class MandateConflict(Exception):
    """A version is already pending, or this one is not the pending latest."""


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value):
    # SQLite hands back naive datetimes.
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _iso(value):
    if isinstance(value, datetime):
        return _utc(value).astimezone(UTC).isoformat()
    if isinstance(value, (date, Decimal)):
        return str(value)
    raise TypeError(type(value).__name__)


def canonical(row: dict) -> bytes:
    return json.dumps(
        {k: row.get(k) for k in SIGNED},
        sort_keys=True,
        separators=(",", ":"),
        default=_iso,
    ).encode()


def digest(row: dict) -> str:
    return hashlib.sha256(canonical(row)).hexdigest()


def _dict(row) -> dict:
    return {
        c.name: _utc(getattr(row, c.key))
        for c in row.__table__.columns
        if c.name not in ("created_at", "updated_at")
    }


def _lock_query(tenant_id: str, agent_id: str):
    # The guard records its decision in the session holding this lock, so
    # the ledger insert's FK check never waits on another connection.
    return (
        select(Agent.id)
        .where(Agent.tenant_id == tenant_id, Agent.id == agent_id)
        .with_for_update()
    )


def _fold(email: str | None) -> str:
    return (email or "").strip().casefold()


class AgentStore:
    def __init__(self, url: str | None = None, graph=None):
        self.url = url
        self.graph = graph

    # -- agents ------------------------------------------------------------

    def _node(self, ctx, row: Agent) -> str | None:
        if self.graph is None:
            return row.graph_node
        return self.graph.upsert_node(
            ctx,
            kind="agent",
            natural_key=f"agents:{row.id}",
            ref_table="agents",
            ref_id=row.id,
            attrs={
                "name": row.name,
                "owner": row.owner,
                "kind": row.kind,
                "status": row.status,
            },
        )

    def register(
        self,
        ctx,
        *,
        name: str,
        owner: str,
        kind: str,
        principal_id: str | None = None,
        principal_email: str | None = None,
        description: str | None = None,
    ) -> dict:
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = Agent(
                tenant_id=ctx.tenant_id,
                name=name,
                owner=_fold(owner),
                kind=kind,
                principal_id=principal_id,
                principal_email=_fold(principal_email) or None,
                description=description,
                status="active",
                registered_by=_fold(ctx.actor),
            )
            s.add(row)
            s.flush()
            agent_id = row.id
        return self._renode(ctx, agent_id)

    def _renode(self, ctx, agent_id: str) -> dict:
        # The graph store opens its own session: write the node after the
        # row has committed, then remember where it is.
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = s.get(Agent, agent_id)
            node = self._node(ctx, row)
            row.graph_node = node
            return _dict(row)

    def _agent(self, s, tenant_id, agent_id) -> Agent | None:
        row = s.get(Agent, agent_id)
        return row if row is not None and row.tenant_id == tenant_id else None

    def get(self, tenant_id: str, agent_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = self._agent(s, tenant_id, agent_id)
            return _dict(row) if row else None

    def by_principal(self, tenant_id: str, principal_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = s.scalars(
                select(Agent).where(
                    Agent.tenant_id == tenant_id,
                    Agent.principal_id == principal_id,
                )
            ).first()
            return _dict(row) if row else None

    def by_emails(self, tenant_id: str, emails) -> dict[str, dict]:
        wanted = {_fold(e) for e in emails if e}
        if not wanted:
            return {}
        with tenant_session(tenant_id, self.url) as s:
            rows = s.scalars(
                select(Agent).where(
                    Agent.tenant_id == tenant_id,
                    Agent.principal_email.in_(wanted),
                )
            ).all()
            return {r.principal_email: _dict(r) for r in rows}

    def by_owner(self, tenant_id: str, owner: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as s:
            rows = s.scalars(
                select(Agent)
                .where(
                    Agent.tenant_id == tenant_id, Agent.owner == _fold(owner)
                )
                .order_by(Agent.created_at, Agent.id)
            ).all()
            return [_dict(r) for r in rows]

    def list(self, tenant_id: str, status: str | None = None) -> list[dict]:
        with tenant_session(tenant_id, self.url) as s:
            q = select(Agent).where(Agent.tenant_id == tenant_id)
            if status:
                q = q.where(Agent.status == status)
            rows = s.scalars(q.order_by(Agent.name, Agent.id)).all()
            return [_dict(r) for r in rows]

    def revoke(self, ctx, agent_id: str, reason: str) -> dict | None:
        now = _now()
        closed: list[str] = []
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = self._agent(s, ctx.tenant_id, agent_id)
            if row is None:
                return None
            row.status = "revoked"
            row.revoked_at = now
            row.revoked_by = ctx.actor
            row.revoke_reason = reason
            for m in s.scalars(
                select(AgentMandate).where(
                    AgentMandate.tenant_id == ctx.tenant_id,
                    AgentMandate.agent_id == agent_id,
                    AgentMandate.status.in_(("active", "proposed")),
                )
            ):
                self._revoke(m, ctx, "agent revoked", now)
                closed.append(m.id)
        # The caller reseals exactly these rows (`mandates.reseal`).
        return {**self._renode(ctx, agent_id), "revoked_mandates": closed}

    # -- mandates ----------------------------------------------------------

    @staticmethod
    def _revoke(m: AgentMandate, ctx, reason: str, now) -> None:
        m.status = "revoked"
        m.revoked_by = ctx.actor
        m.revoked_at = now
        m.reason = reason

    def _latest(self, s, tenant_id, agent_id) -> AgentMandate | None:
        return s.scalars(
            select(AgentMandate)
            .where(
                AgentMandate.tenant_id == tenant_id,
                AgentMandate.agent_id == agent_id,
            )
            .order_by(AgentMandate.version.desc())
        ).first()

    def propose_mandate(
        self,
        ctx,
        agent_id: str,
        *,
        scope: dict,
        valid_from: datetime,
        valid_to: datetime,
        reason: str | None = None,
    ) -> dict:
        with tenant_session(ctx.tenant_id, self.url) as s:
            if self._agent(s, ctx.tenant_id, agent_id) is None:
                raise LookupError("Unknown agent.")
            latest = self._latest(s, ctx.tenant_id, agent_id)
            if latest is not None and latest.status == "proposed":
                raise MandateConflict("A mandate is already awaiting approval.")
            row = AgentMandate(
                tenant_id=ctx.tenant_id,
                agent_id=agent_id,
                version=(latest.version + 1) if latest else 1,
                scope=scope,
                # Stored in UTC: SQLite keeps wall-clock time without the
                # zone, and the digest must survive the round trip.
                valid_from=valid_from.astimezone(UTC),
                valid_to=valid_to.astimezone(UTC),
                status="proposed",
                proposed_by=_fold(ctx.actor),
                reason=reason,
                prev_digest=latest.digest if latest else None,
            )
            s.add(row)
            s.flush()
            row.digest = digest(_dict(row))
            return _dict(row)

    def approve_mandate(
        self, ctx, mandate_id: str, *, signature: str | None
    ) -> dict:
        now = _now()
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = s.get(AgentMandate, mandate_id)
            if row is None or row.tenant_id != ctx.tenant_id:
                raise LookupError("Unknown mandate.")
            latest = self._latest(s, ctx.tenant_id, row.agent_id)
            if row.status != "proposed" or latest.id != row.id:
                raise MandateConflict(
                    "Only the pending latest version can be approved."
                )
            if _fold(ctx.actor) == _fold(row.proposed_by):
                raise SelfApproval(
                    "A mandate needs a second person to approve it."
                )
            superseded: list[str] = []
            for prior in s.scalars(
                select(AgentMandate).where(
                    AgentMandate.tenant_id == ctx.tenant_id,
                    AgentMandate.agent_id == row.agent_id,
                    AgentMandate.status == "active",
                )
            ):
                self._revoke(prior, ctx, "superseded", now)
                superseded.append(prior.id)
            row.status = "active"
            row.approved_by = _fold(ctx.actor)
            row.approved_at = now
            s.flush()
            row.digest = digest(_dict(row))
            row.signature = signature
            # The caller reseals these too (`mandates.reseal`).
            return {**_dict(row), "superseded": superseded}

    def revoke_mandate(self, ctx, mandate_id: str, reason: str) -> dict | None:
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = s.get(AgentMandate, mandate_id)
            if row is None or row.tenant_id != ctx.tenant_id:
                return None
            changed = row.status != "revoked"
            if changed:
                self._revoke(row, ctx, reason, _now())
            # The caller reseals only a row this call changed.
            return {**_dict(row), "changed": changed}

    def mandates(self, tenant_id: str, agent_id: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as s:
            rows = s.scalars(
                select(AgentMandate)
                .where(
                    AgentMandate.tenant_id == tenant_id,
                    AgentMandate.agent_id == agent_id,
                )
                .order_by(AgentMandate.version)
            ).all()
            return [_dict(r) for r in rows]

    def current_mandate(
        self, tenant_id: str, agent_id: str, now: datetime
    ) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = s.scalars(
                select(AgentMandate).where(
                    AgentMandate.tenant_id == tenant_id,
                    AgentMandate.agent_id == agent_id,
                    AgentMandate.status == "active",
                )
            ).first()
            if row is None:
                return None
            m = _dict(row)
        return m if m["valid_from"] <= now < m["valid_to"] else None

    # -- the decision ledger -----------------------------------------------

    def lock_agent(self, s, tenant_id: str, agent_id: str) -> None:
        """Serialise one agent's check-then-record (caps are hard limits)."""
        s.execute(_lock_query(tenant_id, agent_id))

    def set_outcome(self, tenant_id: str, action_id: str, outcome: str) -> None:
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}.")
        with tenant_session(tenant_id, self.url) as s:
            row = s.get(AgentAction, action_id)
            if row is not None and row.tenant_id == tenant_id:
                row.outcome = outcome

    def action(self, tenant_id: str, action_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = s.get(AgentAction, action_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return _dict(row)

    def record_action(self, s, ctx, **cols) -> str:
        row = AgentAction(tenant_id=ctx.tenant_id, **cols)
        s.add(row)
        s.flush()
        return row.id

    def actions(
        self,
        tenant_id: str,
        *,
        agent_id: str | None = None,
        vendor_key: str | None = None,
        action: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        decisions=None,
        sources=None,
        limit: int = 500,
    ) -> list[dict]:
        q = select(AgentAction).where(AgentAction.tenant_id == tenant_id)
        if agent_id:
            q = q.where(AgentAction.agent_id == agent_id)
        if vendor_key:
            q = q.where(AgentAction.vendor_key == vendor_key)
        if action:
            q = q.where(AgentAction.action == action)
        if since:
            q = q.where(AgentAction.occurred_at >= since)
        if until:
            q = q.where(AgentAction.occurred_at < until)
        if decisions:
            q = q.where(AgentAction.decision.in_(decisions))
        if sources:
            q = q.where(AgentAction.source.in_(sources))
        q = q.order_by(
            AgentAction.occurred_at.desc(), AgentAction.created_at.desc()
        ).limit(limit)
        with tenant_session(tenant_id, self.url) as s:
            return [_dict(r) for r in s.scalars(q)]

    def _counted(self, tenant_id):
        # A dry run is a question: it never counts toward anything.
        return (
            AgentAction.tenant_id == tenant_id,
            AgentAction.source != "dryrun",
        )

    def spent(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        since: datetime,
        until: datetime,
        currency: str,
        vendor_key: str | None = None,
        category: str | None = None,
    ) -> Decimal:
        """What the agent approved in the window: a blocked approval spent
        nothing, an export is the same money paid, not more."""
        q = select(func.coalesce(func.sum(AgentAction.amount), 0)).where(
            *self._counted(tenant_id),
            AgentAction.agent_id == agent_id,
            AgentAction.action == "invoice.approve",
            AgentAction.decision != "BLOCK",
            # A failed write spent nothing; unstamped still counts.
            AgentAction.outcome.is_distinct_from("failed"),
            AgentAction.currency == currency,
            AgentAction.occurred_at >= since,
            AgentAction.occurred_at < until,
        )
        if vendor_key is not None:
            q = q.where(AgentAction.vendor_key == vendor_key)
        if category is not None:
            q = q.where(AgentAction.category == category)
        with tenant_session(tenant_id, self.url) as s:
            return Decimal(str(s.scalar(q)))

    def count_since(
        self, tenant_id: str, agent_id: str, *, since: datetime, until: datetime
    ) -> int:
        """Requests in the window: the rows of one bulk request (a vendor
        load, a PO import) share its request id and count once."""
        burst = func.coalesce(AgentAction.request_id, AgentAction.id)
        q = select(func.count(func.distinct(burst))).where(
            *self._counted(tenant_id),
            AgentAction.agent_id == agent_id,
            AgentAction.occurred_at >= since,
            AgentAction.occurred_at < until,
        )
        with tenant_session(tenant_id, self.url) as s:
            return int(s.scalar(q))

    def creators_of(self, tenant_id: str, vendor_key: str) -> list[dict]:
        """The agents that created this vendor (an allowed or reviewed
        `vendor.create`)."""
        with tenant_session(tenant_id, self.url) as s:
            ids = s.scalars(
                select(AgentAction.agent_id)
                .where(
                    *self._counted(tenant_id),
                    AgentAction.action == "vendor.create",
                    AgentAction.vendor_key == vendor_key,
                    AgentAction.decision != "BLOCK",
                )
                .distinct()
            ).all()
            rows = (
                s.scalars(
                    select(Agent).where(
                        Agent.tenant_id == tenant_id, Agent.id.in_(ids)
                    )
                ).all()
                if ids
                else []
            )
            return [_dict(r) for r in sorted(rows, key=lambda r: r.id)]
