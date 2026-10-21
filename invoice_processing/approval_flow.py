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
from types import SimpleNamespace

from . import approvals, tenant_settings
from .db.repositories import StepNotPending
from .ports import RunContext

logger = logging.getLogger(__name__)

NEEDS_DB = "Approval routing needs a database. Configure DATABASE_URL."
BOOK_DOWN = "Purchase-order lines could not be re-checked. Try again."


def _sla_days(tenant_id: str) -> int:
    return int(tenant_settings.effective(tenant_id)["approval_sla_days"])


def routing_invoice(artifacts: dict) -> dict:
    """The invoice as the engine sees it: extracted fields plus GL accounts."""
    invoice = (artifacts.get("extraction") or {}).get("invoice") or {}
    coding = artifacts.get("gl_coding")
    lines = coding.get("lines") if isinstance(coding, dict) else None
    accounts = [
        line["gl_account"]
        for line in (lines if isinstance(lines, list) else [])
        if isinstance(line, dict) and isinstance(line.get("gl_account"), str)
    ]
    return {**invoice, "_gl_accounts": accounts}


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
    # A rejected step closes the chain even when the workflow write was lost
    # (a crash between deciding the step and recording the decision): new
    # pending steps after it would let a later approval overturn the
    # rejection. decide() refuses such a chain for the same reason.
    chain = repo.chain(t, ctx.case_id)
    if any(s["status"] == "rejected" for s in chain):
        return chain
    invoice = routing_invoice(artifacts)
    rules = repo.list_rules(t)
    specs = (
        approvals.build_chain(invoice, rules)
        if approvals.needs_person(invoice, rules)
        else []
    )
    return repo.replace_pending(
        t,
        ctx.case_id,
        _not_yet_asked(chain, specs),
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


def editors_of(ctx, corrections, artifacts: dict) -> list[str]:
    """Who changed the invoice: corrected a field or coded a line by hand.

    Coding a line routes the invoice (a GL rule can add or drop a step), so a
    coder is an editor exactly as a field corrector is.
    """
    rows = corrections.list(ctx.tenant_id, ctx.case_id)
    coding = artifacts.get("gl_coding")
    lines = coding.get("lines") if isinstance(coding, dict) else None
    coders = [
        line.get("coded_by")
        for line in (lines if isinstance(lines, list) else [])
        if isinstance(line, dict) and line.get("source") == "manual"
    ]
    return sorted(
        {r["actor"] for r in rows if r.get("actor")}
        | {c for c in coders if isinstance(c, str) and c}
    )


def assess(
    ctx, principal, chain: list[dict], *, repo, documents, now: datetime
) -> tuple[str | None, str | None]:
    """(why the caller may decide the current step, why they may not).

    The one test both decide() and the chain endpoint apply, so the workbench
    never offers a decision the server would refuse. The refusal is the
    segregation sentence when one applies, else who the step belongs to; it is
    None when the caller may decide. No current step: (None, None).
    """
    current = next((s for s in chain if s["status"] == "pending"), None)
    if current is None:
        return None, None
    t, email = ctx.tenant_id, principal.email
    why = approvals.satisfaction(
        email,
        principal.role,
        current,
        groups=repo.groups_map(t),
        delegators_of=repo.active_delegators_of(t, email, now.date()),
    )
    refusal = approvals.refusal(
        email,
        uploader=uploader_of(ctx, documents),
        previous_deciders=[
            s["decided_by"] for s in chain if s["status"] == "approved"
        ],
    )
    if refusal is None and why is None:
        refusal = (
            f"Step {current['position']} is assigned to "
            f"{current['assignee_kind']}:{current['assignee_value']}."
        )
    return why, refusal


class Refused(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class AlreadyDecided(Refused):
    """Lost a race: another person decided the step first (HTTP 409)."""


class OverBilled(Refused):
    """Another invoice consumed the received units since the match (409)."""


class BookUnavailable(Refused):
    """The PO book could not be read to re-check the match (503)."""


class PaymentHold(Refused):
    """A trust engine in enforcing mode holds this payment (409)."""


def payment_hold(ctx) -> dict | None:
    """The trust layer's hold on this payment ({held, message, holds,
    waiting, transaction_node}), or None when it is not held. See
    trust/gate.py."""
    from .trust import gate  # noqa: PLC0415 - optional layer, loaded on use

    detail = gate.payment_hold_detail(ctx.tenant_id, ctx.case_id)
    return detail if detail["held"] else None


def override_of(held: dict | None) -> dict | None:
    """What an administrator's override let through: the message they saw
    and the findings it named, so a finding raised later still holds."""
    if not held:
        return None
    return {
        "message": held["message"],
        "finding_ids": [h["finding_id"] for h in held["holds"]],
    }


def over_billed(ctx, artifacts: dict) -> tuple[str | None, dict | None]:
    """Why approving now would bill more than was received or ordered, and
    the step-2.3 entry to store if the approval goes through.

    Step 2.3 counts only invoices already approved, so two invoices matched
    side by side can each pass against the same units; the final approval is
    the last place to catch it. The caller holds `db.po_lock` around this and
    the write that bills the invoice, so two approvals cannot both pass. A
    hold the stored entry already carried was accepted by whoever approved it
    and is not raised again. Raises BookUnavailable when the book cannot be
    read: this check fails closed.

    A stored entry the pipeline could not check ("unchecked") accepted
    nothing and has no lines to bill, so it is matched live in full: any hold
    refuses, and on approval the live entry is stored so billed counts it.
    """
    from . import (  # noqa: PLC0415
        paths,
        po_book,
        rematch,
        three_way,
        vendor_master,
    )
    from .api import service  # noqa: PLC0415
    from .shared_libraries.acting import (  # noqa: PLC0415
        general_invoice_agent as agent,
    )

    stored = rematch.three_way_validation(artifacts.get("phase2"))
    unchecked = bool(stored) and three_way.UNCHECKED in (
        stored.get("signals") or []
    )
    if not stored or not (unchecked or stored.get("lines")):
        return None, None
    t = ctx.tenant_id
    invoice = (artifacts.get("extraction") or {}).get("invoice") or {}
    try:
        orders = service.get_purchase_orders()
        if orders is None:
            # ponytail: a file book has no record of approved invoices, so
            # there is nothing cumulative to re-check against.
            return None, None
        # Read once and handed to the matcher, so the ordered quantity named
        # below is the one the match used.
        snapshot = po_book.DbBook(orders, t).book(invoice.get("po_number"))
        vendors = service.get_vendors()
        records = vendors.load_records(t) if vendors else None
        # Built here, not via apply_to_pipeline: that rewrites the agent's
        # process-wide config, which an approval must not do.
        live = agent.three_way_entry(
            invoice,
            SimpleNamespace(book=lambda _po: snapshot),
            {**agent.DEFAULT_CONFIG, **tenant_settings.effective(t)},
            vendor_master.VendorMaster(records=records)
            if records
            else vendor_master.VendorMaster(paths.vendor_master_file(t)),
        )
    except Exception:
        logger.exception("Approval re-check of %s failed", ctx.case_id)
        raise BookUnavailable(BOOK_DOWN) from None
    holds = {
        three_way.OVER_ORDERED,
        three_way.SHORT_RECEIVED,
        three_way.NOT_RECEIVED,
    }
    accepted = {
        ln.get("line_number"): set(ln.get("signals") or [])
        for ln in stored.get("lines") or []
    }
    store = None
    if unchecked:
        accepted = {}  # nobody saw a match, so nobody accepted a hold
        store = dict(live)
        if "routing_before" in stored:
            store["routing_before"] = stored["routing_before"]
    for ln in live.get("lines") or []:
        new = (set(ln["signals"]) & holds) - accepted.get(
            ln["line_number"], set()
        )
        if not new:
            continue
        total = (ln["billed"] or 0) + (ln["quantity"] or 0)
        if three_way.OVER_ORDERED in new:
            ordered = next(
                p.get("qty_ordered")
                for p in snapshot["lines"]
                if p["line_number"] == ln["po_line_number"]
            )
            of = f"{float(ordered or 0):g} ordered"
        else:
            of = f"{ln['received'] or 0:g} received"
        return (
            f"Line {ln['line_number']} would bill {total:g} of {of}; the "
            "purchase order changed since this was checked."
        ), store
    if unchecked and not live.get("passed", True):
        return (
            "Purchase-order lines were not checked when this invoice was "
            f"processed, and checking them now holds it: {live['evidence']}"
        ), store
    return None, store


@dataclass
class Decision:
    step: dict
    completed: bool
    why: str
    overridden: bool
    appended_second: bool
    # Step 2.3 matched live for an "unchecked" hold, to store on approval.
    three_way: dict | None = None
    # The payment hold an administrator overrode, carried to export:
    # {message, finding_ids} (see `override_of`).
    hold_overridden: dict | None = None


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
        invoice = routing_invoice(artifacts)
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
    why, refusal = assess(
        ctx,
        principal,
        repo.chain(t, case),
        repo=repo,
        documents=documents,
        now=now,
    )
    overridden = bool(refusal and override_reason)
    if refusal and not (principal.can("admin") and override_reason):
        raise Refused(refusal)
    pending = [s for s in repo.chain(t, case) if s["status"] == "pending"]
    recheck = None
    held = None
    if approve and pending and pending[-1]["id"] == current["id"]:
        # Only the decision that would complete the chain is re-checked:
        # that is when the invoice starts counting as billed.
        over, recheck = over_billed(ctx, artifacts)
        if over and not (principal.can("admin") and override_reason):
            raise OverBilled(over)
        overridden = overridden or bool(over and override_reason)
        held = payment_hold(ctx)
        if held and not (principal.can("admin") and override_reason):
            raise PaymentHold(held["message"])
        overridden = overridden or bool(held and override_reason)

    acting_for = None
    if why and why.startswith("delegate for"):
        # Name the delegator whose seat this is (the group case does not say).
        groups = repo.groups_map(t)
        acting_for = next(
            d
            for d in repo.active_delegators_of(t, email, now.date())
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
            email,
            editors_of(ctx, corrections, artifacts),
            is_only_pending=nobody_else,
        )
    ):
        # Someone who corrected the fields or coded a line cannot be the only
        # approver.
        repo.append_step(
            t, case, approvals.ROLE_FALLBACK, sla_days=sla, now=now
        )
        completed, second = False, True
    return Decision(
        step,
        completed,
        why or "override",
        overridden,
        second,
        recheck,
        override_of(held),
    )


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
