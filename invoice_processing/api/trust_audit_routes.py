"""Trust Audit routes (FR-29): engagements, batch runs, samples, the report
and the recovery ledger. Approver unless noted; no route takes a tenant."""

from __future__ import annotations

import logging
import secrets
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from ..exports import write_csv
from ..trust.audit import render, report, sampling
from ..trust.audit.store import AUDIT_STATUSES, TrustAuditStore
from ..trust.modes import ENGINES
from .auth import Principal, require_role
from .trust_routes import _findings, _graph, vendor_of

logger = logging.getLogger(__name__)
router = APIRouter()

NEEDS_DB = "Trust audits need a database. Configure DATABASE_URL."
DEFAULT_TERMS = {
    "recovered_pct": "25",
    "prevented_pct": "10",
    "cap": None,
    "minimum": None,
}
MAX_SAMPLE = 200
LEDGER_COLUMNS = [
    "finding_id",
    "type",
    "vendor",
    "to_status",
    "amount",
    "attested_by",
    "at",
    "reason",
]


def _store() -> TrustAuditStore:
    from . import service  # noqa: PLC0415

    if not service._database_configured():
        raise HTTPException(503, NEEDS_DB)
    return TrustAuditStore()


def _audit(principal: Principal, audit_id: str) -> dict:
    audit = _store().get(principal.tenant_id, audit_id)
    if audit is None:
        raise HTTPException(404, "Unknown audit.")
    return audit


def _terms(raw: dict | None) -> dict:
    """Decimal strings, validated: pct and cap/minimum non-negative."""
    out = dict(DEFAULT_TERMS)
    for key, value in (raw or {}).items():
        if key not in DEFAULT_TERMS:
            continue
        if value is None and key in ("cap", "minimum"):
            out[key] = None
            continue
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise HTTPException(400, f"terms.{key} must be a number.") from None
        if not number.is_finite() or number < 0:
            raise HTTPException(
                400, f"terms.{key} must be a non-negative number."
            )
        out[key] = str(number)
    return out


def _period(period_from: date | None, period_to: date | None) -> None:
    if period_from and period_to and period_from > period_to:
        raise HTTPException(400, "period_from must not be after period_to.")


def _scope(principal: Principal, audit: dict) -> list[dict]:
    """The findings the engagement covers, each with its vendor resolved."""
    tenant = principal.tenant_id
    graph = _graph()
    rows = sampling.scope_findings(
        audit, _findings().list(tenant), graph, tenant_id=tenant
    )
    vendors: dict = {}
    for f in rows:
        node = f["transaction_node"]
        if node not in vendors:
            vendors[node] = vendor_of(graph, tenant, node)
        f["vendor"] = vendors[node]
    return rows


# -- engagements --------------------------------------------------------------


class AuditCreate(BaseModel):
    name: str = Field(max_length=128)
    period_from: date | None = None
    period_to: date | None = None
    vendors: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    terms: dict | None = None
    approver: str | None = Field(default=None, max_length=320)


