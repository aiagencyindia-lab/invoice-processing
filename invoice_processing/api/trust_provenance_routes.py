"""Vendor onboarding documents (KYC) and on-demand authenticity scores
(FR-22.3). A KYC document is stored like a contract (content-addressed,
tenant-prefixed, kept out of the invoice pipeline) and scored by the
provenance engine when `trust.evaluate` runs for it."""

from __future__ import annotations

import hashlib
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .auth import Principal, require_role
from .trust_routes import _graph, _vendor_exists

logger = logging.getLogger(__name__)
router = APIRouter()
SUBKINDS = ("gst_certificate", "pan", "udyam", "cancelled_cheque", "abn")
# Invoices (kind NULL), KYC documents and proofs carry a provenance score;
# contracts and scanned supporting documents do not.
SCORED_KINDS = (None, "kyc", "proof")


def _governed_store(
    principal: Principal,
    graph,
    data: bytes,
    filename: str,
    vendor_id: str,
    subkind: str,
) -> dict:
    """400 and 404 first, then the agent guard, then the store: a refused
    upload stores nothing. A duplicate is still one action (it ran)."""
    from ..trust.agents import guard  # noqa: PLC0415
    from . import service  # noqa: PLC0415
    from .ingest import IngestionRefused, check_acceptable  # noqa: PLC0415

    try:
        check_acceptable(data, filename)
    except IngestionRefused as exc:
        raise HTTPException(400, str(exc)) from None
    tenant_id = principal.tenant_id
    if not _vendor_exists(tenant_id, vendor_id):
        raise HTTPException(404, "Unknown vendor.")

    def build():
        vendors = service.get_vendors()
        vendor = vendors.get(tenant_id, vendor_id) if vendors else None
        return guard.document_action(
            principal.context(),
            data,
            filename,
            vendor=vendor or {"id": vendor_id},
        )

    decision = guard.check(principal, build)
    with guard.settling(principal, decision) as settle:
        out = _store(principal, graph, data, filename, vendor_id, subkind)
        settle.written()
    return out


def _store(
    principal: Principal,
    graph,
    data: bytes,
    filename: str,
    vendor_id: str,
    subkind: str,
) -> dict:
    from ..trust import provenance  # noqa: PLC0415
    from ..worker.runner import enqueue_trust_evaluate  # noqa: PLC0415
    from . import service  # noqa: PLC0415
    from .ingest import storage_key_for  # noqa: PLC0415

    tenant_id = principal.tenant_id
    documents = service.get_documents()
    sha256 = hashlib.sha256(data).hexdigest()
    existing = documents.find_by_sha(tenant_id, sha256)
    duplicate, version = existing is not None, None
    if duplicate:
        document_id = existing["id"]
        stored = documents.get(tenant_id, document_id) or {}
        if stored.get("kind") != "kyc":
            raise HTTPException(
                409, "These bytes were already received as another document."
            )
        node = graph.open_node(
            tenant_id, "document_version", f"documents:{document_id}:{sha256}"
        )
        if node is not None:  # None: the first upload stopped before its node
            attrs = node["attrs"]
            if attrs.get("vendor_id") != vendor_id:
                raise HTTPException(
                    409,
                    "This document was already uploaded for another vendor.",
                )
            if attrs.get("subkind") != subkind:
                raise HTTPException(
                    409,
                    f"This document was already uploaded as {attrs.get('subkind')}.",
                )
            version = node["id"]
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
            kind="kyc",
        )
    ctx = principal.context(document_id)
    if version is None:
        version = provenance.plant_vendor_document(
            ctx,
            graph,
            document_id=document_id,
            sha256=sha256,
            vendor_id=vendor_id,
            subkind=subkind,
            filename=filename,
            role=principal.role,
        )
    if not duplicate:
        service.get_audit().emit(
            ctx,
            "trust_vendor_document_uploaded",
            {
                "document_id": document_id,
                "vendor_id": vendor_id,
                "subkind": subkind,
                "sha256": sha256,
            },
        )
    # Queued on a duplicate too: the first upload's enqueue may have failed.
    try:
        enqueue_trust_evaluate(
            service.get_queue(), tenant_id, document_id, ctx.run_id
        )
    except Exception:
        logger.warning("KYC evaluation could not be queued", exc_info=True)
    return {
        "document_id": document_id,
        "vendor_id": vendor_id,
        "subkind": subkind,
        "version_node": version,
        "duplicate": duplicate,
    }


