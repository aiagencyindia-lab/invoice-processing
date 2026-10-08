"""Glue between the pure approval engine and the database.

`approvals.py` decides; this module gathers what it needs (rules, groups,
delegations, who uploaded and who edited the invoice), asks, and writes the
result down. The API and the worker both go through here so a chain is built
and decided the same way wherever it happens.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from . import approvals, tenant_settings
from .db.repositories import StepNotPending
from .ports import RunContext

logger = logging.getLogger(__name__)

NEEDS_DB = "Approval routing needs a database. Configure DATABASE_URL."


def _sla_days(tenant_id: str) -> int:
    return int(tenant_settings.effective(tenant_id)["approval_sla_days"])


def route(ctx, artifacts: dict, *, repo, now: datetime) -> list[dict]:
    """(Re)build the pending chain for a case from the current rules.

    A touchless invoice gets no steps: the AUTO rate is what a pilot is judged
    on, and only an amount tier may pull such an invoice back to a person.
    """
    t = ctx.tenant_id
    # A decided invoice is closed: re-processing (the retry endpoint) must not
    # append a fresh pending chain after an approval or rejection. A chain that
    # is only partly approved is still open, so a re-route keeps the decided
    # steps and replaces the pending ones.
    if (artifacts.get("workflow") or {}).get("decision"):
        return repo.chain(t, ctx.case_id)
    invoice = (artifacts.get("extraction") or {}).get("invoice") or {}
    rules = repo.list_rules(t)
    specs = (
        approvals.build_chain(invoice, rules)
        if approvals.needs_person(invoice, rules)
        else []
    )
    return repo.replace_pending(
        t,
        ctx.case_id,
        _not_yet_asked(repo.chain(t, ctx.case_id), specs),
        sla_days=_sla_days(t),
        now=now,
    )


def _not_yet_asked(chain: list[dict], specs: list) -> list:
    """The specs still to ask, given the decided steps already in the chain."""
    # Someone who already approved a step is not asked again by the new chain,
    # neither for the same seat nor by name (they may have approved through a
    # group or role seat).
    approved = [s for s in chain if s["status"] == "approved"]
    seats = {
        (s["assignee_kind"], s["assignee_value"].casefold()) for s in approved
    }
    people = {s["decided_by"].casefold() for s in approved if s["decided_by"]}
    specs = [
        sp
        for sp in specs
        if (sp.assignee.kind, sp.assignee.value.casefold()) not in seats
        and not (
            sp.assignee.kind == "user"
            and sp.assignee.value.casefold() in people
        )
    ]
    if approved and not specs:
        # Some approvals are recorded but none are left to ask. With no pending
        # step the chain could never complete (only deciding a step writes the
        # workflow decision), so another approver must confirm the changed
        # invoice. Nothing is ever auto-completed.
        specs = [approvals.ROLE_FALLBACK]
    return specs


def uploader_of(ctx, documents) -> str | None:
    # A direct lookup, not a listing: a listing is capped, and a miss here
    # would let the uploader approve their own invoice.
    doc = documents.get(ctx.tenant_id, ctx.case_id)
    if doc and doc.get("source") == "upload":
        return doc.get("source_ref")
    return None


def editors_of(ctx, corrections) -> list[str]:
    rows = corrections.list(ctx.tenant_id, ctx.case_id)
    return sorted({r["actor"] for r in rows if r.get("actor")})


class Refused(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class AlreadyDecided(Refused):
    """Lost a race: another person decided the step first (HTTP 409)."""


@dataclass
class Decision:
    step: dict
    completed: bool
    why: str
    overridden: bool
    appended_second: bool


def decide(
    ctx,
    principal,
    artifacts: dict,
    *,
    repo,
    documents,
    corrections,
    approve: bool,
    note,
    override_reason,
    now: datetime,
) -> Decision:
    t, case = ctx.tenant_id, ctx.case_id
    # Crash recovery: if the chain completed but the workflow write failed, the
    # next act finds no pending step and builds a fresh chain after the decided
    # ones, so the invoice can still be finished.
    current = repo.current(t, case)
    if current is None:
        chain = repo.chain(t, case)
        if any(s["status"] == "rejected" for s in chain):
            # The rejection landed and only the workflow write was lost; a
            # fresh chain would let a later approval overturn it.
            raise AlreadyDecided("This invoice was already rejected.")
        # Processed before routing existed, or routed touchless: someone acting
        # on it has chosen to review it. build_chain is never empty (it falls
        # back to a role step), unlike route() which yields [] for touchless.
        # Filtered as route() filters, so nobody who approved is asked again.
        invoice = (artifacts.get("extraction") or {}).get("invoice") or {}
        repo.replace_pending(
            t,
            case,
            _not_yet_asked(
                chain, approvals.build_chain(invoice, repo.list_rules(t))
            ),
            sla_days=_sla_days(t),
            now=now,
        )
        current = repo.current(t, case)

    email = principal.email
    groups = repo.groups_map(t)
    delegators = repo.active_delegators_of(t, email, now.date())
    why = approvals.satisfaction(
        email, principal.role, current, groups=groups, delegators_of=delegators
    )
    block = approvals.refusal(
        email,
        uploader=uploader_of(ctx, documents),
        previous_deciders=[
            s["decided_by"]
            for s in repo.chain(t, case)
            if s["status"] == "approved"
        ],
    )
    overridden = bool((why is None or block) and override_reason)
    if (why is None or block) and not (
        principal.can("admin") and override_reason
    ):
        raise Refused(block or "This step is not assigned to you.")

    acting_for = None
    if why and why.startswith("delegate for"):
        # Name the delegator whose seat this is (the group case does not say).
        acting_for = next(
            d
            for d in delegators
            if approvals.satisfaction(
                d, "viewer", current, groups=groups, delegators_of=[]
            )
        )
    sla = _sla_days(t)
    try:
        step, completed = repo.decide(
            t,
            current["id"],
            approve=approve,
            decided_by=email,
            acting_for=acting_for,
            override_reason=override_reason if overridden else None,
            note=note,
            sla_days=sla,
            now=now,
        )
    except StepNotPending:
        raise AlreadyDecided("Someone else just decided this step.") from None
    second = False
    # An editor may be one of several approvers; they only need a second
    # person when nobody else has approved.
    nobody_else = not any(
        s["status"] == "approved" and s["id"] != step["id"]
        for s in repo.chain(t, case)
    )
    if (
        approve
        and completed
        and approvals.editor_needs_second(
            email, editors_of(ctx, corrections), is_only_pending=nobody_else
        )
    ):
        # Someone who corrected the fields cannot be the only approver.
        repo.append_step(
            t, case, approvals.ROLE_FALLBACK, sla_days=sla, now=now
        )
        completed, second = False, True
    return Decision(step, completed, why or "override", overridden, second)


def escalate_overdue(repo, audit, *, now: datetime) -> int:
    """Hand every overdue, not-yet-escalated step to a manager, else the admins.

    Escalation notifies; it does not reassign, so the original assignee can
    still decide. One tenant failing must not stop the others being swept.
    """
    count = 0
    for t in repo.tenant_ids():
        try:
            steps = repo.overdue(t, now)
            for step in steps:
                manager = (
                    repo.manager_of(t, step["assignee_value"])
                    if step["assignee_kind"] == "user"
                    else None
                )
                target = manager or "role:admin"
                try:
                    repo.escalate(t, step["id"], to=target, now=now)
                except StepNotPending:
                    continue  # decided or escalated since the scan
                count += 1
                if audit is None:
                    continue
                # The escalation is already recorded; a failing audit sink
                # must not undo it or skip this tenant's remaining steps.
                try:
                    audit.emit(
                        RunContext(
                            tenant_id=t, case_id=step["case_id"], actor="worker"
                        ),
                        "approval_escalated",
                        {
                            "position": step["position"],
                            "assignee": f"{step['assignee_kind']}:{step['assignee_value']}",
                            "escalated_to": target,
                            "due_at": step["due_at"],
                        },
                    )
                except Exception:
                    logger.exception("Audit of escalation failed for %s", t)
        except Exception:
            logger.exception("Escalation scan failed for tenant %s", t)
    return count
