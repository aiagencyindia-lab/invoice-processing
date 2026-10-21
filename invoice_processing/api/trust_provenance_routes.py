"""Vendor onboarding documents (KYC) and on-demand authenticity scores
(FR-22.3). Scaffold stubs; the v2/w3-provenance branch fills them."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from .auth import Principal, require_role

router = APIRouter()
NOT_BUILT = "Not built yet."


@router.post("/api/vendors/{vendor_id}/documents")
def upload_vendor_document(
    vendor_id: str,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    raise HTTPException(503, NOT_BUILT)


@router.post("/api/trust/documents/{document_id}/authenticity")
def score_document(
    document_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
) -> dict:
    raise HTTPException(503, NOT_BUILT)