@router.post("/api/vendors/{vendor_id}/documents", status_code=201)
async def upload_vendor_document(
    vendor_id: str,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
    file: Annotated[UploadFile, File()],
    subkind: Annotated[str, Form()],
):
    from .ingest import MAX_DOCUMENT_BYTES  # noqa: PLC0415

    if subkind not in SUBKINDS:
        raise HTTPException(
            400, f"subkind must be one of {', '.join(SUBKINDS)}."
        )
    graph = _graph()
    data = await file.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise HTTPException(
            413, f"Documents are limited to {MAX_DOCUMENT_BYTES // 2**20} MB."
        )
    out = await run_in_threadpool(
        _governed_store,
        principal,
        graph,
        data,
        file.filename or "document.pdf",
        vendor_id,
        subkind,
    )
    if out["duplicate"]:
        return JSONResponse(out, status_code=200)
    return out


@router.get("/api/vendors/{vendor_id}/documents")
def list_vendor_documents(
    vendor_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
) -> dict:
    """{"documents": [{document_id, subkind, filename, score, uploaded_at}]}:
    the vendor's KYC documents with their latest authenticity score (None
    until scored). `uploaded_at` is the document's, not the score's."""
    from sqlalchemy import select  # noqa: PLC0415

    from ..db.models import Document  # noqa: PLC0415
    from ..db.session import tenant_session  # noqa: PLC0415

    graph = _graph()
    tenant_id = principal.tenant_id
    if not _vendor_exists(tenant_id, vendor_id):
        raise HTTPException(404, "Unknown vendor.")
    nodes = [
        n
        for n in graph.nodes(tenant_id, kind="document_version")
        if n["attrs"].get("kind") == "kyc"
        and n["attrs"].get("vendor_id") == vendor_id
    ]
    with tenant_session(tenant_id) as s:
        uploaded = dict(
            s.execute(
                select(Document.id, Document.created_at).where(
                    Document.tenant_id == tenant_id,
                    Document.id.in_([n["ref_id"] for n in nodes]),
                )
            ).all()
        )
    rows = [
        {
            "document_id": n["ref_id"],
            "subkind": n["attrs"].get("subkind"),
            "filename": n["attrs"].get("filename"),
            "score": n["attrs"].get("score"),
            "uploaded_at": uploaded[n["ref_id"]].isoformat()
            if uploaded.get(n["ref_id"])
            else None,
        }
        for n in nodes
    ]
    rows.sort(key=lambda r: (r["subkind"] or "", r["filename"] or ""))
    return {"documents": rows}


@router.post("/api/trust/documents/{document_id}/authenticity")
def score_document(
    document_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
) -> dict:
    from sqlalchemy import select  # noqa: PLC0415

    from .. import tenant_settings  # noqa: PLC0415
    from ..db.models import Invoice  # noqa: PLC0415
    from ..db.session import tenant_session  # noqa: PLC0415
    from ..trust import provenance  # noqa: PLC0415
    from ..trust.engine import EngineContext  # noqa: PLC0415
    from ..trust.evaluate import _document  # noqa: PLC0415
    from ..trust.graph.links import LinkStore  # noqa: PLC0415
    from ..trust.graph.sync import sync_tenant  # noqa: PLC0415
    from . import service  # noqa: PLC0415

    graph = _graph()
    tenant_id = principal.tenant_id
    with tenant_session(tenant_id) as s:
        document = _document(s, tenant_id, document_id)
        invoice_id = s.scalars(
            select(Invoice.id).where(
                Invoice.tenant_id == tenant_id,
                Invoice.document_id == document_id,
            )
        ).first()
    if document is None:
        raise HTTPException(404, "Unknown document.")
    if document["kind"] not in SCORED_KINDS:
        raise HTTPException(
            409, "Only invoices, vendor documents and proofs are scored."
        )
    ctx = principal.context(document_id)
    links = LinkStore(graph.url)
    invoice_node = None
    if invoice_id is not None:
        found = graph.nodes_by_ref(tenant_id, "invoices", [invoice_id])
        if invoice_id not in found:
            # Not projected yet: sync it, as trust.evaluate would.
            sync_tenant(
                ctx, graph, links, url=graph.url, document_id=document_id
            )
            found = graph.nodes_by_ref(tenant_id, "invoices", [invoice_id])
        invoice_node = found.get(invoice_id)
    if document["kind"] is None and invoice_node is None:
        raise HTTPException(409, "This document has no invoice to score yet.")
    node_id = provenance.score(
        EngineContext(
            ctx=ctx,
            mode="advisory",
            document=document,
            invoice_node=invoice_node,
            graph=graph,
            links=links,
            findings=None,
            providers=None,
            settings=tenant_settings.effective(tenant_id),
            decider=None,
            blobs=service.get_blobs(tenant_id),
            url=graph.url,
        )
    )
    if node_id is None:
        raise HTTPException(422, "The document is not a readable PDF.")
    node = graph.node(tenant_id, node_id)
    return {
        "document_id": document_id,
        "node_id": node_id,
        "valid_from": node["valid_from"],
        "attrs": node["attrs"],
    }
