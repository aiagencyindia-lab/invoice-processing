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
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from .auth import Principal, current_principal, require_role

logger = logging.getLogger(__name__)

router = APIRouter()

# Stages a document can be in, ordered by how far it has travelled.
QUEUED = "queued"
PROCESSING = "processing"
DONE = "done"
FAILED = "failed"
UNKNOWN = "unknown"
# A scanned PO or receipt waits for a person rather than a worker.
AWAITING_CONFIRMATION = "awaiting_confirmation"
BLOCKED = "blocked"
# Accepted, but the PO or receipt was never written (a crash in between).
ACCEPTED_UNSTORED = "accepted_unstored"


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

    depth = _probe(queue, "depth")
    letters = _probe(queue, "dead_letters")
    dead = len(letters) if letters is not None else None

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


def _probe(obj, name: str, default=None):
    """Call `obj.name()`, surviving a missing attribute as well as a failure.

    `_safe(queue.depth, ...)` looked the attribute up before `_safe` could
    guard anything, so a queue implementation without it took the whole status
    endpoint down with an AttributeError -- the one thing this endpoint exists
    to survive. Passing the name keeps the lookup inside the guard.
    """
    try:
        return getattr(obj, name)()
    except Exception:
        return default


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
    records = _probe(queue, "dead_letters", default=[]) or []
    return {
        r.get("payload", {}).get("sha256")
        for r in records
        if r.get("payload", {}).get("sha256")
    }


def _claim_state(tenant_id: str, document_id: str) -> str | None:
    from . import service  # noqa: PLC0415

    return _safe(
        lambda: service.get_claim_store().state(tenant_id, document_id)
    )


def document_stage(
    document: dict,
    tenant_id: str,
    queued: set[str],
    dead: set[str],
    cases,
    already_read: tuple[dict, dict | None] | None = None,
) -> tuple[str, str, dict | None]:
    """Derive a stage, a plain-English explanation and the pipeline progress.

    Order matters: a claim marked `running` beats "queued", because the message
    leaves the ready directory the moment a worker picks it up.
    """
    document_id = document.get("id", "")
    sha = document.get("sha256", "")

    from . import service  # noqa: PLC0415

    # The caller listing a whole page reads every case in one query and hands
    # the result in; a lone caller reads its own.
    artifacts, internal = already_read or _safe(
        lambda: service.read_case(cases, _ctx_for(tenant_id, document_id)),
        default=({}, {}),
    )
    progress = internal.get("progress")
    attempts = internal.get("attempts") or {}

    claim = _claim_state(tenant_id, document_id)
    if claim == "running":
        return PROCESSING, _progress_sentence(progress), progress
    # A finished run outranks a dead-letter record. Artifacts are written only
    # once a run completes, and the record of the attempt that failed before it
    # is never cleared -- so a document that was retried successfully went on
    # reading as "processing failed repeatedly" while its invoice sat in the
    # workbench waiting to be approved.
    if artifacts:
        return DONE, "Processed. Open it in Invoices to review.", progress
    if claim == "done":
        return DONE, "Processed.", progress
    if sha in dead or attempts.get("exhausted"):
        # `dead` is empty on Pub/Sub -- it has no dead-letter topic here, so a
        # document the worker gave up on would otherwise read as "received but
        # never queued" and offer no explanation at all.
        return (
            FAILED,
            "Processing failed repeatedly. Retry to try again.",
            progress,
        )
    if claim == "failed":
        return FAILED, "Processing failed. Retry to try again.", progress
    if sha in queued:
        return (
            QUEUED,
            "Received and waiting for a worker to pick it up.",
            progress,
        )

    if _in_flight(progress):
        # No claim, no artifacts, but the worker has reported a step: it is
        # working on this right now in another container. The claim store is
        # local disk, so the API never sees the claim a deployed worker holds
        # -- which is how an invoice mid-extraction came to be reported as
        # "received, but not queued".
        return PROCESSING, _progress_sentence(progress), progress
    return UNKNOWN, "Received, but not queued. Retry to process it.", progress


# How long a step may stand before it stops meaning "in progress". Long,
# because Vertex occasionally hangs for twenty minutes and a document reported
# as stalled offers a Retry that would duplicate work still under way. A worker
# that dies mid-invoice leaves the document unknown-and-retryable after this.
IN_FLIGHT_SECONDS = 30 * 60


