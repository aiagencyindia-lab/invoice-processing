"""The hold ledger: when each payment hold was placed and lifted.

`gate.payment_hold` still decides from the open findings (spec §3.1.5); this
records the changes so `hold.placed` / `hold.lifted` exist, posture can count
what was stopped before payment, and export can name the holds an override
covered. Written in two places only: `FindingStore.upsert(hold=True)` from an
enforcing engine's run, and `FindingStore.transition(to="rejected")`;
`reconcile` replays both when an engine enters or leaves enforcing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from ..db.models import TrustFinding, TrustHold
from ..db.session import tenant_session
from .findings import model as finding_model
from .webhooks import outbox

# Every status but this one holds (gate.HELD_UNTIL).
LIFTS = "rejected"


def _now() -> datetime:
    return datetime.now(UTC)


def _data(row: TrustHold, finding: TrustFinding | None) -> dict:
    return {
        "finding_id": row.finding_id,
        "type": row.type,
        "engine": row.engine,
        "transaction_node": row.transaction_node,
        "summary": finding.summary if finding else None,
        "impact": None
        if finding is None or finding.impact is None
        else str(finding.impact),
        "currency": finding.currency if finding else None,
    }


def _open(s, tenant_id: str, finding_id: str) -> TrustHold | None:
    return s.scalars(
        select(TrustHold).where(
            TrustHold.tenant_id == tenant_id,
            TrustHold.finding_id == finding_id,
            TrustHold.lifted_at.is_(None),
        )
    ).first()


def engine_of(finding: TrustFinding) -> str | None:
    return (finding.details or {}).get(
        "engine"
    ) or finding_model.TYPE_ENGINE.get(finding.type)


def place(
    s, ctx, finding: TrustFinding, *, by: str = "evaluate", now=None
) -> bool:
    """Open a hold for `finding` unless one is open. True when placed."""
    if _open(s, ctx.tenant_id, finding.id) is not None:
        return False
    now = now or _now()
    row = TrustHold(
        tenant_id=ctx.tenant_id,
        finding_id=finding.id,
        transaction_node=finding.transaction_node,
        engine=engine_of(finding) or "trust",
        type=finding.type,
        placed_at=now,
        placed_by=by,
    )
    s.add(row)
    outbox.emit(s, ctx.tenant_id, "hold.placed", _data(row, finding), now=now)
    return True


def lift(
    s, ctx, finding_id: str, *, by: str, reason: str | None, now=None
) -> bool:
    """Close the open hold on `finding_id`. True when one was open."""
    row = _open(s, ctx.tenant_id, finding_id)
    if row is None:
        return False
    now = now or _now()
    row.lifted_at, row.lifted_by, row.lift_reason = now, by, reason
    finding = s.get(TrustFinding, finding_id)
    outbox.emit(
        s,
        ctx.tenant_id,
        "hold.lifted",
        {**_data(row, finding), "lifted_by": by, "reason": reason},
        now=now,
    )
    return True


def open_for(
    tenant_id: str, transaction_node: str, *, url: str | None = None
) -> list[dict]:
    with tenant_session(tenant_id, url) as s:
        rows = s.scalars(
            select(TrustHold)
            .where(
                TrustHold.tenant_id == tenant_id,
                TrustHold.transaction_node == transaction_node,
                TrustHold.lifted_at.is_(None),
            )
            .order_by(TrustHold.placed_at)
        ).all()
        return [
            {
                "id": r.id,
                "finding_id": r.finding_id,
                "type": r.type,
                "engine": r.engine,
                "placed_at": r.placed_at.isoformat(),
            }
            for r in rows
        ]


def reconcile(
    ctx, engine: str, *, enforcing: bool, url: str | None = None
) -> dict:
    """Make the ledger match an engine entering (`enforcing=True`) or leaving
    enforcing: every unrejected hold-type finding of the engine gets an open
    hold, or every open hold of the engine is lifted."""
    placed = lifted = 0
    with tenant_session(ctx.tenant_id, url) as s:
        if enforcing:
            # ponytail: reads every unrejected finding of the tenant; filter
            # by type in SQL if a tenant's findings pass ~100k.
            for f in s.scalars(
                select(TrustFinding).where(
                    TrustFinding.tenant_id == ctx.tenant_id,
                    TrustFinding.status != LIFTS,
                )
            ):
                holds = (f.details or {}).get(
                    "hold", f.type in finding_model.HOLD_TYPES
                )
                if holds and engine_of(f) == engine:
                    placed += place(s, ctx, f, by="mode-change")
        else:
            for row in s.scalars(
                select(TrustHold).where(
                    TrustHold.tenant_id == ctx.tenant_id,
                    TrustHold.engine == engine,
                    TrustHold.lifted_at.is_(None),
                )
            ).all():
                lifted += lift(
                    s,
                    ctx,
                    row.finding_id,
                    by="mode-change",
                    reason=f"The {engine} engine left enforcing.",
                )
    return {"placed": placed, "lifted": lifted}
