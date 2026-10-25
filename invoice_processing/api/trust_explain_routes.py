"""The Why engine over HTTP (FR-24): a cited explanation, the auditor's
evidence pack as JSON or a print page, and reviewer ratings.

No model is called here or below; a template that names a node the finding
never cited is a bug, surfaced as a 500 and an `explain_uncited` log line,
never as an explanation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..trust.explain.engine import build
from ..trust.explain.model import Uncited
from ..trust.explain.pack import evidence_pack
from ..trust.explain.ratings import RatingStore
from ..trust.explain.render import pack_html
from ..trust.graph.links import LinkStore
from .auth import Principal, require_role
from .trust_routes import _findings, _graph

logger = logging.getLogger(__name__)
router = APIRouter()


def _finding(store, tenant_id: str, finding_id: str) -> dict:
    f = store.get(tenant_id, finding_id)
    if f is None:
        raise HTTPException(404, "Unknown finding.")
    return f


def _uncited(finding: dict, exc: Uncited):
    logger.error(
        "explain_uncited",
        extra={"type": finding["type"], "reason": str(exc).split(":")[0]},
    )
    raise HTTPException(500, "The explanation could not be built.") from None


@router.get("/api/trust/findings/{finding_id}/explanation")
def get_explanation(
    finding_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
):
    store, graph = _findings(), _graph()
    f = _finding(store, principal.tenant_id, finding_id)
    try:
        return build(principal.tenant_id, f, graph=graph).as_dict()
    except Uncited as exc:
        _uncited(f, exc)


@router.get("/api/trust/findings/{finding_id}/evidence-pack")
def get_evidence_pack(
    finding_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
    format: str = "json",
):
    if format not in ("json", "html"):
        raise HTTPException(400, "format must be json or html.")
    store, graph = _findings(), _graph()
    tenant = principal.tenant_id
    f = _finding(store, tenant, finding_id)
    try:
        pack = evidence_pack(
            tenant,
            f,
            graph=graph,
            findings=store,
            ratings=RatingStore(store.url).list(tenant, finding_id),
            links=LinkStore(),
            now=datetime.now(UTC),
        )
    except Uncited as exc:
        _uncited(f, exc)
    if format == "json":
        return pack
    return HTMLResponse(
        pack_html(pack),
        headers={
            "Content-Disposition": (
                f'inline; filename="evidence-pack-{finding_id}.html"'
            )
        },
    )


class RatingRequest(BaseModel):
    correct: bool
    useful: bool
    note: str | None = Field(default=None, max_length=500)


@router.post("/api/trust/findings/{finding_id}/rating")
def rate_explanation(
    finding_id: str,
    request: RatingRequest,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
):
    store = _findings()
    try:
        return RatingStore(store.url).rate(
            principal.context(finding_id),
            finding_id,
            correct=request.correct,
            useful=request.useful,
            note=request.note,
        )
    except KeyError:
        raise HTTPException(404, "Unknown finding.") from None


@router.get("/api/trust/explanations/ratings")
def ratings_summary(
    principal: Annotated[Principal, Depends(require_role("viewer"))],
):
    store = _findings()
    return {"by_type": RatingStore(store.url).aggregate(principal.tenant_id)}