def _in_flight(progress: dict | None) -> bool:
    """Whether a reported step is recent enough to still mean "working"."""
    if not progress:
        return False
    at = progress.get("at")
    if not isinstance(at, (int, float)):
        # Written before steps were timestamped: trust it, the alternative is
        # calling a live extraction stalled.
        return True
    return (time.time() - at) <= IN_FLIGHT_SECONDS


def _progress_sentence(progress: dict | None) -> str:
    """What the worker is doing, when it has told us."""
    if not progress:
        return "A worker is extracting this now."
    return (
        f"Step {progress.get('step')} of {progress.get('total')}: "
        f"{progress.get('label')}."
    )


def _review_stage(row: dict, stored: bool = True) -> tuple[str, str]:
    """Stage and explanation of a scanned PO or receipt, from its review."""
    status = row.get("status")
    if status == "blocked":
        return BLOCKED, f"Blocked: {row.get('blocked_reason')}"
    if status == "accepted" and not stored:
        return (
            ACCEPTED_UNSTORED,
            "Accepted but nothing was stored; an admin can reopen it.",
        )
    if status == "accepted":
        return DONE, "Confirmed."
    if status == "rejected":
        return DONE, "Rejected."
    return AWAITING_CONFIRMATION, "Awaiting confirmation on Purchase orders."


def _reviews(tenant_id: str, listing: list[dict]) -> dict[str, dict]:
    """Review rows of the scanned documents in `listing`, by document id."""
    scanned = [
        str(d.get("id"))
        for d in listing
        if d.get("kind") not in (None, "invoice")
    ]
    if not scanned:
        return {}
    from . import service  # noqa: PLC0415

    rows = _safe(
        lambda: service.get_supporting_documents().for_documents(
            tenant_id, scanned
        ),
        default=[],
    )
    return {r["document_id"]: r for r in rows or []}


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

    # One read for the whole page. A read per document is invisible against a
    # local file and costs a round trip each against Cloud SQL, which is what
    # made this list take five seconds and grow slower with every upload.
    from . import service  # noqa: PLC0415

    read = _safe(
        lambda: service.read_cases(
            cases,
            principal.tenant_id,
            [str(d.get("id") or "") for d in listing],
        ),
        default={},
    )

    reviews = _reviews(principal.tenant_id, listing)
    from .supporting_routes import _stored  # noqa: PLC0415

    accepted = [r for r in reviews.values() if r.get("status") == "accepted"]
    # Unknown reads as stored: the reopen route checks again for itself.
    stored = _safe(
        lambda: (
            _stored(
                service.get_purchase_orders(), principal.tenant_id, accepted
            )
            if accepted
            else set()
        ),
        default=None,
    )

    out: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for document in listing:
        review = reviews.get(str(document.get("id") or ""))
        if review is not None:
            (stage, explanation), progress = (
                _review_stage(
                    review, stored is None or review["document_id"] in stored
                ),
                None,
            )
        else:
            stage, explanation, progress = document_stage(
                document,
                principal.tenant_id,
                queued,
                dead,
                cases,
                read.get(str(document.get("id") or "")),
            )
        counts[stage] = counts.get(stage, 0) + 1
        out.append(
            {
                "document_id": document.get("id"),
                "filename": document.get("filename"),
                "kind": document.get("kind"),
                "source": document.get("source"),
                "byte_size": document.get("byte_size"),
                "sha256": document.get("sha256"),
                "received_at": document.get("created_at"),
                "stage": stage,
                "explanation": explanation,
                "progress": progress,
                "case_id": document.get("id"),
            }
        )
    return {"documents": out, "counts": counts, "total": len(out)}


