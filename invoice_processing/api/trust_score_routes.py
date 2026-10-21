"""Trust Score, vendor trust profile, posture and engine readiness (FR-25).

The /v2 façade calls these functions by name. No route takes a tenant: each
reads the principal's, and a node of another tenant is a 404.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import Principal, require_role
from .trust_routes import _findings, _graph, _when

router = APIRouter()


class ModeChange(BaseModel):
    mode: str = Field(max_length=16)
    reason: str = Field(default="", max_length=500)


def _utc(value: str | None, name: str) -> datetime | None:
    at = _when(value, name)
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at


def _hold(tenant_id: str, invoice: dict) -> dict:
    from ..trust import gate  # noqa: PLC0415
    from . import service  # noqa: PLC0415

    case_id = invoice["attrs"].get("case_id")
    if invoice["ref_table"] != "invoices" or not case_id:
        return {
            "held": False,
            "message": None,
            "holds": [],
            "waiting": [],
            "transaction_node": invoice["id"],
        }
    return gate.payment_hold_detail(
        tenant_id, case_id, settings_store=service.get_tenant_settings()
    )


@router.get("/api/trust/transactions/{node_id}/trust")
def transaction_trust(
    node_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
    as_of: str | None = None,
) -> dict:
    from ..trust import score  # noqa: PLC0415
    from ..trust.score import dimensions as dm  # noqa: PLC0415
    from ..trust.types import DIMENSIONS  # noqa: PLC0415

    graph, store = _graph(), _findings()
    tenant = principal.tenant_id
    when = _utc(as_of, "as_of")
    invoice = graph.node(tenant, node_id)
    if invoice is None or invoice["kind"] != "invoice":
        raise HTTPException(404, "Unknown transaction.")
    # An id from before a re-sync names the same invoice; its findings
    # moved to the open node.
    current = (
        graph.open_node(tenant, "invoice", invoice["natural_key"]) or invoice
    )
    node = score.score_node(graph, tenant, current, as_of=when)
    if node is None:
        raise HTTPException(404, "This transaction has not been scored yet.")
    rows = store.list(tenant, transaction_node=current["id"])
    attrs = node["attrs"]
    if when is None:
        from .. import tenant_settings  # noqa: PLC0415
        from . import service  # noqa: PLC0415

        modes = (
            tenant_settings.effective(
                tenant, service.get_tenant_settings()
            ).get("trust_engine_modes")
            or {}
        )
        attrs = score.with_modes(attrs, rows, modes)
    by_dimension: dict[str, list] = {d: [] for d in (*DIMENSIONS, "other")}
    for f in rows:
        by_dimension[dm.dimension_of(f) or "other"].append(
            {
                "id": f["id"],
                "type": f["type"],
                "status": f["status"],
                "summary": f["summary"],
                "impact": f["impact"],
                "currency": f["currency"],
                "engine": dm.engine_of(f),
                "hold": dm.is_hold(f),
            }
        )
    return {
        **attrs,
        "node": node["id"],
        "valid_from": node["valid_from"],
        "transaction_node": current["id"],
        "hold": _hold(tenant, current),
        "findings": by_dimension,
    }


@router.get("/api/trust/vendors/{node_id}/trust")
def vendor_trust(
    node_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
) -> dict:
    from ..trust.graph.links import LinkStore  # noqa: PLC0415
    from ..trust.score import vendor  # noqa: PLC0415

    out = vendor.profile(
        principal.tenant_id,
        node_id,
        graph=_graph(),
        findings=_findings(),
        links=LinkStore(),
    )
    if out is None:
        raise HTTPException(404, "Unknown vendor.")
    return out


@router.get("/api/trust/posture")
def posture(
    principal: Annotated[Principal, Depends(require_role("viewer"))],
    since: str | None = None,
) -> dict:
    from ..trust.score import posture as posture_mod  # noqa: PLC0415

    return posture_mod.summary(
        principal.tenant_id,
        graph=_graph(),
        findings=_findings(),
        since=_utc(since, "since"),
    )


def _readiness(tenant_id: str, engines=None) -> list[dict]:
    from ..trust.modes import ENGINES  # noqa: PLC0415
    from ..trust.score import readiness as rd  # noqa: PLC0415
    from . import service  # noqa: PLC0415

    return rd.report(
        tenant_id,
        findings=_findings(),
        settings_store=service.get_tenant_settings(),
        engines=engines or ENGINES,
    )


@router.get("/api/trust/readiness")
def readiness(
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    return {"engines": _readiness(principal.tenant_id)}


@router.post("/api/trust/engines/{engine}/mode")
def set_mode(
    engine: str,
    principal: Annotated[Principal, Depends(require_role("admin"))],
    body: ModeChange | None = None,
) -> dict:
    """Never automatic (U2): an administrator moves one engine, with a
    reason, and the change is audited and the hold ledger reconciled."""
    from ..trust import modes  # noqa: PLC0415
    from . import service  # noqa: PLC0415

    _findings()  # 503 before anything is read on a deployment without it
    if body is None:
        raise HTTPException(400, "Send {mode, reason}.")
    if engine not in modes.ENGINES:
        raise HTTPException(
            404, f"engine must be one of {', '.join(modes.ENGINES)}."
        )
    if not body.reason.strip():
        raise HTTPException(400, "A reason is required to change a mode.")
    try:
        change = modes.set_engine_mode(
            principal.context(),
            engine,
            body.mode,
            store=service.get_tenant_settings(),
            audit=service.get_audit(),
            reason=body.reason.strip(),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    (row,) = _readiness(principal.tenant_id, (engine,))
    return {**row, "change": change}
