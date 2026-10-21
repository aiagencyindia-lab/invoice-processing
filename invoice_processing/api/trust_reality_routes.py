"""Evidence requests for reality checks (FR-23.5): ask an owner for proof,
attach it, re-check. Scaffold stubs; the v2/w3-reality branch fills them."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import Principal, require_role

router = APIRouter()
NOT_BUILT = "Not built yet."


class EvidenceRequestIn(BaseModel):
    owner: str = Field(max_length=320)
    due_date: str | None = Field(default=None, max_length=10)
    note: str | None = Field(default=None, max_length=500)


@router.post("/api/trust/findings/{finding_id}/evidence-request")
def create_evidence_request(
    finding_id: str,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
    body: EvidenceRequestIn | None = None,
) -> dict:
    raise HTTPException(503, NOT_BUILT)


@router.get("/api/trust/evidence-requests")
def list_evidence_requests(
    principal: Annotated[Principal, Depends(require_role("viewer"))],
    status: str | None = None,
    overdue: bool = False,
) -> dict:
    raise HTTPException(503, NOT_BUILT)


@router.post("/api/trust/evidence-requests/{request_id}/proof")
def fulfil_evidence_request(
    request_id: str,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    raise HTTPException(503, NOT_BUILT)