def _sent_to_invoices(tenant_id: str, document_id: str) -> bool:
    """Whether a clerk sent this scan to the invoice queue.

    Not `_safe`: an unreadable store is not "no", and an unforced retry of a
    document a clerk sent over would divert it back to review.
    """
    from . import service  # noqa: PLC0415
    from .supporting_routes import SENT_TO_INVOICES  # noqa: PLC0415

    try:
        store = service.get_supporting_documents()
        # None: a database without the table has never held a scan.
        rows = store.for_documents(tenant_id, [document_id]) if store else []
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Could not check whether this document was sent to the "
            "invoice queue; try again.",
        ) from exc
    return any(r.get("note") == SENT_TO_INVOICES for r in rows)


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
    from ..worker.runner import enqueue_document  # noqa: PLC0415
    from . import service  # noqa: PLC0415

    documents = service.get_documents()
    listing = documents.list_documents(principal.tenant_id)
    document = next((d for d in listing if d.get("id") == document_id), None)
    if document is None:
        raise HTTPException(status_code=404, detail="Unknown document")

    # Classified an invoice, or sent to the invoice queue by a clerk (even if
    # marking its kind failed): never diverted to review again. Asked before
    # the claim is released, so a failed check leaves the document as it was.
    force_invoice = document.get("kind") == "invoice" or _sent_to_invoices(
        principal.tenant_id, document_id
    )
    service.get_claim_store().release(principal.tenant_id, document_id)

    try:
        message_id = enqueue_document(
            service.get_queue(),
            principal.context(document_id),
            SimpleNamespace(
                document_id=document_id,
                sha256=document.get("sha256", ""),
                storage_key=document.get("storage_key", ""),
            ),
            force_invoice=force_invoice,
        )
    except Exception:
        # A timed-out publish may still arrive later; a second retry is safe
        # (the worker's claim makes it a no-op once the first one runs).
        logger.exception("%s not requeued", document_id)
        raise HTTPException(
            503, "The document could not be queued; retry it."
        ) from None
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


def _parse_cursor(cursor: str) -> tuple[datetime, str]:
    stamp, sep, event_id = cursor.rpartition("|")
    try:
        if not sep or not event_id:
            raise ValueError
        # Pasted unencoded into a URL, "+00:00" arrives as " 00:00".
        return datetime.fromisoformat(stamp.replace(" ", "+")), event_id
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="cursor must be the next_cursor of a previous page.",
        ) from None


def _audit_dict(e) -> dict:
    """One shape for the list, the export and the local fallback."""
    return {
        "id": e.id,
        "at": e.occurred_at.isoformat() if e.occurred_at else None,
        "actor": e.actor,
        "actor_type": e.actor_type,
        "action": e.action,
        "entity_type": e.entity_type,
        "entity_id": e.entity_id,
        "payload": e.after,
        "before": e.before,
        "request_id": e.request_id,
        "ip": e.ip,
    }


@router.get("/api/audit")
def list_audit(
    principal: Annotated[Principal, Depends(current_principal)],
    limit: int = 100,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: str | None = None,
) -> dict:
    """Tenant-scoped activity trail.

    Read-only for every role: explainability is the point, and a viewer who
    cannot see why an invoice was rejected cannot answer a supplier's question.
    `since`/`until` bound the window; `cursor` is the previous page's
    `next_cursor`.
    """
    from . import service  # noqa: PLC0415

    # A naive bound means UTC everywhere: Postgres would read it in the
    # session's zone, the local file already reads it as UTC.
    since = _as_utc(since) if since is not None else None
    until = _as_utc(until) if until is not None else None
    before = _parse_cursor(cursor) if cursor else None
    sink = service.get_audit()
    limit = max(1, min(limit, 500))

    rows = None
    if hasattr(sink, "query"):
        try:
            rows = sink.query(
                principal.tenant_id,
                limit=limit,
                since=since,
                until=until,
                before=before,
            )
        except Exception:
            # The local file cannot honour a cursor; answering page 1 again
            # would loop a paging client forever.
            if before is not None:
                raise HTTPException(
                    status_code=503,
                    detail="Paging the audit trail needs a database that could not be reached.",
                ) from None
    if rows is None:
        events = _read_jsonl_audit(principal.tenant_id, limit, since, until)
    else:
        events = [_audit_dict(e) for e in rows]
    next_cursor = (
        f"{events[-1]['at']}|{events[-1]['id']}"
        if rows is not None and len(events) == limit
        else None
    )
    return {"events": events, "count": len(events), "next_cursor": next_cursor}


_CSV_COLUMNS = (
    "id",
    "occurred_at",
    "actor",
    "actor_type",
    "action",
    "entity_type",
    "entity_id",
    "request_id",
    "ip",
    "before",
    "after",
)
_EXPORT_PAGE = 1000


