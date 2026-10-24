"""The KPI page: the operating numbers over one window.

A reader only. Everything it serves was written by the pipeline, the
projection or a person deciding an invoice; nothing here changes an invoice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from .auth import Principal, current_principal

router = APIRouter()

WINDOWS = (0, 7, 30, 90)


def _repository():
    from . import service  # noqa: PLC0415

    if not service._database_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "The KPI page needs a database. This deployment stores cases on "
                "local disk, where invoices cannot be counted."
            ),
        )
    from ..db.repositories import KpiRepository  # noqa: PLC0415

    return KpiRepository()


def _since(days: int) -> datetime | None:
    if days not in WINDOWS:
        raise HTTPException(
            status_code=422,
            detail=f"days must be one of {', '.join(map(str, WINDOWS))}.",
        )
    if days == 0:
        return None
    return datetime.now(UTC) - timedelta(days=days)


@router.get("/api/kpis")
def kpis(
    principal: Annotated[Principal, Depends(current_principal)],
    days: int = 30,
) -> dict:
    since = _since(days)
    repository = _repository()
    return {
        "window": {"days": days, "since": since.isoformat() if since else None},
        **repository.summary(principal.tenant_id, since),
    }


@router.get("/api/kpis/cases")
def kpi_cases(
    principal: Annotated[Principal, Depends(current_principal)],
    reason: str,
    days: int = 30,
) -> dict:
    """The invoices behind one exception bucket, for the drill-down."""
    from ..db.repositories import BUCKETS  # noqa: PLC0415

    if reason not in BUCKETS:
        raise HTTPException(
            status_code=422,
            detail=f"reason must be one of {', '.join(BUCKETS)}.",
        )
    since = _since(days)
    return _repository().cases(principal.tenant_id, since, reason)