class AuditPatch(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    status: str | None = None
    period_from: date | None = None
    period_to: date | None = None
    vendors: list[str] | None = None
    categories: list[str] | None = None
    terms: dict | None = None
    approver: str | None = Field(default=None, max_length=320)


@router.post("/api/trust/audits", status_code=201)
def create_audit(
    body: AuditCreate,
    principal: Annotated[Principal, Depends(require_role("approver"))],
):
    if not body.name.strip():
        raise HTTPException(400, "name is required.")
    _period(body.period_from, body.period_to)
    store = _store()
    return store.create(
        principal.context(),
        name=body.name.strip(),
        period_from=body.period_from,
        period_to=body.period_to,
        vendors=[str(v) for v in body.vendors],
        categories=[str(c) for c in body.categories if str(c).strip()],
        terms=_terms(body.terms),
        approver=body.approver or principal.email,
    )


@router.get("/api/trust/audits")
def list_audits(
    principal: Annotated[Principal, Depends(require_role("viewer"))],
):
    return _store().list(principal.tenant_id)


@router.get("/api/trust/audits/{audit_id}")
def get_audit(
    audit_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
):
    audit = _audit(principal, audit_id)
    store = _store()
    return {
        **audit,
        "runs": store.runs(principal.tenant_id, audit_id),
        "samples": store.samples(principal.tenant_id, audit_id),
    }


@router.patch("/api/trust/audits/{audit_id}")
def patch_audit(
    audit_id: str,
    body: AuditPatch,
    principal: Annotated[Principal, Depends(require_role("approver"))],
):
    audit = _audit(principal, audit_id)
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields:
        if not (fields["name"] or "").strip():
            raise HTTPException(400, "name is required.")
        fields["name"] = fields["name"].strip()
    if "status" in fields and fields["status"] not in AUDIT_STATUSES:
        raise HTTPException(
            400, f"status must be one of {', '.join(AUDIT_STATUSES)}."
        )
    if "terms" in fields:
        fields["terms"] = _terms(fields["terms"])
    if "categories" in fields:
        fields["categories"] = [
            str(c) for c in fields["categories"] or [] if str(c).strip()
        ]
    if "vendors" in fields:
        fields["vendors"] = [str(v) for v in fields["vendors"] or []]
    _period(
        fields.get("period_from", _as_date(audit["period_from"])),
        fields.get("period_to", _as_date(audit["period_to"])),
    )
    return _store().update(principal.tenant_id, audit_id, **fields)


def _as_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


# -- runs ---------------------------------------------------------------------


class RunRequest(BaseModel):
    engines: list[str] = Field(default_factory=list)


@router.post("/api/trust/audits/{audit_id}/runs", status_code=202)
def request_run(
    audit_id: str,
    body: RunRequest,
    principal: Annotated[Principal, Depends(require_role("approver"))],
):
    audit = _audit(principal, audit_id)
    unknown = [e for e in body.engines if e not in ENGINES]
    if unknown:
        raise HTTPException(400, f"engines must be among {', '.join(ENGINES)}.")
    store, tenant = _store(), principal.tenant_id
    blocking = store.blocking_run(tenant, audit_id)
    if blocking is not None:
        raise HTTPException(
            409,
            {
                "run_id": blocking["id"],
                "status": blocking["status"],
                "message": "A run is already queued or running.",
            },
        )
    ctx = principal.context()
    run = store.create_run(
        ctx,
        audit_id,
        engines=sorted(set(body.engines), key=ENGINES.index),
        requested_by=principal.email,
    )
    if audit["status"] == "draft":
        store.update(tenant, audit_id, status="active")
    # Best effort, like a contract upload: a queue outage must not refuse
    # the request; the run stays queued until it is abandoned or re-run.
    try:
        from ..worker.runner import enqueue_trust_audit_run  # noqa: PLC0415
        from . import service  # noqa: PLC0415

        enqueue_trust_audit_run(
            service.get_queue(), tenant, run["id"], ctx.run_id
        )
    except Exception:
        logger.warning("Audit run could not be queued", exc_info=True)
    return run


@router.get("/api/trust/audits/{audit_id}/runs/{run_id}")
def get_run(
    audit_id: str,
    run_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
):
    _audit(principal, audit_id)
    run = _store().get_run(principal.tenant_id, run_id)
    if run is None or run["audit_id"] != audit_id:
        raise HTTPException(404, "Unknown run.")
    return run


# -- samples ------------------------------------------------------------------


class SampleRequest(BaseModel):
    size: int
    seed: str | None = Field(default=None, max_length=32)


class VerdictsRequest(BaseModel):
    verdicts: dict[str, bool | None]


@router.post("/api/trust/audits/{audit_id}/samples", status_code=201)
def draw_sample(
    audit_id: str,
    body: SampleRequest,
    principal: Annotated[Principal, Depends(require_role("approver"))],
):
    audit = _audit(principal, audit_id)
    if not 1 <= body.size <= MAX_SAMPLE:
        raise HTTPException(400, f"size must be between 1 and {MAX_SAMPLE}.")
    ids = [f["id"] for f in _scope(principal, audit)]
    seed = body.seed or secrets.token_hex(8)
    try:
        drawn = sampling.draw(ids, body.size, seed)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return _store().create_sample(
        principal.context(),
        audit_id,
        size=body.size,
        seed=seed,
        finding_ids=drawn,
    )


@router.patch("/api/trust/audits/{audit_id}/samples/{sample_id}")
def set_verdicts(
    audit_id: str,
    sample_id: str,
    body: VerdictsRequest,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
):
    _audit(principal, audit_id)
    store = _store()
    sample = store.get_sample(principal.tenant_id, sample_id)
    if sample is None or sample["audit_id"] != audit_id:
        raise HTTPException(404, "Unknown sample.")
    strangers = set(body.verdicts) - set(sample["finding_ids"])
    if strangers:
        raise HTTPException(400, "Every verdict must name a sampled finding.")
    return store.set_verdicts(principal.tenant_id, sample_id, body.verdicts)


# -- report + ledger ----------------------------------------------------------


def _report(principal: Principal, audit: dict) -> dict:
    from .. import tenant_settings  # noqa: PLC0415

    tenant = principal.tenant_id
    scope = _scope(principal, audit)
    events = _findings().events(tenant)
    runs = _store().runs(tenant, audit["id"])
    modes = tenant_settings.effective(tenant).get("trust_engine_modes") or {}
    return report.assemble(
        audit,
        scope,
        events,
        graph=_graph(),
        run=runs[0] if runs else None,
        now=datetime.now(timezone.utc),
        tenant_id=tenant,
        modes=modes,
        findings=_findings(),
    )


@router.get("/api/trust/audits/{audit_id}/report")
def get_report(
    audit_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
    format: str = "json",
):
    if format not in ("json", "html"):
        raise HTTPException(400, "format must be json or html.")
    out = _report(principal, _audit(principal, audit_id))
    if format == "html":
        return HTMLResponse(render.report_html(out))
    return out


@router.get("/api/trust/audits/{audit_id}/ledger")
def get_ledger(
    audit_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
    format: str = "json",
):
    if format not in ("json", "csv"):
        raise HTTPException(400, "format must be json or csv.")
    out = _report(principal, _audit(principal, audit_id))["ledger"]
    if format == "csv":
        return Response(
            content=write_csv(LEDGER_COLUMNS, out["rows"], {}),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="ledger-{audit_id}.csv"'
            },
        )
    return out
