"""Agent control routes (FR-28, PRD §9), built dark. Scaffold stubs: each
answers 503 after auth until the registry branch (v2/w4-registry) builds it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from .auth import Principal, require_role
from .integration_routes import Admin

router = APIRouter()
Approver = Annotated[Principal, Depends(require_role("approver"))]
NOT_BUILT = "Agent control is not built yet."


def _stub():
    raise HTTPException(503, NOT_BUILT)


# Static paths before /{agent_id}, or "ledger" would be read as an id.
@router.get("/api/trust/agents/ledger")
def tenant_ledger(principal: Approver):
    _stub()


@router.post("/api/trust/agents/actions/import")
def import_actions(principal: Admin):
    _stub()


@router.post("/api/trust/agents", status_code=201)
def register_agent(principal: Admin):
    _stub()


@router.get("/api/trust/agents")
def list_agents(principal: Approver):
    _stub()


@router.get("/api/trust/agents/{agent_id}")
def get_agent(agent_id: str, principal: Approver):
    _stub()


@router.post("/api/trust/agents/{agent_id}/revoke")
def revoke_agent(agent_id: str, principal: Admin):
    _stub()


@router.post("/api/trust/agents/{agent_id}/mandates", status_code=201)
def propose_mandate(agent_id: str, principal: Admin):
    _stub()


@router.get("/api/trust/agents/{agent_id}/mandates")
def list_mandates(agent_id: str, principal: Approver):
    _stub()


@router.post("/api/trust/agents/{agent_id}/mandates/{mandate_id}/approve")
def approve_mandate(agent_id: str, mandate_id: str, principal: Admin):
    _stub()


@router.post("/api/trust/agents/{agent_id}/mandates/{mandate_id}/revoke")
def revoke_mandate(agent_id: str, mandate_id: str, principal: Admin):
    _stub()


@router.post("/api/trust/agents/{agent_id}/actions/evaluate")
def evaluate_action(agent_id: str, principal: Approver):
    _stub()


@router.get("/api/trust/agents/{agent_id}/ledger")
def get_ledger(agent_id: str, principal: Approver):
    _stub()
