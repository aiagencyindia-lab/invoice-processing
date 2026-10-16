"""Reviewer verdicts on explanations (FR-24.5): one row per rater and
finding, upserted, each write audited like a finding transition."""

from __future__ import annotations

from sqlalchemy import case, func, select

from ...db.models import TrustExplanationRating, TrustFinding
from ...db.repositories import audit_event_row
from ...db.session import tenant_session
from ...ports import RunContext


def _dict(r: TrustExplanationRating) -> dict:
    return {
        "rater": r.rater,
        "correct": bool(r.correct),
        "useful": bool(r.useful),
        "note": r.note,
        "at": r.updated_at.isoformat() if r.updated_at else None,
    }


class RatingStore:
    def __init__(self, url: str | None = None):
        self.url = url

    def rate(
        self,
        ctx: RunContext,
        finding_id: str,
        *,
        correct: bool,
        useful: bool,
        note: str | None = None,
    ) -> dict:
        rater = ctx.actor or "system"
        with tenant_session(ctx.tenant_id, self.url) as s:
            owned = s.scalars(
                select(TrustFinding.id).where(
                    TrustFinding.tenant_id == ctx.tenant_id,
                    TrustFinding.id == finding_id,
                )
            ).first()
            if owned is None:
                raise KeyError(finding_id)
            row = s.scalars(
                select(TrustExplanationRating).where(
                    TrustExplanationRating.tenant_id == ctx.tenant_id,
                    TrustExplanationRating.finding_id == finding_id,
                    TrustExplanationRating.rater == rater,
                )
            ).first()
            if row is None:
                row = TrustExplanationRating(
                    tenant_id=ctx.tenant_id, finding_id=finding_id, rater=rater
                )
                s.add(row)
            row.correct, row.useful, row.note = (
                bool(correct),
                bool(useful),
                note,
            )
            s.add(
                audit_event_row(
                    ctx.child(case_id=finding_id),
                    "trust_explanation_rated",
                    {
                        "finding_id": finding_id,
                        "correct": bool(correct),
                        "useful": bool(useful),
                        "note": note,
                    },
                )
            )
            s.flush()
            s.refresh(row)
            return _dict(row)

    def list(self, tenant_id: str, finding_id: str) -> list[dict]:
        with tenant_session(tenant_id, self.url) as s:
            rows = s.scalars(
                select(TrustExplanationRating)
                .where(
                    TrustExplanationRating.tenant_id == tenant_id,
                    TrustExplanationRating.finding_id == finding_id,
                )
                .order_by(TrustExplanationRating.rater)
            )
            return [_dict(r) for r in rows]

    def aggregate(self, tenant_id: str) -> dict[str, dict]:
        """`{type: {rated, correct, useful}}` over this tenant's ratings."""
        q = (
            select(
                TrustFinding.type,
                func.count(),
                func.sum(case((TrustExplanationRating.correct, 1), else_=0)),
                func.sum(case((TrustExplanationRating.useful, 1), else_=0)),
            )
            .join(
                TrustFinding,
                TrustFinding.id == TrustExplanationRating.finding_id,
            )
            .where(
                TrustExplanationRating.tenant_id == tenant_id,
                TrustFinding.tenant_id == tenant_id,
            )
            .group_by(TrustFinding.type)
        )
        with tenant_session(tenant_id, self.url) as s:
            return {
                t: {
                    "rated": int(n),
                    "correct": int(c or 0),
                    "useful": int(u or 0),
                }
                for t, n, c, u in s.execute(q).all()
            }
