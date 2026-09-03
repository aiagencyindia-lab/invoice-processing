"""Proposed rules, and the one place a person approves one.

This is what replaces the chat: teaching the system used to mean running an
ADK agent on a laptop, and the rules it wrote went to a file nothing read. The
loop is now: a reviewer overrules the pipeline and says it was policy, that
becomes a proposal, someone drafts a rule from it, and someone approves it --
at which point it starts correcting decisions, in shadow until a tenant arms
them.

The approval is the whole point of the screen. A rule changes what gets paid,
so it has to be readable before it is live: the conditions it fires on, and
every other case it would touch.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from .auth import Principal, current_principal, require_role

logger = logging.getLogger(__name__)
router = APIRouter()

# What a proposal can be, in the order it moves through.
PENDING = "pending_draft"  # a case worth a rule; nothing written yet
DRAFTING = "drafting"  # the model is working on it
DRAFTED = "draft"  # a rule exists, waiting for a person
APPROVED = "approved"  # live, and correcting decisions
REJECTED = "rejected"  # considered and declined


def _rules():
    from . import service  # noqa: PLC0415

    rules = service.get_rules()
    if not hasattr(rules, "propose"):
        # The file-backed store has no notion of a proposal. Rather than
        # pretend, say so: this is a database feature.
        raise HTTPException(
            status_code=503,
            detail="Proposed rules need a database. None is configured.",
        )
    return rules


@router.get("/api/rules/proposals")
def list_proposals(
    principal: Annotated[Principal, Depends(require_role("approver"))],
    status: str | None = None,
) -> dict:
    """Everything waiting for a decision, newest first."""
    return {"proposals": _rules().proposals(principal.tenant_id, status)}


@router.get("/api/rules/proposals/{rule_id}")
def get_proposal(
    rule_id: str,
    principal: Annotated[Principal, Depends(require_role("approver"))],
) -> dict:
    proposal = _rules().proposal(principal.tenant_id, rule_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Unknown proposal.")
    return proposal


@router.post("/api/rules/proposals/{rule_id}/draft")
def draft_proposal(
    rule_id: str,
    background: BackgroundTasks,
    principal: Annotated[Principal, Depends(require_role("approver"))],
) -> dict:
    """Write a rule for this proposal, in the background.

    Several Vertex calls, and Vertex has been seen hanging for twenty minutes,
    so it cannot happen inside the request. The proposal row survives an API
    restart that loses the job -- recovery is pressing the button again, not a
    lost rejection.
    """
    rules = _rules()
    proposal = rules.proposal(principal.tenant_id, rule_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Unknown proposal.")
    if proposal["status"] not in (PENDING, "failed", DRAFTED):
        raise HTTPException(
            status_code=409,
            detail=f"That proposal is {proposal['status']}.",
        )

    rules.update_proposal(principal.tenant_id, rule_id, status=DRAFTING)
    background.add_task(draft, principal.tenant_id, rule_id)
    return {"rule_id": rule_id, "status": DRAFTING}


def draft(tenant_id: str, rule_id: str) -> dict:
    """Run the safety loop against this tenant's own corpus.

    Generate, validate the field paths, measure what else the rule would
    match, and tighten it if it matches cases it should not -- the loop that
    already exists, pointed at a tenant's cases instead of a folder on disk.
    """
    from ..core.safe_rule_orchestrator import (  # noqa: PLC0415
        SafeRuleOrchestrator,
    )
    from ..tenant_cases import TenantCaseLoader  # noqa: PLC0415
    from . import service  # noqa: PLC0415

    rules = service.get_rules()
    proposal = rules.proposal(tenant_id, rule_id) or {}
    case_id = proposal.get("source_case_id")
    try:
        loader = TenantCaseLoader(service.get_cases(), tenant_id)
        result = SafeRuleOrchestrator(loader=loader).discover(
            case_id, _feedback(proposal)
        )
    except Exception as exc:
        logger.exception("Drafting %s failed", rule_id)
        return rules.update_proposal(
            tenant_id,
            rule_id,
            status="failed",
            metadata={"error": str(exc)[:2000]},
        )

    if not result.get("success"):
        return rules.update_proposal(
            tenant_id,
            rule_id,
            status="failed",
            metadata={"error": result.get("error", "The rule writer failed.")},
        )

    rule = result["rule"]
    return rules.update_proposal(
        tenant_id,
        rule_id,
        status=DRAFTED,
        name=rule.get("name") or proposal.get("name"),
        description=rule.get("description"),
        scope=rule.get("scope") or "global",
        priority=rule.get("priority") or 100,
        conditions=rule.get("conditions") or [],
        actions=rule.get("actions") or [],
        metadata={
            "impact": result.get("impact"),
            # Carried separately because the orchestrator reports success even
            # when the rule still matches cases nobody meant it to.
            "has_collateral": bool(result.get("has_collateral")),
            "collateral_warning": result.get("collateral_warning"),
            "error": None,
        },
    )


def _feedback(proposal: dict) -> str:
    """What the reviewer said, in the form the rule writer reads.

    The direction matters as much as the words: a rule written for an invoice
    a person approved has to loosen a check, and one written for an invoice
    they rejected has to tighten it. Saying so in the prompt is the difference
    between the two.
    """
    note = proposal.get("note") or "No note was left."
    return (
        f"A reviewer overruled the pipeline on this invoice and recorded it "
        f"as a policy exception. They wrote: {note!r}. "
        "Write a rule that covers this policy and nothing else."
    )


@router.post("/api/rules/proposals/{rule_id}/decision")
def decide_proposal(
    body: dict,
    rule_id: str,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    """Approve or reject a drafted rule. Approving makes it live.

    An approver may approve a rule their own rejection produced. A pilot has
    one admin, and a rule nobody can approve is a feature nobody has -- but
    the trail names author and approver either way, so a tenant that later
    wants two people can see whether it ever had them.
    """
    from . import service  # noqa: PLC0415

    decision = str(body.get("decision") or "").lower()
    if decision not in ("approve", "reject"):
        raise HTTPException(
            status_code=400, detail="decision must be approve or reject"
        )

    rules = _rules()
    proposal = rules.proposal(principal.tenant_id, rule_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Unknown proposal.")
    if proposal["status"] != DRAFTED:
        raise HTTPException(
            status_code=409,
            detail=f"That proposal is {proposal['status']}, not drafted.",
        )

    if (
        decision == "approve"
        and proposal["has_collateral"]
        and not body.get("accept_collateral")
    ):
        # The one refusal that matters. A rule which also matches other
        # invoices will quietly re-decide them the moment it goes live, and
        # "approve" pressed without seeing that is exactly how a well-meant
        # exemption becomes a hole in a control.
        raise HTTPException(
            status_code=409,
            detail={
                "message": "This rule also matches other cases. Approve it "
                "only if that is intended.",
                "collateral_warning": proposal["collateral_warning"],
                "impact": proposal["impact"],
            },
        )

    updated = rules.set_status(
        principal.tenant_id,
        rule_id,
        APPROVED if decision == "approve" else REJECTED,
        approver=principal.email,
    )
    service.get_audit().emit(
        principal.context(proposal.get("source_case_id") or ""),
        "rule_approved" if decision == "approve" else "rule_rejected",
        {
            "rule": rule_id,
            "author": proposal["author"],
            "approver": principal.email,
            # Visible rather than hidden: whether anyone else checked this.
            "self_approved": proposal["author"] == principal.email,
            "accepted_collateral": bool(body.get("accept_collateral")),
            "note": body.get("note"),
        },
        before={"status": proposal["status"]},
    )
    return updated


@router.post("/api/rules/{rule_id}/disable")
def disable_rule(
    rule_id: str,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    """The kill switch. A live rule stops correcting anything, immediately."""
    from . import service  # noqa: PLC0415

    rules = _rules()
    if rules.proposal(principal.tenant_id, rule_id) is None:
        raise HTTPException(status_code=404, detail="Unknown rule.")

    updated = rules.update_proposal(principal.tenant_id, rule_id, enabled=False)
    service.get_audit().emit(
        principal.context(),
        "rule_disabled",
        {"rule": rule_id},
    )
    return updated


@router.get("/api/rules")
def list_rules(
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict:
    """The rules in force, readable by anyone who can read a decision.

    A reviewer looking at a corrected invoice must be able to look up the rule
    that corrected it. Governance nobody can read is not governance.
    """
    from . import service  # noqa: PLC0415

    doc, version = service.get_rules().load(principal.tenant_id)
    return {"rules": doc.get("rules", []), "version": version}
