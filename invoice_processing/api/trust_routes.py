"""History uploads for the v2 trust layer.

A file is stored whole or not at all; every bad row is named with its row
number (the header is row 1). A re-upload of the same bytes is a no-op.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from ..trust.history import mapping as mp
from ..trust.history.readers import read_table
from .auth import Principal, require_role

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_IMPORT_BYTES = 5 * 1024 * 1024
TOO_BIG = "Import is over 5 MB."
NEEDS_DB = "History import needs a database. Configure DATABASE_URL."


def _importer():
    from . import service  # noqa: PLC0415

    try:
        importer = service.get_history_importer()
    except Exception:
        logger.exception("History imports could not be reached")
        raise HTTPException(503, "History imports are unavailable.") from None
    if importer is None:
        raise HTTPException(503, NEEDS_DB)
    return importer


async def _read(request: Request) -> tuple[bytes, str, dict]:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_IMPORT_BYTES + 64 * 1024:
        raise HTTPException(413, TOO_BIG)
    ctype = request.headers.get("content-type", "")
    if not ctype.startswith("multipart/form-data"):
        raise HTTPException(
            400, "Send multipart form data with the file as 'file'."
        )
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise HTTPException(400, "Attach the file as 'file'.")
    data = await upload.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(413, TOO_BIG)
    fields = {k: str(v) for k, v in form.items() if k != "file"}
    if fields.get("kind", "") not in mp.KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(mp.KINDS)}.")
    if not fields.get("source_name", "").strip():
        raise HTTPException(400, "source_name is required.")
    return data, upload.filename or "upload", fields


@router.post("/api/trust/history/mapping")
async def propose_mapping(
    request: Request,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
):
    importer = _importer()
    data, filename, fields = await _read(request)
    try:
        headers, _rows = read_table(data, filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    kind, source = fields["kind"], fields["source_name"].strip()
    saved = await run_in_threadpool(
        importer.saved_mapping, principal.tenant_id, source, kind
    )
    proposed = await run_in_threadpool(mp.propose, kind, headers)
    return {"headers": headers, "proposed": proposed, "saved": saved}


@router.post("/api/trust/history/import", status_code=201)
async def import_history(
    request: Request,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
):
    importer = _importer()
    data, filename, fields = await _read(request)
    try:
        mapping = json.loads(fields.get("mapping") or "{}")
    except ValueError:
        raise HTTPException(400, "mapping must be a JSON object.") from None
    if not isinstance(mapping, dict) or not all(
        isinstance(v, str) for v in mapping.values()
    ):
        raise HTTPException(
            400, "mapping must be a JSON object of column to field."
        )
    try:
        out = await run_in_threadpool(
            importer.import_file,
            principal.context(),
            kind=fields["kind"],
            source_name=fields["source_name"].strip(),
            filename=filename,
            data=data,
            mapping=mapping,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    body = out.as_dict()
    if out.duplicate_of:
        return JSONResponse(body, status_code=200)
    if out.rows_ok == 0:
        raise HTTPException(400, "The file has no rows.")
    # The importer wrote the audit row in the same transaction as the rows.
    _queue_sync(principal.tenant_id, principal.context().run_id)
    return body


def _queue_sync(tenant_id: str, request_id: str) -> None:
    """Best effort, like purchase-order re-matching: a queue outage must not
    refuse the upload."""
    try:
        from ..worker.runner import enqueue_trust_graph_sync  # noqa: PLC0415
        from . import service  # noqa: PLC0415

        enqueue_trust_graph_sync(service.get_queue(), tenant_id, request_id)
    except Exception:
        logger.warning(
            "Graph sync could not be queued after a history import",
            exc_info=True,
        )


@router.get("/api/trust/history/imports")
def list_imports(
    principal: Annotated[Principal, Depends(require_role("viewer"))],
):
    return _importer().list_imports(principal.tenant_id)


# ---------------------------------------------------------------------------
# Contracts (Wave 1): the documents the Leakage engine reads clauses from.
# Stored like any upload (content-addressed, tenant-prefixed), but kept out of
# the invoice pipeline: a contract is evaluated by the trust engines only.
# ---------------------------------------------------------------------------

NEEDS_GRAPH = "Contracts need a database. Configure DATABASE_URL."


def _graph():
    from . import service  # noqa: PLC0415

    try:
        graph = service.get_graph_store()
    except Exception:
        logger.exception("The evidence graph could not be reached")
        raise HTTPException(503, "The evidence graph is unavailable.") from None
    if graph is None:
        raise HTTPException(503, NEEDS_GRAPH)
    return graph


def _vendor_exists(tenant_id: str, vendor_id: str) -> bool:
    from sqlalchemy import select  # noqa: PLC0415

    from ..db.models import Vendor  # noqa: PLC0415
    from ..db.session import tenant_session  # noqa: PLC0415

    with tenant_session(tenant_id) as s:
        return (
            s.scalars(
                select(Vendor.id).where(
                    Vendor.tenant_id == tenant_id, Vendor.id == vendor_id
                )
            ).first()
            is not None
        )


def _store_contract(
    principal: Principal, graph, data: bytes, filename: str, vendor_id: str
):
    import hashlib  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    from . import service  # noqa: PLC0415
    from .ingest import IngestionRefused, check_acceptable, storage_key_for  # noqa: PLC0415

    try:
        check_acceptable(data, filename)
    except IngestionRefused as exc:
        raise HTTPException(400, str(exc)) from None
    tenant_id = principal.tenant_id
    if not _vendor_exists(tenant_id, vendor_id):
        raise HTTPException(404, "Unknown vendor.")
    documents = service.get_documents()
    sha256 = hashlib.sha256(data).hexdigest()
    existing = documents.find_by_sha(tenant_id, sha256)
    duplicate = existing is not None
    if duplicate:
        stored = documents.get(tenant_id, existing["id"]) or {}
        if stored.get("kind") != "contract":
            raise HTTPException(
                409, "These bytes were already received as another document."
            )
        document_id = existing["id"]
        prior_id = graph.nodes_by_ref(
            tenant_id, "documents", [document_id]
        ).get(document_id)
        prior = graph.node(tenant_id, prior_id) if prior_id else None
        if prior and prior["attrs"].get("vendor_id") not in (None, vendor_id):
            raise HTTPException(
                409, "This contract was already uploaded for another vendor."
            )
    else:
        key = storage_key_for(sha256)
        service.get_blobs(tenant_id).put(
            key, data, content_type="application/pdf"
        )
        document_id = documents.create(
            tenant_id=tenant_id,
            storage_key=key,
            sha256=sha256,
            filename=filename,
            mime_type="application/pdf",
            byte_size=len(data),
            source="upload",
            source_ref=principal.email,
            kind="contract",
        )
    ctx = principal.context(document_id)
    at = datetime.now(timezone.utc)
    node = graph.upsert_node(
        ctx,
        kind="contract",
        natural_key=f"documents:{document_id}",
        ref_table="documents",
        ref_id=document_id,
        attrs={"filename": filename, "sha256": sha256, "vendor_id": vendor_id},
        at=at,
    )
    # The vendor's node exists once a sync has run; until then the next full
    # sync draws this edge from attrs.vendor_id.
    vendor_node = graph.nodes_by_ref(tenant_id, "vendors", [vendor_id]).get(
        vendor_id
    )
    if vendor_node:
        graph.upsert_edge(
            ctx,
            src=node,
            dst=vendor_node,
            kind="governs",
            source_system="upload",
            provenance="human_assertion",
            confidence=1.0,
            observed_at=at,
            at=at,
        )
    if not duplicate:
        service.get_audit().emit(
            ctx,
            "trust_contract_uploaded",
            {
                "document_id": document_id,
                "vendor_id": vendor_id,
                "sha256": sha256,
            },
        )
    try:
        from ..worker.runner import enqueue_trust_evaluate  # noqa: PLC0415

        enqueue_trust_evaluate(
            service.get_queue(), tenant_id, document_id, ctx.run_id
        )
    except Exception:
        logger.warning("Contract evaluation could not be queued", exc_info=True)
    return {
        "document_id": document_id,
        "contract_node": node,
        "vendor_id": vendor_id,
        "duplicate": duplicate,
    }


@router.post("/api/trust/contracts", status_code=201)
async def upload_contract(
    request: Request,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
):
    from .ingest import MAX_DOCUMENT_BYTES  # noqa: PLC0415

    graph = _graph()
    too_large = HTTPException(
        413, f"Contracts are limited to {MAX_DOCUMENT_BYTES // 2**20} MB."
    )
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    # Multipart framing adds a little; anything far past the cap is refused
    # before the body is read.
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
    vendor_id = str(form.get("vendor_id") or "").strip()
    if not vendor_id:
        raise HTTPException(400, "vendor_id is required.")
    data = await upload.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise too_large
    out = await run_in_threadpool(
        _store_contract,
        principal,
        graph,
        data,
        upload.filename or "contract.pdf",
        vendor_id,
    )
    if out["duplicate"]:
        return JSONResponse(out, status_code=200)
    return out


@router.get("/api/trust/contracts")
def list_contracts(
    principal: Annotated[Principal, Depends(require_role("viewer"))],
):
    graph = _graph()
    return [
        {
            "contract_node": n["id"],
            "document_id": n["ref_id"],
            "filename": n["attrs"].get("filename"),
            "vendor_id": n["attrs"].get("vendor_id"),
            "since": n["valid_from"],
        }
        for n in graph.nodes(principal.tenant_id, kind="contract")
    ]
