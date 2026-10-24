"""Agent control routes (FR-28.1, FR-28.5, FR-28.6; PRD §9), built dark.

Registry and mandate changes are for a human admin (`Admin` refuses an API
key); reads are approver. `actions/evaluate` also answers the agent's own
principal about itself. Nothing here refuses a business write: `dryrun` asks
a question and `log` records a past action; the inline guard is the policy
branch's.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from ..po_matching import normalize_vendor
from ..trust import thresholds
from ..trust.agents import ACTIONS, ledger, mandates, record, registry
from ..trust.agents.policy import Action
from ..trust.agents.policy import evaluate_action as evaluate
from ..trust.agents.store import MandateConflict, SelfApproval
from ..trust.history.readers import read_table
from ..trust.modes import engine_mode
from .auth import Principal, require_exact_role, require_role
from .integration_routes import Admin

logger = logging.getLogger(__name__)
router = APIRouter()
Approver = Annotated[Principal, Depends(require_role("approver"))]
Anyone = Annotated[Principal, Depends(require_role("viewer"))]
# A power the controller alone holds (PRD §4: Controller approval of agents).
Controller = Annotated[Principal, Depends(require_exact_role("controller"))]
NEEDS_DB = "Agent control needs a database. Configure DATABASE_URL."
OFF = (
    "Agent control is off for this tenant; turn it on (shadow or higher) first."
)
CHECK_FAILED = "A check could not run; try again."
NO_AGENT = "Name the agent: this caller is not registered as one."
MAX_IMPORT_ROWS = 5000
MAX_IMPORT_BYTES = 10 * 1024 * 1024
SUBJECT_TABLES = {
    "po": "purchase_orders",
    "document": "documents",
    "vendor": "vendors",
    "bank_change": "vendor_bank_changes",
    "invoice": "invoices",
}


class CheckFailed(RuntimeError):
    """A structural rule raised: nothing was recorded (B8)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _service():
    from . import service  # noqa: PLC0415

    return service


def _store():
    store = _service().get_agent_store()
    if store is None:
        raise HTTPException(503, NEEDS_DB)
    return store


def _audit():
    return _service().get_audit()


def _agent(principal: Principal, agent_id: str) -> dict:
    agent = _store().get(principal.tenant_id, agent_id)
    if agent is None:
        raise HTTPException(404, "Unknown agent.")
    return agent


def _current(tenant_id: str, agent_id: str, now: datetime) -> dict | None:
    store = _store()
    current = store.current_mandate(tenant_id, agent_id, now)
    if current is None:
        return None
    rows = mandates.annotate(store.mandates(tenant_id, agent_id), now)
    return next(m for m in rows if m["id"] == current["id"])


def _settings(tenant_id: str) -> dict:
    from .. import tenant_settings  # noqa: PLC0415

    return thresholds.merged(
        tenant_settings.effective(tenant_id).get("trust_thresholds")
    )


def _mode(tenant_id: str) -> str:
    """Off means the engine does nothing: no ledger row, no finding."""
    mode = engine_mode(tenant_id, "agents")
    if mode == "off":
        raise HTTPException(409, OFF)
    return mode


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _vendor_id(tenant_id: str, name) -> str | None:
    """The stored vendor of that name (`normalized_name` is unique)."""
    vendors = _service().get_vendors() if name else None
    found = vendors.find_by_name(tenant_id, name) if vendors else None
    return (found or {}).get("id")


def _invoice_node(tenant_id: str, subject: str) -> str | None:
    table, _, case_id = subject.partition(":")
    if table != "invoices" or not case_id:
        return None
    return _service()._trust_node(tenant_id, case_id)


