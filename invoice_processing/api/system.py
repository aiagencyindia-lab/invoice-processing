"""System visibility: what the platform is doing right now.

Everything here exists to answer one question that the review workbench could
not: *I uploaded an invoice — where is it?*

Ingestion and processing are deliberately separate, so an upload is stored and
queued while a worker does the extraction later. That is the right design, but
it made the middle of the pipeline invisible. A document that was received,
queued, and never picked up because nobody started a worker looked exactly like
an upload that failed — and the only way to tell was reading a log.

The endpoints below make that state legible:

* `/api/system/status` — is a worker alive, how deep is the queue, is anything
  dead-lettered, and are we on cloud or local storage.
* `/api/documents` — every received file with the stage it has reached.
* `/api/documents/{id}/retry` — put a failed document back on the queue.
* `/api/audit` — who did what, tenant-scoped.
* `/api/me` — the caller's own role, so the UI can hide what the server
  would refuse anyway.

Stages are derived, not stored. A single `status` column would need every
component to update it in lockstep and would go stale the moment one of them
crashed; reading the queue, the claim store, and the case repository instead
means the answer is always the truth as those components see it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from .auth import Principal, current_principal, require_role

router = APIRouter()

# Stages a document can be in, ordered by how far it has travelled.
QUEUED = "queued"
PROCESSING = "processing"
DONE = "done"
FAILED = "failed"
UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@router.get("/api/me")
def whoami(
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict:
    """Who the caller is, so the UI can hide what the server would refuse.

    Cosmetic only. Every endpoint enforces its own role independently, and this
    one must never become the thing that decides access -- a client is free to
    lie about what it received here, and gains nothing by it.

    The tenant is echoed rather than accepted: it comes from the verified token
    like everywhere else, and showing it lets a user notice immediately if they
    are signed into the wrong organisation.
    """
    return {
        "email": principal.email,
        "role": principal.role,
        "tenant_id": principal.tenant_id,
    }


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@router.get("/api/system/status")
def system_status(
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict:
    """Live health of the processing pipeline.

    The `worker.alive` flag is what lets the UI say "processing is paused"
    instead of leaving a user to wonder why an upload never appears. It is
    reported alongside the heartbeat age rather than as a bare boolean, because
    a worker mid-extraction on a slow model can legitimately exceed the poll
    interval -- the UI should be able to say "last seen 45s ago", not just
    "dead".
    """
    from ..worker.runner import (  # noqa: PLC0415
        HEARTBEAT_STALE_SECONDS,
        read_heartbeat,
    )
    from . import service  # noqa: PLC0415

    heartbeat = read_heartbeat()
    queue = service.get_queue()

    depth = _safe(queue.depth, default=None)
    dead = _safe(lambda: len(queue.dead_letters()), default=None)

    return {
        "worker": {
            "alive": bool(heartbeat and heartbeat["alive"]),
            "last_seen_seconds": heartbeat["age_seconds"]
            if heartbeat
            else None,
            "stale_after_seconds": HEARTBEAT_STALE_SECONDS,
            "ever_run": heartbeat is not None,
        },
        "queue": {"depth": depth, "dead_letters": dead},
        "storage": {
            # Shown so an operator can see at a glance whether this instance is
            # writing to durable cloud storage or to a container-local disk
            # that empties on restart.
            "documents": "gcs" if os.getenv("GCS_BUCKET") else "local",
            "database": "postgres"
            if service._database_configured()
            else "sqlite/local",
            "queue": "pubsub" if os.getenv("PUBSUB_TOPIC") else "local",
        },
    }


def _safe(fn, default=None):
    """Never let a status endpoint fail because one probe did.

    A dashboard that 500s when the queue is briefly unreadable is worse than
    one reporting a missing number.
    """
    try:
        return fn()
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def _queued_shas(queue) -> set[str]:
    """Content hashes currently waiting on the queue."""
    ready = getattr(queue, "ready", None)
    if ready is None or not isinstance(ready, Path):
        return set()
    shas = set()
    for path in ready.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8")).get(
                "payload", {}
            )
        except (OSError, ValueError):
            continue
        if payload.get("sha256"):
            shas.add(payload["sha256"])
    return shas


def _dead_shas(queue) -> set[str]:
    records = _safe(queue.dead_letters, default=[]) or []
    return {
        r.get("payload", {}).get("sha256")
        for r in records
        if r.get("payload", {}).get("sha256")
    }


def _claim_state(tenant_id: str, document_id: str) -> str | None:
    from .. import paths  # noqa: PLC0415
    from ..worker.runner import ClaimStore  # noqa: PLC0415

    return _safe(
        lambda: ClaimStore(paths.DATA_DIR / "claims").state(
            tenant_id, document_id
        )
    )


def document_stage(
    document: dict, tenant_id: str, queued: set[str], dead: set[str], cases
) -> tuple[str, str]:
    """Derive a stage and a plain-English explanation for one document.

    Order matters: a claim marked `running` beats "queued", because the message
    leaves the ready directory the moment a worker picks it up.
    """
    document_id = document.get("id", "")
    sha = document.get("sha256", "")

    claim = _claim_state(tenant_id, document_id)
    if claim == "running":
        return PROCESSING, "A worker is extracting this now."
    if sha in dead:
        return FAILED, "Processing failed repeatedly. Retry to try again."
    if claim == "failed":
        return FAILED, "Processing failed. Retry to try again."
    if sha in queued:
        return QUEUED, "Received and waiting for a worker to pick it up."

    artifacts = _safe(
        lambda: cases.get_artifacts(_ctx_for(tenant_id, document_id)),
        default={},
    )
    if artifacts:
        return DONE, "Processed. Open it in Invoices to review."
    if claim == "done":
        return DONE, "Processed."
    return UNKNOWN, "Received, but not queued. Retry to process it."


def _ctx_for(tenant_id: str, case_id: str):
    from ..ports import RunContext  # noqa: PLC0415

    return RunContext(tenant_id=tenant_id, case_id=case_id)


@router.get("/api/documents")
def list_documents(
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    """Every received document with the stage it has reached."""
    from . import service  # noqa: PLC0415

    documents = service.get_documents()
    listing = (
        _safe(lambda: documents.list_documents(principal.tenant_id), default=[])
        or []
    )

    queue = service.get_queue()
    queued, dead = _queued_shas(queue), _dead_shas(queue)
    cases = service.get_cases()

    out: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for document in listing:
        stage, explanation = document_stage(
            document, principal.tenant_id, queued, dead, cases
        )
        counts[stage] = counts.get(stage, 0) + 1
        out.append(
            {
                "document_id": document.get("id"),
                "filename": document.get("filename"),
                "source": document.get("source"),
                "byte_size": document.get("byte_size"),
                "sha256": document.get("sha256"),
                "received_at": document.get("created_at"),
                "stage": stage,
                "explanation": explanation,
                "case_id": document.get("id"),
            }
        )
    return {"documents": out, "counts": counts, "total": len(out)}


@router.post("/api/documents/{document_id}/retry")
def retry_document(
    document_id: str,
    principal: Annotated[Principal, Depends(require_role("approver"))],
) -> dict:
    """Put a failed or stalled document back on the queue.

    Requires approver: a retry re-runs extraction, which costs a model call and
    can change a decision, so it is not a routine clerk action.

    The claim is released first. Leaving it would let the worker's own
    idempotency guard immediately skip the message we just published -- the
    guard doing its job, but with the user watching nothing happen.
    """
    from .. import paths  # noqa: PLC0415
    from ..worker.runner import ClaimStore, enqueue_document  # noqa: PLC0415
    from . import service  # noqa: PLC0415

    documents = service.get_documents()
    listing = documents.list_documents(principal.tenant_id)
    document = next((d for d in listing if d.get("id") == document_id), None)
    if document is None:
        raise HTTPException(status_code=404, detail="Unknown document")

    ClaimStore(paths.DATA_DIR / "claims").release(
        principal.tenant_id, document_id
    )

    class _Result:
        pass

    result = _Result()
    result.document_id = document_id
    result.sha256 = document.get("sha256", "")
    result.storage_key = document.get("storage_key", "")

    message_id = enqueue_document(
        service.get_queue(), principal.context(document_id), result
    )
    service.get_audit().emit(
        principal.context(document_id),
        "document_retried",
        {"document_id": document_id, "message_id": message_id},
    )
    return {
        "document_id": document_id,
        "queued": message_id,
        "stage": QUEUED,
        "message": "Requeued. A worker will pick it up shortly.",
    }


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@router.get("/api/audit")
def list_audit(
    principal: Annotated[Principal, Depends(current_principal)],
    limit: int = 100,
) -> dict:
    """Tenant-scoped activity trail.

    Read-only for every role: explainability is the point, and a viewer who
    cannot see why an invoice was rejected cannot answer a supplier's question.
    """
    from . import service  # noqa: PLC0415

    sink = service.get_audit()
    limit = max(1, min(limit, 500))

    events = _safe(
        lambda: sink.query(principal.tenant_id, limit=limit), default=None
    )
    if events is None:
        events = _read_jsonl_audit(principal.tenant_id, limit)
    else:
        events = [
            {
                "at": e.occurred_at.isoformat() if e.occurred_at else None,
                "actor": e.actor,
                "action": e.action,
                "entity_type": e.entity_type,
                "entity_id": e.entity_id,
                "payload": e.after,
            }
            for e in events
        ]
    return {"events": events, "count": len(events)}


def _read_jsonl_audit(tenant_id: str, limit: int) -> list[dict]:
    """The local sink has no query method; read its file directly.

    Filtered by tenant even locally, so the two sinks behave the same way and a
    developer never sees a shape production would not produce.
    """
    from .. import paths  # noqa: PLC0415

    path = paths.SESSIONS_DIR / "audit.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("tenant_id") not in (None, tenant_id):
            continue
        payload = record.get("payload") or {}
        # `timestamp` is the key JsonlAuditSink.emit writes. Reading "at" here
        # silently produced a null on every local event, so the activity trail
        # rendered without a single time on it.
        entity_id = (
            record.get("case_id")
            or payload.get("case_id")
            or payload.get("document_id")
        )
        events.append(
            {
                "at": record.get("timestamp"),
                "actor": record.get("actor"),
                "action": record.get("event_type"),
                # Ingestion events carry no case yet -- the document is the
                # only thing that exists at that point in the pipeline.
                "entity_type": ("invoice" if record.get("case_id") else None)
                or ("document" if entity_id else None),
                "entity_id": entity_id,
                "payload": record.get("payload"),
            }
        )
    return list(reversed(events))[:limit]