def _cell(value: Any) -> str:
    """One CSV cell, safe to open in a spreadsheet: a leading = + - @ is a
    formula there, so it is prefixed with a quote."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/api/audit/export")
def export_audit(
    principal: Annotated[Principal, Depends(require_role("admin"))],
    since: datetime,
    until: datetime,
):
    """The trail for a window, as the file an auditor asks for.

    The export is itself an event, written before the first byte goes out:
    a dropped connection must still leave the record that someone pulled it.
    """
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415

    from fastapi.responses import StreamingResponse  # noqa: PLC0415

    from . import service  # noqa: PLC0415

    since, until = _as_utc(since), _as_utc(until)  # as list_audit does
    sink = service.get_audit()

    def query(before):
        return sink.query(
            principal.tenant_id,
            limit=_EXPORT_PAGE,
            since=since,
            until=until,
            before=before,
        )

    # The first page is read here, before the response starts: a streamed
    # response cannot change its status, so a dead database must fail now.
    # A query that raises is never answered from the local file -- that would
    # hand an auditor a different, partial trail without saying so.
    first = None
    if hasattr(sink, "query"):
        try:
            first = query(None)
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="Exporting the audit trail needs a database that could not be reached.",
            ) from None

    # Recorded once the data is known to be readable, still before any byte
    # goes out: a refused export is not a pull, a dropped download still is.
    try:
        sink.emit(
            principal.context(),
            "audit_exported",
            {"since": since.isoformat(), "until": until.isoformat()},
        )
    except Exception:
        # An export that cannot be recorded is not handed out.
        raise HTTPException(
            status_code=503,
            detail="Exporting the audit trail needs a database to record the export, and it could not be reached.",
        ) from None

    def rows():
        if first is None:  # local JSONL sink: one slice, no paging
            yield from _read_jsonl_audit(
                principal.tenant_id, None, since, until
            )
            return
        page = first
        while True:
            for e in page:
                yield _audit_dict(e)
            if len(page) < _EXPORT_PAGE:
                return
            try:
                page = query((page[-1].occurred_at, page[-1].id))
            except Exception:
                # Not caught into a clean end: a truncated file that looks
                # complete is worse than a download that visibly fails.
                logger.exception("Audit export failed after the first page")
                raise

    def stream():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(_CSV_COLUMNS)
        yield buffer.getvalue()
        for event in rows():
            buffer.seek(0)
            buffer.truncate()
            writer.writerow(
                _cell(v)
                for v in (
                    event["id"],
                    event["at"],
                    event["actor"],
                    event["actor_type"],
                    event["action"],
                    event["entity_type"],
                    event["entity_id"],
                    event["request_id"],
                    event["ip"],
                    json.dumps(event["before"], default=str)
                    if event["before"] is not None
                    else None,
                    json.dumps(event["payload"], default=str)
                    if event["payload"] is not None
                    else None,
                )
            )
            yield buffer.getvalue()

    name = f"audit-{since.date().isoformat()}-{until.date().isoformat()}.csv"
    return StreamingResponse(
        stream(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _as_utc(stamp: datetime) -> datetime:
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp


def _read_jsonl_audit(
    tenant_id: str,
    limit: int | None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
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
        if since is not None or until is not None:
            try:
                when = _as_utc(datetime.fromisoformat(record["timestamp"]))
            except (KeyError, TypeError, ValueError):
                continue
            if (since is not None and when < _as_utc(since)) or (
                until is not None and when > _as_utc(until)
            ):
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
                "id": None,
                "at": record.get("timestamp"),
                "actor": record.get("actor"),
                "actor_type": "user" if record.get("actor") else "system",
                "action": record.get("event_type"),
                # Ingestion events carry no case yet -- the document is the
                # only thing that exists at that point in the pipeline.
                "entity_type": ("invoice" if record.get("case_id") else None)
                or ("document" if entity_id else None),
                "entity_id": entity_id,
                "payload": record.get("payload"),
                "before": record.get("before"),
                "request_id": record.get("run_id") or None,
                "ip": None,
            }
        )
    # ponytail: the local file is one developer's trail; pages by slice only.
    return list(reversed(events))[:limit]
