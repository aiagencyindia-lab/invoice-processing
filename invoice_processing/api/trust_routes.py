"""History uploads for the v2 trust layer.

A file is stored whole or not at all; every bad row is named with its row
number (the header is row 1). A re-upload of the same bytes is a no-op.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from ..trust.graph.locate import source_of, tax_id, vendor_of
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
    from datetime import datetime  # noqa: PLC0415

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
    at = datetime.now(UTC)
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


# ---------------------------------------------------------------------------
# Findings, nodes, lineage, contract files and clauses (Wave 2 scaffold).
# Every surface in Wave 2 reads through these. `source_of` and `vendor_of`
# live in `trust/graph/locate.py` so audit and explain never import the API.
# ---------------------------------------------------------------------------

NEEDS_FINDINGS = "Findings need a database. Configure DATABASE_URL."
OPEN_STATUSES = ("detected", "triaged", "needs_vendor_input")
MONEY_STATUSES = frozenset(
    {"accepted", "rejected", "claimed", "recovered", "prevented", "written_off"}
)
LEDGER_STATUSES = frozenset({"recovered", "prevented", "written_off"})
CLAUSE_TYPES = ("rate", "escalator", "tier", "rebate", "discount", "term")
MAX_PAGE = 200
# The ledger column is Numeric(18, 4): fourteen integer digits.
MAX_AMOUNT = Decimal("1e14")


def _findings():
    from . import service  # noqa: PLC0415

    try:
        store = service.get_finding_store()
    except Exception:
        logger.exception("Findings could not be reached")
        raise HTTPException(503, "Findings are unavailable.") from None
    if store is None:
        raise HTTPException(503, NEEDS_FINDINGS)
    return store


def _role_for(status: str) -> str:
    """Clerks triage; the statuses that move money need an approver."""
    return "approver" if status in MONEY_STATUSES else "clerk"


def _when(value: str | None, name: str):
    from datetime import datetime  # noqa: PLC0415

    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            400, f"{name} must be an ISO-8601 datetime."
        ) from None


def _engine_types(engine: str | None, hold: bool) -> set[str] | None:
    from ..trust.findings import model as fm  # noqa: PLC0415
    from ..trust.modes import ENGINES  # noqa: PLC0415

    types: set[str] | None = None
    if engine:
        if engine not in ENGINES and engine != "trust":
            raise HTTPException(
                400, f"engine must be one of {', '.join(ENGINES)}."
            )
        types = {t for t, e in fm.TYPE_ENGINE.items() if e == engine}
    if hold:
        types = fm.HOLD_TYPES if types is None else types & fm.HOLD_TYPES
    return types


def _row(graph, tenant_id: str, f: dict, vendors: dict) -> dict:
    from ..trust.findings import model as fm  # noqa: PLC0415

    node = f["transaction_node"]
    if node not in vendors:
        vendors[node] = vendor_of(graph, tenant_id, node)
    details = f.get("details") or {}
    return {
        **f,
        "vendor": vendors[node],
        "engine": details.get("engine") or fm.TYPE_ENGINE.get(f["type"]),
        "hold": bool(details.get("hold", f["type"] in fm.HOLD_TYPES)),
    }


@router.get("/api/trust/findings")
def list_findings(
    principal: Annotated[Principal, Depends(require_role("viewer"))],
    status: str | None = None,
    open: bool = False,
    type: str | None = None,
    engine: str | None = None,
    vendor: str | None = None,
    hold: bool = False,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    from ..trust.findings.model import STATUSES  # noqa: PLC0415

    store, graph = _findings(), _graph()
    if status and status not in STATUSES:
        raise HTTPException(
            400, f"status must be one of {', '.join(STATUSES)}."
        )
    types = _engine_types(engine, hold)
    if type:
        types = {type} if types is None else types & {type}
    statuses = set(OPEN_STATUSES) if open else None
    if status:
        statuses = {status} if statuses is None else statuses & {status}
    if (types is not None and not types) or (
        statuses is not None and not statuses
    ):
        return {"findings": [], "total": 0, "offset": offset, "limit": limit}
    filters = {
        "statuses": sorted(statuses) if statuses is not None else None,
        "types": sorted(types) if types is not None else None,
        "vendor_node": vendor,
        "since": _when(since, "since"),
        "until": _when(until, "until"),
    }
    limit = max(1, min(limit, MAX_PAGE))
    offset = max(0, offset)
    tenant = principal.tenant_id
    rows = store.list(tenant, limit=limit, offset=offset, **filters)
    vendors: dict = {}
    return {
        "findings": [_row(graph, tenant, f, vendors) for f in rows],
        "total": store.count(tenant, **filters),
        "offset": offset,
        "limit": limit,
    }


@router.get("/api/trust/findings/summary")
def findings_summary(
    principal: Annotated[Principal, Depends(require_role("viewer"))],
):
    from ..trust.findings import model as fm  # noqa: PLC0415

    store, graph = _findings(), _graph()
    tenant = principal.tenant_id
    out = store.summary(tenant)
    for v in out["vendors"]:
        node = graph.node(tenant, v["node"])
        attrs = (node or {}).get("attrs", {})
        v["name"], v["tax_id"] = attrs.get("name"), tax_id(attrs)
    out["hold_open"] = store.count(
        tenant, statuses=OPEN_STATUSES, types=sorted(fm.HOLD_TYPES)
    )
    return out


@router.get("/api/trust/findings/{finding_id}")
def get_finding(
    finding_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
):
    from ..trust.findings.model import TRANSITIONS  # noqa: PLC0415
    from ..trust.graph.lineage import lineage  # noqa: PLC0415
    from ..trust.graph.links import LinkStore  # noqa: PLC0415

    store, graph = _findings(), _graph()
    tenant = principal.tenant_id
    f = store.get(tenant, finding_id)
    if f is None:
        raise HTTPException(404, "Unknown finding.")
    out = _row(graph, tenant, f, {})
    for c in out["citations"]:
        node = graph.node(tenant, c["node_id"])
        c["node"] = node
        c["source"] = source_of(graph, tenant, node)
    txn = graph.node(tenant, f["transaction_node"])
    out["transaction"] = {"node": txn, "source": source_of(graph, tenant, txn)}
    try:
        out["lineage"] = [
            link.__dict__
            for link in lineage(
                tenant, f["transaction_node"], graph=graph, links=LinkStore()
            )
        ]
    except KeyError:
        out["lineage"] = []
    out["allowed_transitions"] = sorted(TRANSITIONS.get(f["status"], ()))
    return out


class TransitionRequest(BaseModel):
    to_status: str
    reason: str | None = Field(default=None, max_length=500)
    amount: str | None = None
    expect: str | None = None


@router.post("/api/trust/findings/{finding_id}/transition")
def transition_finding(
    finding_id: str,
    request: TransitionRequest,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
):
    from decimal import InvalidOperation  # noqa: PLC0415

    from ..ports import StatusChanged  # noqa: PLC0415
    from ..trust.findings.model import STATUSES, IllegalTransition  # noqa: PLC0415

    store = _findings()
    to_status = request.to_status
    if to_status not in STATUSES:
        raise HTTPException(
            400, f"to_status must be one of {', '.join(STATUSES)}."
        )
    if not principal.can(_role_for(to_status)):
        raise HTTPException(
            403, f"Marking a finding {to_status} needs an approver."
        )
    tenant = principal.tenant_id
    current = store.get(tenant, finding_id)
    if current is None:
        raise HTTPException(404, "Unknown finding.")
    amount = None
    if request.amount is not None:
        try:
            amount = Decimal(request.amount)
        except (InvalidOperation, ValueError):
            raise HTTPException(400, "amount must be a number.") from None
        if not amount.is_finite() or amount < 0 or amount >= MAX_AMOUNT:
            raise HTTPException(
                400, "amount must be a non-negative number below 10^14."
            )
    elif to_status in LEDGER_STATUSES and current.get("impact") is not None:
        # The ledger never falls back: what was recovered is, by default,
        # what the finding said was at stake.
        amount = Decimal(current["impact"])
    try:
        return store.transition(
            principal.context(finding_id),
            finding_id,
            to_status,
            actor=principal.email,
            reason=request.reason,
            amount=amount,
            expect=request.expect,
        )
    except IllegalTransition as exc:  # a ValueError: caught first
        raise HTTPException(
            409, {"current": current["status"], "message": str(exc)}
        ) from None
    except StatusChanged as exc:
        raise HTTPException(
            409,
            {"current": exc.status, "message": "The finding moved meanwhile."},
        ) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("/api/trust/nodes/{node_id}")
def get_node(
    node_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
):
    graph = _graph()
    tenant = principal.tenant_id
    node = graph.node(tenant, node_id)
    if node is None:
        raise HTTPException(404, "Unknown node.")
    return {
        "node": node,
        "source": source_of(graph, tenant, node),
        "edges_out": graph.edges(tenant, src=node_id)[:50],
        "edges_in": graph.edges(tenant, dst=node_id)[:50],
    }


@router.get("/api/trust/lineage/{node_id}")
def get_lineage(
    node_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
    as_of: str | None = None,
):
    from ..trust.graph.lineage import lineage  # noqa: PLC0415
    from ..trust.graph.links import LinkStore  # noqa: PLC0415

    graph = _graph()
    try:
        links = lineage(
            principal.tenant_id,
            node_id,
            graph=graph,
            links=LinkStore(),
            as_of=_when(as_of, "as_of"),
        )
    except KeyError:
        raise HTTPException(404, "Unknown node.") from None
    return {"node_id": node_id, "links": [link.__dict__ for link in links]}


def _contract_document(tenant_id: str, document_id: str) -> dict:
    from . import service  # noqa: PLC0415

    doc = service.get_documents().get(tenant_id, document_id)
    if doc is None or doc.get("kind") != "contract":
        raise HTTPException(404, "Unknown contract.")
    return doc


@router.get("/api/trust/contracts/{document_id}/file")
def contract_file(
    document_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
):
    from fastapi.responses import Response  # noqa: PLC0415

    from . import service  # noqa: PLC0415

    doc = _contract_document(principal.tenant_id, document_id)
    try:
        data = service.get_blobs(principal.tenant_id).get(doc["storage_key"])
    except Exception as exc:  # a store's own NotFound is not an OSError
        logger.warning(
            "trust_contract_blob_missing", extra={"error": type(exc).__name__}
        )
        raise HTTPException(404, "The contract file is not stored.") from None
    return Response(content=data, media_type="application/pdf")


def _clause_dict(n: dict) -> dict:
    a = n.get("attrs") or {}
    return {
        "node_id": n["id"],
        "type": a.get("type"),
        "params": a.get("params") or {},
        "page": a.get("page"),
        "excerpt": a.get("excerpt"),
        "confidence": a.get("confidence"),
        "reviewed": bool(a.get("reviewed")),
        "since": n.get("valid_from"),
    }


@router.get("/api/trust/contracts/{document_id}/clauses")
def contract_clauses(
    document_id: str,
    principal: Annotated[Principal, Depends(require_role("viewer"))],
):
    graph = _graph()
    tenant = principal.tenant_id
    contract = graph.nodes_by_ref(tenant, "documents", [document_id]).get(
        document_id
    )
    if contract is None:
        raise HTTPException(404, "Unknown contract.")
    clauses = [
        graph.node(tenant, e["src"])
        for e in graph.edges(tenant, dst=contract, kind="part_of")
    ]
    clauses = [n for n in clauses if n and n["kind"] == "contract_clause"]
    clauses.sort(key=lambda n: (n["attrs"].get("page") or 0, n["natural_key"]))
    return [_clause_dict(n) for n in clauses]


class ClauseEdit(BaseModel):
    type: str | None = None
    params: dict | None = None


@router.patch("/api/trust/clauses/{node_id}")
def review_clause(
    node_id: str,
    edit: ClauseEdit,
    principal: Annotated[Principal, Depends(require_role("approver"))],
):
    from datetime import datetime  # noqa: PLC0415

    from . import service  # noqa: PLC0415

    graph = _graph()
    tenant = principal.tenant_id
    node = graph.node(tenant, node_id)
    if node is None or node["kind"] != "contract_clause":
        raise HTTPException(404, "Unknown clause.")
    if node.get("valid_to") is not None:
        # A stale id (a second tab, a double submit): review the open version
        # of the same clause, not a copy of what the stale one said.
        node = graph.open_node(tenant, "contract_clause", node["natural_key"])
        if node is None:
            raise HTTPException(404, "Unknown clause.")
        node_id = node["id"]
    if edit.type is not None and edit.type not in CLAUSE_TYPES:
        raise HTTPException(
            400, f"type must be one of {', '.join(CLAUSE_TYPES)}."
        )
    attrs = dict(node["attrs"])
    if edit.type is not None:
        attrs["type"] = edit.type
    if edit.params is not None:
        attrs["params"] = {str(k): str(v) for k, v in edit.params.items()}
    attrs["reviewed"] = True
    ctx = principal.context()
    now = datetime.now(UTC)
    # A reviewed clause is a new version; the old row keeps what the engine
    # read. Re-versioning closes the clause's edges, so they are redrawn.
    out_edges = graph.edges(tenant, src=node_id)
    in_edges = graph.edges(tenant, dst=node_id)
    new_id = graph.upsert_node(
        ctx,
        kind="contract_clause",
        natural_key=node["natural_key"],
        attrs=attrs,
        natural_keys=node.get("natural_keys") or None,
        at=now,
    )
    if new_id != node_id:
        for e in out_edges + in_edges:
            graph.upsert_edge(
                ctx,
                src=new_id if e["src"] == node_id else e["src"],
                dst=new_id if e["dst"] == node_id else e["dst"],
                kind=e["kind"],
                source_system=e["source_system"],
                provenance=e["provenance"],
                confidence=e["confidence"],
                observed_at=now,
                attrs=e.get("attrs"),
                at=now,
            )
        service.get_audit().emit(
            ctx,
            "trust_clause_reviewed",
            {"clause": new_id, "previous": node_id, "type": attrs.get("type")},
        )
    return _clause_dict(graph.node(tenant, new_id))
