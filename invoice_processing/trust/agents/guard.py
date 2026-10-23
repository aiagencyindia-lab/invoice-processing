"""What each governed route calls just before its write (FR-28.2, U1).

Read the `agents` mode first: `off` returns before anything else is read or
built. Then find the agent behind the principal; none → the route proceeds as
it always did, except U9's owner check on an invoice decision. For an agent:
lock it, evaluate, record, and in enforcing refuse a BLOCK with a 403 whose
body explains it. Nothing here writes the governed table, so a refusal
leaves it untouched.

A failure is a 503 in enforcing (the action was not taken) and a logged
no-op otherwise (B8). A mode that cannot be read is a logged no-op: the
route then meets the same database failure it always did.
"""

from __future__ import annotations

import dataclasses
import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from ...db.session import tenant_session
from .. import modes, thresholds
from . import ENGINE
from .policy import Action, Decision, Reason, evaluate_action
from .record import record

logger = logging.getLogger(__name__)

UNAVAILABLE = "Agent control is unavailable; the action was not taken."
CHAIN = ("invoice.approve", "invoice.reject")


def _fold(email) -> str:
    return (email or "").strip().casefold()


def check(principal, action, *, settings_store=None, now=None, store=None):
    """`action` is an `Action`, or a zero-argument builder of one (or of a
    list, checked in order, the first refusal stopping the rest). A builder
    runs only when the engine is on, so an engine-off route reads nothing
    extra. Returns the decision (a list for a list), or None when the guard
    has nothing to say."""
    try:
        mode = modes.engine_mode(principal.tenant_id, ENGINE, settings_store)
    except Exception as exc:
        # The settings live in the database the write is about to use: let
        # the route meet that failure as it always has.
        _log(exc)
        return None
    if mode == "off":
        return None
    try:
        return _check(
            principal,
            action,
            mode=mode,
            settings_store=settings_store,
            now=now or datetime.now(UTC),
            store=store,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _log(exc)
        if mode == "enforcing":
            raise HTTPException(503, UNAVAILABLE) from None
        return None


class Settle:
    """Handed to the governed route: `written()` right after the write
    commits, so a failure in the bookkeeping that follows does not turn a
    committed write into "failed" (the cap would lose real money)."""

    def __init__(self):
        self.committed = False

    def written(self) -> None:
        self.committed = True


@contextmanager
def settling(principal, decision):
    """Wrap the governed write: stamp the ledger row `executed` when the
    body finishes, or when it raises after `written()`; `failed` when it
    raises before (and re-raise). Spend skips a failed one, so a retry the
    route then refuses (409 "already decided") costs the agent nothing.
    No-op when the guard had nothing to say."""
    ids = [
        d.action_id
        for d in (decision if isinstance(decision, list) else [decision])
        if d is not None and d.action_id
    ]
    settle = Settle()
    try:
        yield settle
    except BaseException:
        _settle(principal, ids, "executed" if settle.committed else "failed")
        raise
    _settle(principal, ids, "executed")


def _settle(principal, ids, outcome) -> None:
    if not ids:
        return
    try:
        from ...api import service  # noqa: PLC0415

        store = service.get_agent_store()
        for action_id in ids:
            store.set_outcome(principal.tenant_id, action_id, outcome)
    except Exception as exc:
        # The write's own result stands; an unstamped row still counts.
        _log(exc)


def _log(exc: Exception) -> None:
    logger.warning("agent_guard_failed", extra={"error": type(exc).__name__})


def _check(principal, action, *, mode, settings_store, now, store):
    if store is None:
        from ...api import service  # noqa: PLC0415 - asked from the API

        store = service.get_agent_store()
    if store is None:
        return None  # no table: nothing can be registered
    built = action() if callable(action) else action
    many = isinstance(built, list)
    # Once per request, however many actions it carries.
    agent = (
        store.by_principal(principal.tenant_id, principal.user_id)
        if principal.user_id
        else None
    )
    out = [
        _one(
            agent,
            principal,
            a,
            mode=mode,
            settings_store=settings_store,
            now=now,
            store=store,
        )
        for a in (built if many else [built])
    ]
    return out if many else out[0]


def _one(agent, principal, action: Action, *, mode, settings_store, now, store):
    tenant = principal.tenant_id
    if agent is None:
        if action.kind in CHAIN:
            return _owner_check(
                principal, action, mode=mode, now=now, store=store
            )
        return None
    from ... import tenant_settings  # noqa: PLC0415 - avoids an import cycle

    settings = thresholds.merged(
        tenant_settings.effective(tenant, settings_store).get(
            "trust_thresholds"
        )
    )
    ctx = principal.context()
    # Held from the spend read to the ledger row, so two approvals cannot
    # both fit one cap (B17). Record writes from its own connection; the
    # lock is FOR NO KEY UPDATE so its FK check does not wait on this one.
    with tenant_session(tenant, store.url) as s:
        store.lock_agent(s, tenant, agent["id"])
        mandate = store.current_mandate(tenant, agent["id"], now)
        decision = evaluate_action(
            ctx,
            action,
            agent=agent,
            mandate=mandate,
            store=store,
            settings=settings,
            now=now,
            mode=mode,
            decider=None,
        )
        decision = _record(
            ctx, decision, action, agent, mandate, store, mode, now
        )
    return _answer(decision, mode)


def _owner_check(principal, action: Action, *, mode, now, store):
    """A person deciding a chain an agent they own already decided (U9).
    The only time the guard touches a human."""
    deciders = {_fold(d) for d in action.prior_deciders}
    if not deciders or not principal.email:
        return None
    tenant = principal.tenant_id
    mine = [
        a
        for a in store.by_owner(tenant, principal.email)
        if _fold(a.get("principal_email")) in deciders
    ]
    if not mine:
        return None
    agent = mine[0]
    verb = "approve" if action.kind == "invoice.approve" else "decide"
    reason = Reason(
        "same_owner_chain",
        "AGENT-SAME-OWNER-CHAIN",
        "BLOCK",
        f"You and your agent {agent['name']} cannot both {verb} this invoice.",
    )
    decision = Decision(
        "BLOCK", (reason,), reason.message, agent_id=agent["id"]
    )
    mandate = store.current_mandate(tenant, agent["id"], now)
    ctx = principal.context()
    decision = _record(ctx, decision, action, agent, mandate, store, mode, now)
    return _answer(decision, mode)


def _record(ctx, decision, action, agent, mandate, store, mode, now):
    from ...api import service  # noqa: PLC0415

    action_id = record(
        ctx,
        decision,
        action,
        agent=agent,
        mandate=mandate,
        store=store,
        findings=service.get_finding_store(),
        graph=store.graph,
        mode=mode,
        source="inline",
        refused=mode == "enforcing" and decision.decision == "BLOCK",
        now=now,
    )
    return dataclasses.replace(decision, action_id=action_id)


def _answer(decision: Decision, mode: str) -> Decision:
    if mode == "enforcing" and decision.decision == "BLOCK":
        raise HTTPException(
            403,
            {
                "detail": decision.explanation,
                "action_id": decision.action_id,
                "reasons": [
                    {"type": r.type, "message": r.message}
                    for r in decision.reasons
                ],
            },
        )
    return decision


# -- what the invoice routes build -------------------------------------------


def _amount(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


def invoice_action(ctx, artifacts: dict, kind: str) -> Action:
    """The `Action` for approving, rejecting or exporting this invoice, from
    the artifacts the route already read plus the lookups the rules need."""
    from ... import approval_flow, po_matching  # noqa: PLC0415
    from ...api import service  # noqa: PLC0415
    from . import inputs  # noqa: PLC0415

    t = ctx.tenant_id
    invoice = (artifacts.get("extraction") or {}).get("invoice") or {}
    name = str(invoice.get("vendor_name") or "")
    vendors = service.get_vendors()
    vendor = (vendors.find_by_name(t, name) if vendors and name else None) or {}
    approvals = service.get_approvals()
    steps = approvals.chain(t, ctx.case_id) if approvals else []

    def text() -> str:
        # Read only if a detector asks: the PDF text layer and email body.
        document = service.get_documents().get(t, ctx.case_id) or {}
        return inputs.text_of(document, service.get_blobs(t))()

    return Action(
        kind=kind,
        subject_table="invoices",
        subject_ref=f"invoices:{ctx.case_id}",
        vendor_key=po_matching.normalize_vendor(name) or None,
        category=vendor.get("kind") or None,
        amount=None
        if kind == "invoice.reject"
        else _amount(invoice.get("invoice_total_inc_tax")),
        # As the mandate names it: spend sums match the currency exactly.
        currency=str(invoice.get("currency") or "").strip().upper() or None,
        transaction_node=service._trust_node(t, ctx.case_id),
        initiated_by=approval_flow.uploader_of(ctx, service.get_documents()),
        prior_deciders=tuple(
            s["decided_by"]
            for s in steps
            if s["status"] != "pending" and s.get("decided_by")
        ),
        vendor_verified=bool(vendor.get("bank_verified_at")),
        vendor_verified_by=vendor.get("bank_verified_by"),
        text=text,
    )
