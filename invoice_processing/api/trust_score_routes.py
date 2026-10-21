"""Trust Score, vendor trust profile, posture and engine readiness (FR-25).

Scaffold stubs with the names the /v2 façade and the frontend build against;
the v2/w3-score branch fills them.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import Principal, require_role

router = APIRouter()
NOT_BUILT = "Not built yet."


class ModeChange(BaseModel):
    mode: str = Field(max_length=16)
    reason: str = Field(default="", max_length=500)


@router.get("/api/trust/transactions/{node_id}/trust")
def transaction_trust(
    node_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
    as_of: str | None = None,
) -> dict:
    raise HTTPException(503, NOT_BUILT)


@router.get("/api/trust/vendors/{node_id}/trust")
def vendor_trust(
    node_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
) -> dict:
    raise HTTPException(503, NOT_BUILT)


@router.get("/api/trust/posture")
def posture(
    principal: Annotated[Principal, Depends(require_role("viewer"))],
    since: str | None = None,
) -> dict:
    raise HTTPException(503, NOT_BUILT)


@router.get("/api/trust/readiness")
def readiness(
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    raise HTTPException(503, NOT_BUILT)


@router.post("/api/trust/engines/{engine}/mode")
def set_mode(
    engine: str,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    body: ModeChange | None = None,
) -> dict:
    raise HTTPException(503, NOT_BUILT)