def _judge(
    ctx, agent: dict, row: dict, source: str, now: datetime, mode: str
) -> dict:
    """Evaluate one past or hypothetical action and record the decision.
    ValueError names the first thing wrong with `row`."""
    kind = row.get("action")
    if kind not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}.")
    subject = (row.get("subject_ref") or "").strip()
    if not subject or len(subject) > 128:
        raise ValueError("subject_ref is required (at most 128 characters).")
    vendor = normalize_vendor(row["vendor"]) if row.get("vendor") else None
    if vendor is not None and len(vendor) > 128:
        raise ValueError("vendor is too long.")
    category = (row.get("category") or "").strip() or None
    if category is not None and len(category) > 64:
        raise ValueError("category is too long.")
    amount = None
    if row.get("amount") not in (None, ""):
        try:
            amount = Decimal(str(row["amount"]).strip())
        except InvalidOperation:
            raise ValueError("amount must be a number like 1200.50.") from None
        if not amount.is_finite() or amount < 0 or amount >= Decimal("1e14"):
            raise ValueError("amount must be a non-negative amount.")
    currency = (row.get("currency") or "").strip().upper() or None
    if currency is not None and (len(currency) != 3 or not currency.isalpha()):
        raise ValueError("currency must be a three-letter ISO code.")
    at = row.get("occurred_at")
    if isinstance(at, str):
        try:
            at = datetime.fromisoformat(at.strip()) if at.strip() else None
        except ValueError:
            raise ValueError("occurred_at must be an ISO date-time.") from None
    at = _utc(at)
    if source == "log" and at is None:
        raise ValueError("occurred_at is required for a logged action.")
    action = Action(
        kind=kind,
        subject_table=SUBJECT_TABLES[kind.split(".")[0]],
        subject_ref=subject,
        vendor_key=vendor,
        vendor_id=_vendor_id(ctx.tenant_id, row.get("vendor")),
        category=category,
        amount=amount,
        currency=currency,
        occurred_at=at,
        transaction_node=_invoice_node(ctx.tenant_id, subject),
    )
    store, service = _store(), _service()
    when = at or now
    # The version in force when it happened, not today's (B15).
    mandate = mandates.in_force(
        store.mandates(ctx.tenant_id, agent["id"]), when
    )
    try:
        decision = evaluate(
            ctx,
            action,
            agent=agent,
            mandate=mandate,
            store=store,
            settings=_settings(ctx.tenant_id),
            now=when,
        )
    except Exception as exc:
        logger.warning("agent_rule_failed", extra={"error": type(exc).__name__})
        raise CheckFailed from None
    action_id = record.record(
        ctx,
        decision,
        action,
        agent=agent,
        mandate=mandate,
        store=store,
        findings=service.get_finding_store(),
        graph=service.get_graph_store(),
        mode=mode,
        source=source,
        # A question or a past action: nothing here was stopped (B15).
        refused=False,
        now=now,
    )
    return {
        "action_id": action_id,
        "decision": decision.decision,
        "reasons": record._reasons(decision),
        "explanation": decision.explanation,
        "mode": mode,
        "source": source,
    }


def resolve_agents(
    store, tenant_id: str
) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    """The tenant's agents by casefolded name (a name may repeat) and by
    casefolded principal email, for naming an agent in a log row."""
    by_name: dict[str, list[dict]] = {}
    by_email: dict[str, dict] = {}
    for a in store.list(tenant_id):
        by_name.setdefault(a["name"].casefold(), []).append(a)
        if a["principal_email"]:
            by_email[a["principal_email"].casefold()] = a
    return by_name, by_email


def judge_log(
    principal: Principal, rows: list[dict], mode: str, *, first: int, own=False
) -> dict:
    """Judge each row as a logged action and answer as the import does. Row
    `i` is reported as `first + i`. With `own`, a row naming no agent is the
    caller's own agent (an API key bound to one)."""
    store, ctx, now = _store(), principal.context(), _now()
    by_name, by_email = resolve_agents(store, principal.tenant_id)
    mine = (
        store.by_principal(principal.tenant_id, principal.user_id)
        if own
        else None
    )
    counts: dict[str, int] = {}
    errors = []
    for line, row in enumerate(rows, start=first):
        try:
            who = (row.get("agent") or "").strip().casefold()
            if own and not who:
                agent = mine
                if agent is None:
                    raise ValueError(NO_AGENT)
            else:
                named = by_name.get(who, [])
                # A revoked namesake does not make the live agent ambiguous.
                named = [a for a in named if a["status"] == "active"] or named
                if len(named) > 1:
                    raise ValueError(
                        "Two agents share this name; use its principal email."
                    )
                agent = named[0] if named else by_email.get(who)
                if agent is None:
                    raise ValueError("Unknown agent.")
            out = _judge(ctx, agent, row, "log", now, mode)
        except ValueError as exc:
            errors.append({"row": line, "message": str(exc)})
            continue
        except CheckFailed:
            errors.append({"row": line, "message": CHECK_FAILED})
            continue
        counts[out["decision"]] = counts.get(out["decision"], 0) + 1
    return {"rows": len(rows), "by_decision": counts, "errors": errors}


