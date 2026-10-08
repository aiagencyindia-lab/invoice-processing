"""Who must approve what: the queue, an invoice's chain, and the rules,
groups and delegations that build chains.

Rules and groups are tenant policy, so only an admin writes them (a clerk may
read, to see why an invoice went where it did). Delegations are personal: an
approver hands over their own seat, an admin may do it for anyone. Everything
is validated at write time, because a rule naming an email nobody owns would
only surface later as an invoice nobody can approve.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import approval_flow, approvals
from ..db import repositories as repos
from .auth import ROLE_RANK, Principal, require_role

logger = logging.getLogger(__name__)
router = APIRouter()

# ponytail: kind names and match keys are duplicated from approvals.py's
# engine; if a fourth rule kind appears, derive both from one table.
MATCH_KEYS = {
    "vendor": {"vendor_name"},
    "entity": {"organization_name"},
    "amount": {"min_amount", "currency"},
}
STEP_KINDS = ("user", "group", "role")

Name = Annotated[str, Field(max_length=128)]
Email = Annotated[str, Field(max_length=320)]


class GroupIn(BaseModel):
    name: Name
    members: list[Email] = Field(max_length=500)


class GroupPatch(BaseModel):
    name: Name | None = None
    members: list[Email] | None = Field(default=None, max_length=500)


class StepIn(BaseModel):
    kind: str = Field(max_length=16)
    value: str = Field(max_length=320)


class RuleIn(BaseModel):
    name: Name
    kind: str = Field(max_length=16)
    match: dict
    steps: list[StepIn] = Field(max_length=20)
    enabled: bool = True


class RulePatch(BaseModel):
    name: Name | None = None
    kind: str | None = Field(default=None, max_length=16)
    match: dict | None = None
    steps: list[StepIn] | None = Field(default=None, max_length=20)
    enabled: bool | None = None


class DelegationIn(BaseModel):
    delegator_email: Email
    delegate_email: Email
    starts_on: date
    ends_on: date


def _repo():
    from . import service  # noqa: PLC0415

    try:
        repo = service.get_approvals()
    except Exception:
        logger.exception("Approval routing could not be reached")
        raise HTTPException(503, "Approval routing is unavailable.") from None
    if repo is None:
        raise HTTPException(503, approval_flow.NEEDS_DB)
    return repo


def _audit():
    from . import service  # noqa: PLC0415

    return service.get_audit()


def _tenant_users(tenant_id: str) -> dict[str, str]:
    from .admin_routes import _provisioner  # noqa: PLC0415

    return {
        u["email"].casefold(): u["role"]
        for u in _provisioner().list_users(tenant_id)
    }


def _fold(email: str) -> str:
    return email.strip().casefold()


# Roles that can decide a step. Anyone else named on a chain would be a seat
# nobody can fill: the act endpoint needs approver.
APPROVING = ("approver", "admin")


def _need_users(
    tenant_id: str,
    emails,
    what: str,
    known: dict[str, str] | None = None,
    *,
    approving: bool = True,
) -> list[str]:
    known = _tenant_users(tenant_id) if known is None else known
    out = [_fold(e) for e in emails]
    for e in out:
        if e not in known:
            raise HTTPException(
                400, f"{e} is not a user of this account, so {what}."
            )
        if approving and known[e] not in APPROVING:
            raise HTTPException(
                400,
                f"{e} is a {known[e]}, so {what}; only an approver or admin "
                "can approve.",
            )
    return out


def _rules_naming(repo, tenant_id: str, group_name: str) -> list[str]:
    return [
        r["name"]
        for r in repo.list_rules(tenant_id)
        if any(
            s.get("kind") == "group" and s.get("value") == group_name
            for s in r["steps"]
        )
    ]


def _check_rule(repo, tenant_id: str, kind, match, steps) -> tuple[dict, list]:
    if kind not in MATCH_KEYS:
        raise HTTPException(
            400, f"Unknown rule kind {kind!r}. Use vendor, entity or amount."
        )
    allowed = MATCH_KEYS[kind]
    stray = set(match) - allowed
    if stray:
        raise HTTPException(
            400,
            f"A {kind} rule matches on {', '.join(sorted(allowed))}, "
            f"not {', '.join(sorted(stray))}.",
        )
    if kind == "amount":
        floor = match.get("min_amount")
        if (
            isinstance(floor, bool)
            or not isinstance(floor, int | float)
            or not math.isfinite(floor)
            or floor < 0
        ):
            raise HTTPException(
                400, "min_amount must be a number, zero or more."
            )
        cur = match.get("currency")
        if cur is not None and not (
            isinstance(cur, str) and len(cur) == 3 and cur.isalpha()
        ):
            raise HTTPException(
                400, "currency must be a three-letter code such as AUD."
            )
        match = {k: v for k, v in match.items() if v is not None}
    else:
        (key,) = allowed
        if not (isinstance(match.get(key), str) and match[key].strip()):
            raise HTTPException(400, f"A {kind} rule needs a {key}.")
    if not steps:
        raise HTTPException(400, "A rule needs at least one approval step.")
    groups = repo.groups_map(tenant_id)
    known = None
    clean = []
    for step in steps:
        raw = step if isinstance(step, dict) else step.model_dump()
        k, v = raw["kind"], raw["value"].strip()
        if k not in STEP_KINDS:
            raise HTTPException(
                400, f"Unknown step kind {k!r}. Use user, group or role."
            )
        if k == "user":
            known = _tenant_users(tenant_id) if known is None else known
            (v,) = _need_users(
                tenant_id, [v], "it cannot approve a step", known
            )
        elif k == "group" and v not in groups:
            raise HTTPException(400, f"There is no approver group {v!r}.")
        elif k == "group" and not groups[v]:
            raise HTTPException(
                400,
                f"Approver group {v!r} has no members, so nobody could approve.",
            )
        elif k == "role" and v not in ROLE_RANK:
            raise HTTPException(
                400,
                f"Unknown role {v!r}. Use one of {', '.join(sorted(ROLE_RANK))}.",
            )
        clean.append({"kind": k, "value": v})
    return match, clean


# -- the queue and a chain -----------------------------------------------------


@router.get("/api/approvals/mine")
def my_queue(
    principal: Annotated[Principal, Depends(require_role("viewer"))],
) -> dict:
    from . import service  # noqa: PLC0415

    repo, t = _repo(), principal.tenant_id
    now = datetime.now(timezone.utc)
    steps = repo.mine(
        t,
        email=principal.email,
        role=principal.role,
        groups_map=repo.groups_map(t),
        delegators_of=repo.active_delegators_of(t, principal.email, now.date()),
    )
    # The queue offers only what the caller could actually decide: the
    # uploader of an invoice, or someone who decided an earlier step of it,
    # would be refused by act, so listing it is a promise the server breaks.
    # ponytail: two lookups per waiting case; batch them if queues grow large.
    documents = service.get_documents()
    steps = [
        s
        for s in steps
        if approvals.refusal(
            principal.email,
            uploader=approval_flow.uploader_of(
                principal.context(s["case_id"]), documents
            ),
            previous_deciders=[
                x["decided_by"]
                for x in repo.chain(t, s["case_id"])
                if x["status"] == "approved"
            ],
        )
        is None
    ]
    cases = service.read_cases(
        service.get_cases(), t, [s["case_id"] for s in steps]
    )
    items = []
    for s in steps:
        summary = service._summarize(s["case_id"], cases[s["case_id"]][0])
        due = s["due_at"]
        overdue = False
        if due:
            when = datetime.fromisoformat(due)
            overdue = when.replace(tzinfo=when.tzinfo or timezone.utc) < now
        items.append(
            {
                "case_id": s["case_id"],
                "vendor": summary["vendor"],
                "invoice_number": summary["invoice_number"],
                "total": summary["total"],
                "currency": summary["currency"],
                "position": s["position"],
                "due_at": due,
                "overdue": overdue,
                "why": s["why"],
                "assignee": f"{s['assignee_kind']}:{s['assignee_value']}",
            }
        )
    return {"items": items, "count": len(items)}


@router.get("/api/invoices/{case_id}/approvals")
def invoice_chain(
    case_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
) -> dict:
    from . import service  # noqa: PLC0415

    repo, ctx = _repo(), principal.context(case_id)
    if not service.get_cases().get_artifacts(ctx):
        raise HTTPException(404, f"Unknown case {case_id}")
    steps = repo.chain(principal.tenant_id, case_id)
    current = next((s for s in steps if s["status"] == "pending"), None)
    # The same test act applies, so the workbench's buttons and reason match
    # what a decision would get. An admin may still override a refusal.
    _, reason = approval_flow.assess(
        ctx,
        principal,
        steps,
        repo=repo,
        documents=service.get_documents(),
        now=datetime.now(timezone.utc),
    )
    if current is not None and not principal.can("approver"):
        # act needs the approver role before any seat counts; a seat holder
        # demoted since being named would only get a 403 (require_role's words).
        reason = "'approver' or higher is required."
    return {
        "steps": steps,
        "current": current["position"] if current else None,
        "can_decide": current is not None and reason is None,
        "reason": reason,
    }


# -- groups ---------------------------------------------------------------------
# Literal paths first so an id can never shadow them.


@router.get("/api/approvals/groups")
def list_groups(
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    groups = _repo().list_groups(principal.tenant_id)
    return {"groups": groups, "count": len(groups)}


def _name_free(repo, t: str, name: str, *, except_id: str | None = None):
    if any(
        g["name"].casefold() == name.casefold() and g["id"] != except_id
        for g in repo.list_groups(t)
    ):
        raise HTTPException(409, f"A group called {name!r} already exists.")


@router.post("/api/approvals/groups", status_code=201)
def create_group(
    request: GroupIn,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    repo, t = _repo(), principal.tenant_id
    name = request.name.strip()
    if not name:
        raise HTTPException(400, "A group needs a name.")
    if not request.members:
        raise HTTPException(400, "A group needs at least one member.")
    members = _need_users(t, request.members, "it cannot join a group")
    _name_free(repo, t, name)
    group = repo.create_group(t, name, members)
    _audit().emit(principal.context(), "approval_group_created", group)
    return group


@router.patch("/api/approvals/groups/{group_id}")
def update_group(
    group_id: str,
    request: GroupPatch,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    repo, t = _repo(), principal.tenant_id
    before = next((g for g in repo.list_groups(t) if g["id"] == group_id), None)
    if before is None:
        raise HTTPException(404, "Unknown group")
    name = request.name.strip() if request.name is not None else None
    if name is not None:
        if not name:
            raise HTTPException(400, "A group needs a name.")
        if name != before["name"]:
            _name_free(repo, t, name, except_id=group_id)
            named = _rules_naming(repo, t, before["name"])
            if named:
                raise HTTPException(
                    409,
                    f"Rule {named[0]!r} still names this group; "
                    "change the rule first.",
                )
    if request.members is not None and not request.members:
        raise HTTPException(400, "A group needs at least one member.")
    members = (
        _need_users(t, request.members, "it cannot join a group")
        if request.members is not None
        else None
    )
    try:
        after = repo.update_group(t, group_id, name=name, members=members)
    except repos.UnknownGroup:
        raise HTTPException(404, "Unknown group") from None
    _audit().emit(
        principal.context(),
        "approval_group_updated",
        after,
        before=before,
    )
    return after


@router.delete("/api/approvals/groups/{group_id}")
def delete_group(
    group_id: str,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    repo, t = _repo(), principal.tenant_id
    group = next((g for g in repo.list_groups(t) if g["id"] == group_id), None)
    if group is None:
        raise HTTPException(404, "Unknown group")
    named = _rules_naming(repo, t, group["name"])
    if named:
        raise HTTPException(
            409,
            f"Rule {named[0]!r} still names this group; "
            "change or delete the rule first.",
        )
    try:
        repo.delete_group(t, group_id)
    except repos.UnknownGroup:
        raise HTTPException(404, "Unknown group") from None
    _audit().emit(principal.context(), "approval_group_deleted", group)
    return {"deleted": group_id}


# -- rules ----------------------------------------------------------------------


@router.get("/api/approvals/rules")
def list_rules(
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    rules = _repo().list_rules(principal.tenant_id)
    return {"rules": rules, "count": len(rules)}


@router.post("/api/approvals/rules", status_code=201)
def create_rule(
    request: RuleIn,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    repo, t = _repo(), principal.tenant_id
    name = request.name.strip()
    if not name:
        raise HTTPException(400, "A rule needs a name.")
    match, steps = _check_rule(
        repo, t, request.kind, request.match, request.steps
    )
    rule = repo.create_rule(
        t,
        name=name,
        kind=request.kind,
        match=match,
        steps=steps,
        enabled=request.enabled,
    )
    _audit().emit(principal.context(), "approval_rule_created", rule)
    return rule


@router.patch("/api/approvals/rules/{rule_id}")
def update_rule(
    rule_id: str,
    request: RulePatch,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    repo, t = _repo(), principal.tenant_id
    before = next((r for r in repo.list_rules(t) if r["id"] == rule_id), None)
    if before is None:
        raise HTTPException(404, "Unknown rule")
    fields = {}
    if request.name is not None:
        if not request.name.strip():
            raise HTTPException(400, "A rule needs a name.")
        fields["name"] = request.name.strip()
    if request.enabled is not None:
        fields["enabled"] = request.enabled
    # Re-validate only what the caller changed: a user who has since left must
    # not block disabling the rule that names them. A new kind or match is
    # checked against the merged rule so the two cannot disagree.
    if {"kind", "match", "steps"} & request.model_fields_set:
        kind = request.kind or before["kind"]
        match, steps = _check_rule(
            repo,
            t,
            kind,
            request.match if request.match is not None else before["match"],
            request.steps if request.steps is not None else before["steps"],
        )
        fields |= {"kind": kind, "match": match, "steps": steps}
    try:
        after = repo.update_rule(t, rule_id, **fields)
    except repos.UnknownRule:
        raise HTTPException(404, "Unknown rule") from None
    _audit().emit(
        principal.context(), "approval_rule_updated", after, before=before
    )
    return after


@router.delete("/api/approvals/rules/{rule_id}")
def delete_rule(
    rule_id: str,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    repo, t = _repo(), principal.tenant_id
    rule = next((r for r in repo.list_rules(t) if r["id"] == rule_id), None)
    if rule is None:
        raise HTTPException(404, "Unknown rule")
    try:
        repo.delete_rule(t, rule_id)
    except repos.UnknownRule:
        raise HTTPException(404, "Unknown rule") from None
    _audit().emit(principal.context(), "approval_rule_deleted", rule)
    return {"deleted": rule_id}


# -- delegations ----------------------------------------------------------------


@router.get("/api/approvals/delegations")
def list_delegations(
    principal: Annotated[Principal, Depends(require_role("approver"))],
) -> dict:
    rows = _repo().list_delegations(
        principal.tenant_id,
        email=None if principal.can("admin") else principal.email,
    )
    return {"delegations": rows, "count": len(rows)}


@router.post("/api/approvals/delegations", status_code=201)
def create_delegation(
    request: DelegationIn,
    principal: Annotated[Principal, Depends(require_role("approver"))],
) -> dict:
    repo, t = _repo(), principal.tenant_id
    if not principal.can("admin") and _fold(request.delegator_email) != _fold(
        principal.email
    ):
        raise HTTPException(403, "You can only delegate your own approvals.")
    # Only the delegate must be able to approve: they decide in the seat.
    (delegator,) = _need_users(
        t,
        [request.delegator_email],
        "it cannot take part in a delegation",
        approving=False,
    )
    (delegate,) = _need_users(
        t, [request.delegate_email], "it cannot cover approvals"
    )
    try:
        row = repo.create_delegation(
            t,
            delegator=delegator,
            delegate=delegate,
            starts_on=request.starts_on,
            ends_on=request.ends_on,
            created_by=principal.email,
        )
    except repos.InvalidDelegation as exc:
        raise HTTPException(400, str(exc)) from None
    _audit().emit(principal.context(), "delegation_created", row)
    return row


@router.delete("/api/approvals/delegations/{delegation_id}")
def delete_delegation(
    delegation_id: str,
    principal: Annotated[Principal, Depends(require_role("approver"))],
) -> dict:
    repo, t = _repo(), principal.tenant_id
    row = next(
        (d for d in repo.list_delegations(t) if d["id"] == delegation_id),
        None,
    )
    if row is None:
        raise HTTPException(404, "Unknown delegation")
    if not principal.can("admin") and row["delegator_email"] != _fold(
        principal.email
    ):
        raise HTTPException(403, "You can only remove your own delegations.")
    repo.delete_delegation(t, delegation_id)
    _audit().emit(principal.context(), "delegation_deleted", row)
    return {"deleted": delegation_id}
