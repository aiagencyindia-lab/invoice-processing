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
