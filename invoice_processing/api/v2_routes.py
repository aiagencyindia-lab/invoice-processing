"""The public /v2 API (PRD §9): a thin façade over the /api handlers, for
API-key callers and signed-in users alike.

Each route calls the existing handler and returns its answer. Calling a
handler directly skips its own `Depends`, so each route here declares the
same `require_role` the handler does. A transaction id is the invoice's
graph node id, as `/v2/findings` rows carry it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..trust.findings.model import REASON_REQUIRED
from . import (
    trust_agent_routes,
    trust_audit_routes,
    trust_explain_routes,
    trust_provenance_routes,
    trust_routes,
    trust_score_routes,
)
from .auth import Principal, require_role
from .integration_routes import Admin

router = APIRouter(prefix="/v2")

Viewer = Annotated[Principal, Depends(require_role("viewer"))]
Clerk = Annotated[Principal, Depends(require_role("clerk"))]
Approver = Annotated[Principal, Depends(require_role("approver"))]

DECISIONS = {
    "accept": "accepted",
    "reject": "rejected",
    "needs_vendor_input": "needs_vendor_input",
}


# -- transactions -------------------------------------------------------------


@router.get("/transactions/{node_id}/trust")
def transaction_trust(
    node_id: str, principal: Viewer, as_of: str | None = None
):
    return trust_score_routes.transaction_trust(
        node_id=node_id, principal=principal, as_of=as_of
    )


@router.get("/transactions/{node_id}/explanation")
def transaction_explanation(node_id: str, principal: Viewer):
    """The transaction and, for each of its open findings, the cited
    explanation `/api/trust/findings/{id}/explanation` gives."""
    store, graph = trust_routes._findings(), trust_routes._graph()
    tenant = principal.tenant_id
    node = graph.node(tenant, node_id)
    if node is None:
        raise HTTPException(404, "Unknown node.")
    rows = store.list(
        tenant,
        transaction_node=node_id,
        statuses=sorted(trust_routes.OPEN_STATUSES),
        limit=trust_routes.MAX_PAGE,
    )
    vendors: dict = {}
    return {
        "transaction": node,
        "findings": [
            {
                "finding": trust_routes._row(graph, tenant, f, vendors),
                "explanation": trust_explain_routes.get_explanation(
                    finding_id=f["id"], principal=principal
                ),
            }
            for f in rows
        ],
    }


@router.get("/transactions/{node_id}/lineage")
def transaction_lineage(
    node_id: str, principal: Viewer, as_of: str | None = None
):
    return trust_routes.get_lineage(
        node_id=node_id, principal=principal, as_of=as_of
    )


# -- findings -----------------------------------------------------------------


@router.get("/findings")
def list_findings(
    principal: Viewer,
    status: str | None = None,
    open: bool = False,
    type: str | None = None,
    engine: str | None = None,
    vendor: str | None = None,
    hold: bool = False,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    return trust_routes.list_findings(
        principal=principal,
        status=status,
        open=open,
        type=type,
        engine=engine,
        vendor=vendor,
        hold=hold,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )


class Decision(BaseModel):
    decision: Literal["accept", "reject", "needs_vendor_input"]
    reason: str | None = Field(default=None, max_length=500)


@router.post("/findings/{finding_id}/decision")
def decide_finding(finding_id: str, body: Decision, principal: Clerk):
    """One call for a decision: a `detected` finding is triaged first, with
    the same reason, after the caller's role was checked for the decision
    itself, so a refusal never leaves the finding half-moved."""
    to_status = DECISIONS[body.decision]
    if not principal.can(trust_routes._role_for(to_status)):
        raise HTTPException(
            403, f"Marking a finding {to_status} needs an approver."
        )
    if to_status in REASON_REQUIRED and not (body.reason or "").strip():
        raise HTTPException(
            400, f"A reason is required to mark a finding {to_status}."
        )
    current = trust_routes._findings().get(principal.tenant_id, finding_id)
    if current is None:
        raise HTTPException(404, "Unknown finding.")
    if current["status"] == "detected":
        trust_routes.transition_finding(
            finding_id=finding_id,
            request=trust_routes.TransitionRequest(
                to_status="triaged", reason=body.reason, expect="detected"
            ),
            principal=principal,
        )
    return trust_routes.transition_finding(
        finding_id=finding_id,
        request=trust_routes.TransitionRequest(
            to_status=to_status, reason=body.reason
        ),
        principal=principal,
    )


# -- vendors, documents, evidence ---------------------------------------------


@router.get("/vendors/{node_id}/trust")
def vendor_trust(node_id: str, principal: Viewer):
    return trust_score_routes.vendor_trust(node_id=node_id, principal=principal)


@router.post("/documents/{document_id}/authenticity")
def document_authenticity(document_id: str, principal: Viewer):
    return trust_provenance_routes.score_document(
        document_id=document_id, principal=principal
    )


@router.get("/evidence/{node_id}")
def evidence(node_id: str, principal: Viewer):
    return trust_routes.get_node(node_id=node_id, principal=principal)


# -- trust audits -------------------------------------------------------------


@router.post("/trust-audits", status_code=201)
def create_trust_audit(
    body: trust_audit_routes.AuditCreate, principal: Approver
):
    return trust_audit_routes.create_audit(body=body, principal=principal)


@router.post("/trust-audits/{audit_id}/runs", status_code=202)
def request_trust_audit_run(
    audit_id: str, body: trust_audit_routes.RunRequest, principal: Approver
):
    return trust_audit_routes.request_run(
        audit_id=audit_id, body=body, principal=principal
    )


@router.get("/trust-audits/{audit_id}/report")
def trust_audit_report(audit_id: str, principal: Viewer, format: str = "json"):
    return trust_audit_routes.get_report(
        audit_id=audit_id, principal=principal, format=format
    )


# -- agents -------------------------------------------------------------------
# Registering an agent and proposing its mandate are a human admin's acts (an
# API key gets 403); approving a mandate stays on /api, a second person's.


@router.post("/agents", status_code=201)
def register_agent(body: trust_agent_routes.AgentCreate, principal: Admin):
    return trust_agent_routes.register_agent(body=body, principal=principal)


@router.post("/agents/{agent_id}/mandates", status_code=201)
def propose_mandate(
    agent_id: str, body: trust_agent_routes.MandateCreate, principal: Admin
):
    return trust_agent_routes.propose_mandate(
        agent_id=agent_id, body=body, principal=principal
    )


@router.post("/agents/{agent_id}/actions/evaluate")
def evaluate_action(
    agent_id: str, body: trust_agent_routes.Evaluate, principal: Viewer
):
    """An approver, or the agent's own principal about itself."""
    return trust_agent_routes.evaluate_action(
        agent_id=agent_id, body=body, principal=principal
    )


@router.get("/agents/{agent_id}/ledger")
def agent_ledger(
    agent_id: str,
    principal: Approver,
    since: datetime | None = None,
    until: datetime | None = None,
):
    return trust_agent_routes.get_ledger(
        agent_id=agent_id, principal=principal, since=since, until=until
    )
