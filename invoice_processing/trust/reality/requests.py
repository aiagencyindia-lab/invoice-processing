"""Evidence requests (FR-23.5, U4): an internal tracker. A finding's owner
is asked for proof by a date; the proof is attached as a document and the
invoice re-checked. No public link and no email: nothing leaves the tenant.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select, update

from ...db.models import TrustEvidenceRequest, TrustFinding
from ...db.session import tenant_session
from ...ports import RunContext

STATUSES = ("open", "fulfilled", "cancelled")


class RequestClosed(ValueError):
    """The request is no longer open."""


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _row(r: TrustEvidenceRequest, today: date) -> dict:
    return {
        "id": r.id,
        "finding_id": r.finding_id,
        "owner": r.owner,
        "due_date": _iso(r.due_date),
        "note": r.note,
        "status": r.status,
        "proof_document_id": r.proof_document_id,
        "requested_by": r.requested_by,
        "fulfilled_at": _iso(r.fulfilled_at),
        "created_at": _iso(r.created_at),
        # Computed on read: a due date passes without anyone writing a row.
        "overdue": r.status == "open"
        and r.due_date is not None
        and r.due_date < today,
    }


class RequestStore:
    def __init__(self, url: str | None = None):
        self.url = url

    def create(
        self,
        ctx: RunContext,
        finding_id: str,
        *,
        owner: str,
        due_date: date | None,
        note: str | None,
        requested_by: str | None,
    ) -> dict:
        with tenant_session(ctx.tenant_id, self.url) as s:
            row = TrustEvidenceRequest(
                tenant_id=ctx.tenant_id,
                finding_id=finding_id,
                owner=owner,
                due_date=due_date,
                note=note,
                status="open",
                requested_by=requested_by,
            )
            s.add(row)
            s.flush()
            return _row(row, _today())

    def get(self, tenant_id: str, request_id: str) -> dict | None:
        with tenant_session(tenant_id, self.url) as s:
            row = s.scalars(
                select(TrustEvidenceRequest).where(
                    TrustEvidenceRequest.tenant_id == tenant_id,
                    TrustEvidenceRequest.id == request_id,
                )
            ).first()
            return _row(row, _today()) if row else None

    def list(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        overdue: bool = False,
        today: date | None = None,
    ) -> list[dict]:
        today = today or _today()
        with tenant_session(tenant_id, self.url) as s:
            q = select(TrustEvidenceRequest).where(
                TrustEvidenceRequest.tenant_id == tenant_id
            )
            if status:
                q = q.where(TrustEvidenceRequest.status == status)
            if overdue:
                q = q.where(
                    TrustEvidenceRequest.status == "open",
                    TrustEvidenceRequest.due_date < today,
                )
            q = q.order_by(
                TrustEvidenceRequest.created_at.desc(), TrustEvidenceRequest.id
            )
            return [_row(r, today) for r in s.scalars(q)]

    def _close(self, ctx: RunContext, request_id: str, **values) -> dict:
        """open -> fulfilled | cancelled, once: a conditional update, so two
        clerks closing the same request cannot both succeed."""
        with tenant_session(ctx.tenant_id, self.url) as s:
            changed = s.execute(
                update(TrustEvidenceRequest)
                .where(
                    TrustEvidenceRequest.tenant_id == ctx.tenant_id,
                    TrustEvidenceRequest.id == request_id,
                    TrustEvidenceRequest.status == "open",
                )
                .values(**values)
            ).rowcount
        if changed != 1:
            if self.get(ctx.tenant_id, request_id) is None:
                raise KeyError(request_id)
            raise RequestClosed("The evidence request is no longer open.")
        return self.get(ctx.tenant_id, request_id)

    def fulfil(
        self,
        ctx: RunContext,
        request_id: str,
        document_id: str,
        *,
        at: datetime | None = None,
    ) -> dict:
        return self._close(
            ctx,
            request_id,
            status="fulfilled",
            proof_document_id=document_id,
            fulfilled_at=at or datetime.now(timezone.utc),
        )

    def cancel(self, ctx: RunContext, request_id: str) -> dict:
        return self._close(ctx, request_id, status="cancelled")

    def proofs(self, tenant_id: str) -> dict[str, list[str]]:
        """Invoice node -> proof document ids of its fulfilled requests."""
        with tenant_session(tenant_id, self.url) as s:
            rows = s.execute(
                select(
                    TrustFinding.transaction_node,
                    TrustEvidenceRequest.proof_document_id,
                )
                .join(
                    TrustFinding,
                    TrustFinding.id == TrustEvidenceRequest.finding_id,
                )
                .where(
                    TrustEvidenceRequest.tenant_id == tenant_id,
                    TrustFinding.tenant_id == tenant_id,
                    TrustEvidenceRequest.status == "fulfilled",
                    TrustEvidenceRequest.proof_document_id.is_not(None),
                )
                .order_by(TrustEvidenceRequest.fulfilled_at)
            ).all()
        out: dict[str, list[str]] = {}
        for node, doc in rows:
            out.setdefault(node, []).append(doc)
        return out
