"""Evidence requests for reality checks (FR-23.5, U4): ask an owner for
proof, attach it, re-check. An internal tracker: no public link, no email."""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from ..ports import StatusChanged
from ..trust.findings.model import IllegalTransition
from ..trust.reality.requests import STATUSES, RequestClosed, RequestStore
from .auth import Principal, require_role
from .trust_routes import _findings, _graph, _role_for

logger = logging.getLogger(__name__)
router = APIRouter()
ASKED = "needs_vendor_input"
# The steps a finding takes to reach "asked the vendor".
_PATH = {"detected": ("triaged", ASKED), "triaged": (ASKED,), ASKED: ()}


class EvidenceRequestIn(BaseModel):
    owner: str = Field(min_length=1, max_length=320)
    due_date: date | None = None
    note: str | None = Field(default=None, max_length=500)


def _requests() -> RequestStore:
    return RequestStore(_findings().url)


def _audit(ctx, event: str, payload: dict) -> None:
    from . import service  # noqa: PLC0415

    service.get_audit().emit(ctx, event, payload)


@router.post(
    "/api/trust/findings/{finding_id}/evidence-request", status_code=201
)
def create_evidence_request(
    finding_id: str,
    body: EvidenceRequestIn,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    store = _findings()
    tenant = principal.tenant_id
    current = store.get(tenant, finding_id)
    if current is None:
        raise HTTPException(404, "Unknown finding.")
    if not principal.can(_role_for(ASKED)):
        raise HTTPException(403, "Asking for evidence needs a clerk.")
    status = current["status"]
    if status not in _PATH:
        raise HTTPException(
            409,
            {
                "current": status,
                "message": f"A {status} finding cannot be sent for evidence.",
            },
        )
    owner = body.owner.strip()
    if not owner:
        raise HTTPException(400, "owner is required.")
    ctx = principal.context(finding_id)
    try:
        for step in _PATH[status]:
            store.transition(ctx, finding_id, step, actor=principal.email)
    except (IllegalTransition, StatusChanged):
        now = (store.get(tenant, finding_id) or {}).get("status")
        raise HTTPException(
            409, {"current": now, "message": "The finding moved meanwhile."}
        ) from None
    row = _requests().create(
        ctx,
        finding_id,
        owner=owner,
        due_date=body.due_date,
        note=body.note,
        requested_by=principal.email,
    )
    _audit(
        ctx,
        "trust_evidence_requested",
        {
            "finding_id": finding_id,
            "request_id": row["id"],
            "due_date": row["due_date"],
        },
    )
    return {**row, "finding_status": ASKED}


@router.get("/api/trust/evidence-requests")
def list_evidence_requests(
    principal: Annotated[Principal, Depends(require_role("viewer"))],
    status: str | None = None,
    overdue: bool = False,
    finding_id: str | None = None,
) -> dict:
    if status and status not in STATUSES:
        raise HTTPException(
            400, f"status must be one of {', '.join(STATUSES)}."
        )
    rows = _requests().list(
        principal.tenant_id,
        status=status,
        overdue=overdue,
        finding_id=finding_id,
    )
    return {"requests": rows}


def _open_request(principal: Principal, request_id: str) -> dict:
    row = _requests().get(principal.tenant_id, request_id)
    if row is None:
        raise HTTPException(404, "Unknown evidence request.")
    if row["status"] != "open":
        raise HTTPException(
            409, {"current": row["status"], "message": "The request is closed."}
        )
    return row


def _store_proof(
    principal: Principal, graph, data: bytes, filename: str, request_id: str
) -> dict:
    """Like `trust_routes._store_contract`: the bytes go to the blob store
    once, a `Document(kind="proof")` row names them, and the graph gets a
    `document_version` node that `evidences` the invoice."""
    from . import service  # noqa: PLC0415
    from .ingest import IngestionRefused, check_acceptable, storage_key_for  # noqa: PLC0415

    row = _open_request(principal, request_id)
    try:
        check_acceptable(data, filename)
    except IngestionRefused as exc:
        raise HTTPException(400, str(exc)) from None
    tenant = principal.tenant_id
    finding = _findings().get(tenant, row["finding_id"])
    if finding is None:  # the FK makes this unreachable; never a 500
        raise HTTPException(404, "Unknown finding.")
    documents = service.get_documents()
    sha256 = hashlib.sha256(data).hexdigest()
    existing = documents.find_by_sha(tenant, sha256)
    if existing is not None:
        stored = documents.get(tenant, existing["id"]) or {}
        if stored.get("kind") != "proof":
            raise HTTPException(
                409, "These bytes were already received as another document."
            )
        document_id = existing["id"]
    else:
        key = storage_key_for(sha256)
        service.get_blobs(tenant).put(key, data, content_type="application/pdf")
        document_id = documents.create(
            tenant_id=tenant,
            storage_key=key,
            sha256=sha256,
            filename=filename,
            mime_type="application/pdf",
            byte_size=len(data),
            source="upload",
            source_ref=principal.email,
            kind="proof",
        )
    ctx = principal.context(request_id)
    at = datetime.now(timezone.utc)
    natural_key = f"documents:{document_id}:{sha256}"
    # Provenance scores the same node later and keeps richer attrs; never
    # overwrite them with these.
    prior = graph.open_node(tenant, "document_version", natural_key)
    node = (prior or {}).get("id") or graph.upsert_node(
        ctx,
        kind="document_version",
        natural_key=natural_key,
        ref_table="documents",
        ref_id=document_id,
        attrs={"kind": "proof", "sha256": sha256, "source": "upload"},
        at=at,
    )
    invoice = finding["transaction_node"]
    graph.upsert_edge(
        ctx,
        src=node,
        dst=invoice,
        kind="evidences",
        source_system="upload",
        provenance="human_assertion",
        confidence=1.0,
        observed_at=at,
        at=at,
    )
    try:
        out = _requests().fulfil(ctx, request_id, document_id, at=at)
    except RequestClosed:
        raise HTTPException(409, "The request closed meanwhile.") from None
    _audit(
        ctx,
        "trust_evidence_fulfilled",
        {"request_id": request_id, "document_id": document_id},
    )
    invoice_doc = ((graph.node(tenant, invoice) or {}).get("attrs") or {}).get(
        "document_id"
    )
    try:
        from ..worker.runner import enqueue_trust_evaluate  # noqa: PLC0415

        queue = service.get_queue()
        for doc in dict.fromkeys(d for d in (document_id, invoice_doc) if d):
            enqueue_trust_evaluate(queue, tenant, doc, ctx.run_id)
    except Exception:
        logger.warning("Proof re-evaluation could not be queued", exc_info=True)
    return out


@router.post("/api/trust/evidence-requests/{request_id}/proof")
async def fulfil_evidence_request(
    request_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    from .ingest import MAX_DOCUMENT_BYTES  # noqa: PLC0415

    graph = _graph()
    too_large = HTTPException(
        413, f"Proofs are limited to {MAX_DOCUMENT_BYTES // 2**20} MB."
    )
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_DOCUMENT_BYTES + 64 * 1024:
        raise too_large
    if not request.headers.get("content-type", "").startswith(
        "multipart/form-data"
    ):
        raise HTTPException(
            400, "Send multipart form data with the file as 'file'."
        )
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise HTTPException(400, "Attach the file as 'file'.")
    data = await upload.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise too_large
    return await run_in_threadpool(
        _store_proof,
        principal,
        graph,
        data,
        upload.filename or "proof.pdf",
        request_id,
    )


@router.post("/api/trust/evidence-requests/{request_id}/cancel")
def cancel_evidence_request(
    request_id: str,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    ctx = principal.context(request_id)
    try:
        row = _requests().cancel(ctx, request_id)
    except KeyError:
        raise HTTPException(404, "Unknown evidence request.") from None
    except RequestClosed:
        raise HTTPException(409, "The request is closed.") from None
    _audit(ctx, "trust_evidence_cancelled", {"request_id": request_id})
    return row