# -- bodies ---------------------------------------------------------------------


class AgentPrincipal(BaseModel):
    model_config = ConfigDict(strict=True)

    api_key_id: str | None = Field(default=None, max_length=36)
    user_id: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=320)


class AgentCreate(BaseModel):
    name: str = Field(max_length=128)
    owner: str = Field(max_length=320)
    description: str | None = Field(default=None, max_length=500)
    principal: AgentPrincipal | None = None


class Reason(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class MandateCreate(BaseModel):
    scope: dict
    valid_from: datetime
    valid_to: datetime
    reason: str | None = Field(default=None, max_length=500)


class Evaluate(BaseModel):
    action: str = Field(max_length=32)
    subject_ref: str = Field(max_length=128)
    vendor: str | None = Field(default=None, max_length=256)
    category: str | None = Field(default=None, max_length=64)
    amount: str | None = Field(default=None, max_length=32)
    currency: str | None = Field(default=None, max_length=8)
    occurred_at: datetime | None = None
    source: Literal["dryrun", "log"]


# -- routes ---------------------------------------------------------------------


# Static paths before /{agent_id}, or "ledger" would be read as an id.
@router.get("/api/trust/agents/ledger")
def tenant_ledger(
    principal: Approver,
    since: datetime | None = None,
    until: datetime | None = None,
):
    try:
        since, until = ledger.window(since, until, _now())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return ledger.tenant_ledger(
        _store(), principal.tenant_id, since=since, until=until
    )


@router.get("/api/trust/agents/blocks/count")
def blocks_count(principal: Approver):
    """The shell banner's numbers: what was stopped and what needs review in
    the last `agents.banner_window_hours`."""
    hours = int(_settings(principal.tenant_id)["agents.banner_window_hours"])
    since = _now() - timedelta(hours=hours)
    return {
        **_store().count_blocked(principal.tenant_id, since),
        "window_hours": hours,
    }


@router.post("/api/trust/agents/actions/import")
def import_actions(principal: Admin, file: Annotated[UploadFile, File()]):
    mode = _mode(principal.tenant_id)
    data = file.file.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(400, "The file is larger than 10 MB.")
    try:
        _, rows = read_table(data, file.filename or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if len(rows) > MAX_IMPORT_ROWS:
        raise HTTPException(400, f"At most {MAX_IMPORT_ROWS} rows per file.")
    return judge_log(principal, rows, mode, first=2)  # line 1 is the header


@router.post("/api/trust/agents", status_code=201)
def register_agent(body: AgentCreate, principal: Admin):
    try:
        return registry.register(
            principal.context(),
            body.model_dump(),
            store=_store(),
            audit=_audit(),
            url=_store().url,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except registry.AgentConflict as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/api/trust/agents")
def list_agents(principal: Approver, status: str | None = None):
    if status not in (None, "proposed", "active", "rejected", "revoked"):
        raise HTTPException(
            400, "status must be proposed, active, rejected or revoked."
        )
    now, tenant = _now(), principal.tenant_id
    # Each row with its current mandate, so the agents screen needs one call.
    return {
        "agents": [
            {**a, "mandate": _current(tenant, a["id"], now)}
            for a in _store().list(tenant, status)
        ]
    }


@router.get("/api/trust/agents/{agent_id}")
def get_agent(agent_id: str, principal: Approver):
    agent = _agent(principal, agent_id)
    return {**agent, "mandate": _current(principal.tenant_id, agent_id, _now())}


@router.post("/api/trust/agents/{agent_id}/revoke")
def revoke_agent(agent_id: str, body: Reason, principal: Admin):
    try:
        return registry.revoke(
            principal.context(),
            agent_id,
            body.reason,
            store=_store(),
            audit=_audit(),
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except registry.NotProposed as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/api/trust/agents/{agent_id}/approve")
def approve_agent(agent_id: str, principal: Controller):
    return _settle(principal, agent_id, approve=True)


@router.post("/api/trust/agents/{agent_id}/reject")
def reject_agent(agent_id: str, body: Reason, principal: Controller):
    return _settle(principal, agent_id, approve=False, reason=body.reason)


def _settle(principal, agent_id: str, **kw):
    try:
        return registry.settle(
            principal.context(), agent_id, store=_store(), audit=_audit(), **kw
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except registry.NotProposed as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/api/trust/agents/{agent_id}/mandates", status_code=201)
def propose_mandate(agent_id: str, body: MandateCreate, principal: Admin):
    agent = _agent(principal, agent_id)
    if agent["status"] != "active":
        raise HTTPException(409, "Only an active agent takes a mandate.")
    try:
        scope, start, end = mandates.validate(
            body.model_dump(),
            now=_now(),
            # None without the vendors table: names only.
            vendors=_service().get_vendors(),
            tenant_id=principal.tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    ctx = principal.context()
    try:
        row = _store().propose_mandate(
            ctx,
            agent_id,
            scope=scope,
            valid_from=start,
            valid_to=end,
            reason=body.reason,
        )
    except MandateConflict as exc:
        raise HTTPException(409, str(exc)) from None
    _audit().emit(ctx, "agent_mandate_proposed", {"version": row["version"]})
    return row


@router.get("/api/trust/agents/{agent_id}/mandates")
def list_mandates(agent_id: str, principal: Approver):
    _agent(principal, agent_id)
    rows = _store().mandates(principal.tenant_id, agent_id)
    return {"mandates": mandates.annotate(rows, _now())}


def _mandate(principal: Principal, agent_id: str, mandate_id: str) -> dict:
    _agent(principal, agent_id)
    for m in _store().mandates(principal.tenant_id, agent_id):
        if m["id"] == mandate_id:
            return m
    raise HTTPException(404, "Unknown mandate.")


@router.post("/api/trust/agents/{agent_id}/mandates/{mandate_id}/approve")
def approve_mandate(agent_id: str, mandate_id: str, principal: Admin):
    _mandate(principal, agent_id, mandate_id)
    ctx = principal.context()
    try:
        row = mandates.approve(ctx, _store(), mandate_id)
    except mandates.MandateKeyMissing as exc:
        raise HTTPException(503, str(exc)) from None
    except SelfApproval as exc:
        raise HTTPException(403, str(exc)) from None
    except MandateConflict as exc:
        raise HTTPException(409, str(exc)) from None
    _audit().emit(ctx, "agent_mandate_approved", {"version": row["version"]})
    return row


@router.post("/api/trust/agents/{agent_id}/mandates/{mandate_id}/revoke")
def revoke_mandate(
    agent_id: str, mandate_id: str, body: Reason, principal: Admin
):
    before = _mandate(principal, agent_id, mandate_id)
    ctx = principal.context()
    row = mandates.revoke(ctx, _store(), mandate_id, body.reason)
    if before["status"] != "revoked":
        _audit().emit(
            ctx,
            "agent_mandate_revoked",
            {"version": row["version"], "reason": body.reason},
        )
    return row


@router.post("/api/trust/agents/{agent_id}/actions/evaluate")
def evaluate_action(agent_id: str, body: Evaluate, principal: Anyone):
    agent = _agent(principal, agent_id)
    if not (
        principal.can("approver") or principal.user_id == agent["principal_id"]
    ):
        raise HTTPException(
            403, "Only an approver, or the agent itself, may ask."
        )
    if body.source == "log" and not principal.can("approver"):
        # A past action is an approver's attestation, not the agent's own.
        raise HTTPException(403, "Only an approver may record a logged action.")
    mode = _mode(principal.tenant_id)
    try:
        return _judge(
            principal.context(),
            agent,
            body.model_dump(),
            body.source,
            _now(),
            mode,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except CheckFailed:
        raise HTTPException(503, CHECK_FAILED) from None


@router.get("/api/trust/agents/{agent_id}/ledger")
def get_ledger(
    agent_id: str,
    principal: Approver,
    since: datetime | None = None,
    until: datetime | None = None,
):
    agent = _agent(principal, agent_id)
    now = _now()
    try:
        since, until = ledger.window(since, until, now)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return ledger.agent_ledger(
        _store(), principal.tenant_id, agent, since=since, until=until, now=now
    )
