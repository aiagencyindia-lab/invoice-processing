"""The worker that drains the queue.

Its whole job is to turn at-least-once *delivery* into exactly-once *effect*.
Pub/Sub redelivers whenever an ack is lost, and a worker that crashes after
finishing but before acking has lost one -- so a redelivered message is normal
operation, not an error, and processing an invoice twice must be impossible
rather than unlikely.

Three rules, in the order they matter:

* **Claim before working.** A document is marked in-progress before extraction
  starts, and that claim is what a redelivery sees. Checking "is it already
  done?" only at the end leaves the whole extraction window open for a duplicate
  to run alongside the original.
* **Ack only once the result is durable.** Acking first would turn every crash
  into a silently dropped invoice.
* **Give up loudly.** A document that fails `MAX_ATTEMPTS` times is dead-lettered
  with its error, not retried forever. One malformed PDF must not block every
  invoice queued behind it -- and it must not vanish either.

The processing function is injectable, so the retry, idempotency and
dead-letter behaviour is tested without running a real extraction.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..ports import RunContext
from .queue import Message, Queue

logger = logging.getLogger(__name__)

# After this many failed deliveries the message is dead-lettered. Three is
# enough to ride out a transient model timeout and few enough that a genuinely
# broken document is parked in minutes rather than hours.
MAX_ATTEMPTS = 3
# ponytail: a poll-loop timer; a scheduler when there is one.
ESCALATION_SCAN_SECONDS = 60

DOCUMENT_INGESTED = "document.ingested"
# A goods receipt arrived for a PO a held case quotes; see rematch.py.
REMATCH = "case.rematch"
REMATCH_ATTEMPTS = "rematch_attempts"


class ProcessingFailed(RuntimeError):
    """The work failed. Retriable unless attempts are exhausted."""


class PermanentFailure(ProcessingFailed):
    """The work can never succeed. Dead-letter it now rather than retrying."""


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class ClaimStore:
    """Records which documents have been claimed or completed.

    A file per document, created exclusively: `O_EXCL` makes the claim atomic,
    so two workers pulling the same redelivered message cannot both win. A
    database row with a unique constraint is the same idea and is what this
    becomes when the pipeline writes to Postgres.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, tenant_id: str, document_id: str) -> Path:
        # Tenant in the name: two tenants may hold the same document id, and
        # one must never satisfy the other's claim.
        safe_tenant = tenant_id.replace("/", "_") or "unknown"
        safe_document = document_id.replace("/", "_")
        return self.root / f"{safe_tenant}__{safe_document}.json"

    def claim(self, tenant_id: str, document_id: str) -> bool:
        """True if this caller won the claim; False if someone already holds it."""
        path = self._path(tenant_id, document_id)
        try:
            with open(path, "x", encoding="utf-8") as handle:
                handle.write(
                    f'{{"claimed_at": {time.time()}, "state": "running"}}'
                )
            return True
        except FileExistsError:
            return False

    def complete(
        self, tenant_id: str, document_id: str, result: str = "done"
    ) -> None:
        self._path(tenant_id, document_id).write_text(
            f'{{"completed_at": {time.time()}, "state": "{result}"}}',
            encoding="utf-8",
        )

    def release(self, tenant_id: str, document_id: str) -> None:
        """Drop a claim so a retry can pick the work up again."""
        path = self._path(tenant_id, document_id)
        if path.exists():
            path.unlink()

    def state(self, tenant_id: str, document_id: str) -> str | None:
        path = self._path(tenant_id, document_id)
        if not path.exists():
            return None
        import json  # noqa: PLC0415

        try:
            return json.loads(path.read_text(encoding="utf-8")).get("state")
        except (OSError, ValueError):
            return None


# ---------------------------------------------------------------------------
# The real work
# ---------------------------------------------------------------------------


def seen_invoices_for(ctx: RunContext, cases, documents=None):
    """The corpus duplicate detection compares against, for one tenant.

    The pipeline's own default scans a local output directory. On a deployed
    worker that directory is ephemeral -- so a duplicate submitted after a
    restart goes undetected -- and it is shared by every tenant the worker
    serves, so a match could name another tenant's case id in this tenant's
    validation record. Reading through the repository fixes both: the corpus
    is durable, and `tenant_id` scopes it.

    A scanned PO or receipt is not an invoice, so its case is left out.
    """

    def load(exclude_case_id: str = ""):
        from ..duplicates import InvoiceKey  # noqa: PLC0415

        skip = {exclude_case_id}
        if documents is not None:
            try:
                skip |= documents.non_invoice_ids(ctx.tenant_id)
            except Exception as exc:
                # Comparing against too much only finds more duplicates.
                logger.warning("Document kinds not read: %s", exc)
        case_ids = [
            case_id
            for case_id in cases.iter_case_ids(ctx.tenant_id)
            if case_id not in skip
        ]
        if not case_ids:
            return []
        # One query for the page, not one per case: this runs inside every
        # invoice's validation.
        fetched = cases.get_many_artifacts(ctx.tenant_id, case_ids) or {}
        corpus = []
        for case_id, artifacts in fetched.items():
            invoice = (artifacts.get("extraction") or {}).get("invoice") or {}
            if invoice:
                corpus.append(InvoiceKey.from_invoice(case_id, invoice))
        return corpus

    return load


def _apply_rules(ctx: RunContext, cases, artifacts: dict, *, binding: bool):
    """Evaluate this tenant's correction rules and store what they said.

    Never fails the invoice. A malformed rule is an operator's problem and an
    un-corrected invoice is still a processed invoice; refusing to file it
    would turn a bad rule into an accounts-payable outage. That is the
    opposite of the policy for a failed extraction just above, deliberately:
    extraction failing means there is no invoice at all.
    """
    from .. import alf_stage  # noqa: PLC0415
    from ..api import service  # noqa: PLC0415

    try:
        rules_doc, version = service.get_rules().load(ctx.tenant_id)
        written = alf_stage.run(
            artifacts,
            rules_doc,
            binding=binding,
            rule_base_version=version,
        )
    except Exception as exc:
        logger.warning("Rules not applied to %s: %s", ctx.case_id, exc)
        written = {alf_stage.AUDIT_ARTIFACT: {"error": str(exc)[:2000]}}

    for key, payload in written.items():
        cases.put_artifact(ctx, key, payload)
    return written


def _code_gl(ctx: RunContext, cases, artifacts: dict) -> dict:
    """Code each line from this tenant's GL rules and store the coding.

    Informational, so a failure logs and keeps whatever coding was stored
    rather than failing the invoice. A line a person coded is kept: the coding
    stored at write time is passed as `previous`. A decided invoice keeps its
    coding as signed off: the account an approver approved is the one exported.
    """
    from .. import case_projection, gl_coding  # noqa: PLC0415
    from ..api import service  # noqa: PLC0415

    previous = None
    try:
        stored = cases.get_artifacts(ctx) or {}
        previous = stored.get("gl_coding")
        kept = {"gl_coding": previous} if isinstance(previous, dict) else {}
        if (stored.get(service.WORKFLOW_ARTIFACT) or {}).get("decision"):
            return kept
        repo = service.get_gl_rules()
        if repo is None:
            return kept
        invoice = case_projection.extraction(artifacts)
        lines = case_projection.lines(artifacts)
        rules = repo.list_rules(ctx.tenant_id)
        # Recoded from the coding stored at write time, not the one read
        # above: a line a person coded meanwhile is kept.
        coding = cases.update_artifact(
            ctx,
            "gl_coding",
            lambda current: gl_coding.code_lines(
                invoice, lines, rules, previous=current
            ),
        )
    except Exception as exc:
        logger.warning("Lines of %s not GL coded: %s", ctx.case_id, exc)
        return {"gl_coding": previous} if isinstance(previous, dict) else {}
    return {"gl_coding": coding}


def process_supporting(
    ctx: RunContext,
    document: dict,
    pdf_path,
    document_type: str,
    *,
    cases,
    documents,
    supporting,
    purchase_orders,
    audit,
) -> dict:
    """Read a scanned PO or receipt and park it for a person to confirm.

    Never validated, coded, projected or routed: nothing it says counts until
    someone accepts it. An unreadable scan is still stored, empty and with its
    error, so the clerk can key it in or reject it -- never dropped.
    """
    from .. import po_matching, supporting_docs  # noqa: PLC0415

    document_id = document.get("id") or ctx.case_id
    error = None
    try:
        extracted = supporting_docs.extract(pdf_path, document_type)
    except Exception as exc:
        error = str(exc)[:500]
        extracted = {
            "kind": document_type,
            "fields": {},
            "lines": [],
            "_evidence": {},
            "_repairs": [],
        }
    shown = str(extracted["fields"].get("po_number") or "").strip()
    po_number = po_matching.normalize_po_number(shown) or None
    blocked_reason = None
    # Only a PO number that was read can be missing from the book; a receipt
    # with none waits as pending for the clerk to key it in. No book at all
    # is not a book that has the PO.
    if (
        document_type == "goods_receipt"
        and po_number
        and (
            purchase_orders is None
            or purchase_orders.book(ctx.tenant_id, shown) is None
        )
    ):
        blocked_reason = f"Purchase order {shown} is not on file"

    row = supporting.upsert_from_extraction(
        ctx.tenant_id,
        document_id=document_id,
        kind=document_type,
        extracted=extracted,
        po_number=po_number,
        blocked_reason=blocked_reason,
        extraction_error=error,
    )
    documents.set_kind(ctx.tenant_id, document_id, document_type)
    if row is None:
        logger.info(
            "Supporting document %s already decided; left alone", document_id
        )
        return {
            "case_id": ctx.case_id,
            "decision": "SUPPORTING",
            "artifacts": 0,
        }

    cases.put_artifact(ctx, "supporting_extraction", extracted)
    audit.emit(
        ctx,
        "supporting_document_extracted",
        {
            "kind": document_type,
            "po_number": po_number,
            "blocked_reason": blocked_reason,
            "extraction_error": error,
        },
    )
    return {"case_id": ctx.case_id, "decision": "SUPPORTING", "artifacts": 1}


def process_document(ctx: RunContext, document: dict, blobs, cases) -> dict:
    """Run the acting pipeline over one stored document.

    The pipeline is folder-oriented and writes to a package-relative output
    directory, so this materialises the blob into a temporary case folder, runs
    it, and then hands the artifacts to the CaseRepository -- which is where the
    tenant boundary is enforced. Rewriting the pipeline to be storage-agnostic
    is a larger change than this needs, and doing it here would put a refactor
    of the extraction path inside the queue worker.
    """
    from ..shared_libraries.acting.general_invoice_agent import (  # noqa: PLC0415
        get_output_folder,
        process_invoice,
    )

    case_id = ctx.case_id or document["id"]
    workspace = Path(tempfile.mkdtemp(prefix=f"case-{case_id}-"))
    try:
        source = workspace / case_id
        source.mkdir(parents=True)
        (source / "invoice.pdf").write_bytes(blobs.get(document["storage_key"]))

        # Load this tenant's settings into the engine before extracting. The
        # pipeline reads a module-level config dict, so a worker handling two
        # tenants in sequence must reload between them -- otherwise the second
        # invoice is validated against the first tenant's organisation names
        # and rejected for belonging to somebody else.
        from ..tenant_settings import apply_to_pipeline  # noqa: PLC0415

        settings = apply_to_pipeline(ctx.tenant_id)
        # Where the email said replies go, for the sender-domain signal in
        # step 3.4. The pipeline reads config, not the document row, so it is
        # handed over here. Reply-To beats From: it is what a fraudster sets.
        settings["document_sender"] = (
            document.get("reply_to") or document.get("sender") or None
        )

        # Publish each step through the CaseRepository as the pipeline
        # reaches it. It goes through the port rather than onto local disk
        # because the API answering the browser is a different container from
        # the worker doing the work -- a progress file would be invisible to
        # exactly the process that has to report it.
        def publish_step(record: dict) -> None:
            cases.put_artifact(ctx, "progress", record)

        from ..api import service  # noqa: PLC0415

        documents = service.get_documents()
        result = process_invoice(
            source,
            on_step=publish_step,
            seen_invoices=seen_invoices_for(ctx, cases, documents),
        )

        # `process_invoice` catches its own exceptions and returns
        # {"decision": "ERROR"} rather than raising, so a crashed pipeline
        # reaches us looking exactly like a completed one. Left alone, the
        # worker acks the message, marks the claim done, and the document is
        # never retried -- an invoice silently dropped, with the queue reporting
        # a clean run. Surfacing it as a failure lets the normal retry and
        # dead-letter path do its job.
        if (result or {}).get("decision") == "ERROR":
            raise ProcessingFailed(
                f"Pipeline failed for {case_id}: {result.get('error', 'unknown error')}"
            )

        if result.get("decision") == "SUPPORTING":
            supporting = service.get_supporting_documents()
            if supporting is None:
                # Retried, not dropped: the table arrives with a migration.
                raise ProcessingFailed(
                    f"No supporting_documents table to store {case_id} in"
                )
            return process_supporting(
                ctx,
                document,
                source / "invoice.pdf",
                result["document_type"],
                cases=cases,
                documents=documents,
                supporting=supporting,
                purchase_orders=service.get_purchase_orders(),
                audit=service.get_audit(),
            )

        try:
            documents.set_kind(
                ctx.tenant_id, document.get("id") or case_id, "invoice"
            )
        except Exception as exc:
            logger.warning("Kind of %s not recorded: %s", case_id, exc)

        # Copy the artifacts through the port so they land under this tenant,
        # keyed the way every reader expects.
        #
        # This used to store `path.stem` -- "02_extraction". The filesystem
        # repository survived it by accident: it writes `<key>.json`, and its
        # reader maps `02_extraction.json` back to "extraction" through
        # ARTIFACT_MAP. The Postgres one stores the key verbatim, so in a
        # deployed system every artifact was filed under a name no reader
        # looks for, and an invoice that had been fully processed appeared in
        # the workbench with no vendor, no fields, no line items and no
        # findings.
        output = get_output_folder(case_id)
        by_filename = {
            filename: key for key, filename in _artifact_map().items()
        }
        stored = 0
        artifacts: dict[str, dict] = {}
        for path in sorted(output.glob("*.json")):
            import json  # noqa: PLC0415

            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            # An unmapped file keeps its stem, so nothing is dropped.
            key = by_filename.get(path.name, path.stem)
            cases.put_artifact(ctx, key, payload)
            artifacts[key] = payload
            stored += 1

        # Then this tenant's own correction rules, before the projection, so
        # a rule that changes the decision changes the column the sheet and
        # the queue read. This is the half of the learning loop that was
        # missing: rules were taught, validated, impact-assessed and written,
        # and nothing on any deployed path ever read them.
        artifacts.update(
            _apply_rules(
                ctx,
                cases,
                artifacts,
                binding=bool(settings.get("rules_binding")),
            )
        )

        # Then the GL account of each line, so the projected lines carry it.
        artifacts.update(_code_gl(ctx, cases, artifacts))

        # Then the queryable view of them. Deliberately after: the artifacts
        # are the record, and a projection that failed must not cost a run
        # that succeeded -- it can be rebuilt from what is already stored.
        try:
            cases.project_case(ctx, artifacts)
        except Exception as exc:
            logger.warning(
                "Case %s stored but not projected into the relational "
                "tables: %s",
                case_id,
                exc,
            )

        # Who must approve, decided now that the columns exist. Separate from
        # projection on purpose: a projection failure must not leave an invoice
        # unrouted, and an unrouted invoice is one nobody is asked to approve.
        try:
            from .. import approval_flow  # noqa: PLC0415
            from ..api import service  # noqa: PLC0415

            repo = service.get_approvals()
            if repo is not None:
                # The workflow decision lives in the stored record, not in the
                # pipeline's output: without it a retried, already-decided
                # case would be handed a fresh pending chain.
                record = cases.get_artifacts(ctx) or {}
                approval_flow.route(
                    ctx,
                    {**artifacts, "workflow": record.get("workflow")},
                    repo=repo,
                    now=datetime.now(timezone.utc),
                )
        except Exception as exc:
            logger.warning("Case %s not routed for approval: %s", case_id, exc)

        return {
            "case_id": case_id,
            "decision": result.get("decision"),
            "artifacts": stored,
        }
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


HEARTBEAT_STALE_SECONDS = 30


def heartbeat_path() -> Path:
    from .. import paths  # noqa: PLC0415

    return paths.DATA_DIR / "worker_heartbeat.json"


def _worker_id() -> str:
    """Identifies this process across restarts of a deployment.

    Host and pid: two workers on one machine differ by pid, and two containers
    differ by host, so neither overwrites the other's row.
    """
    import socket  # noqa: PLC0415

    return f"{socket.gethostname()}:{os.getpid()}"


def _database_heartbeats() -> bool:
    """Whether liveness belongs in the database rather than on local disk.

    With a database configured there is more than one container, and a file is
    private to whichever one wrote it -- which is precisely how the API came to
    report that no worker had ever run while one was processing invoices.
    """
    from ..api import service  # noqa: PLC0415

    return service._database_configured()


def write_heartbeat(path: Path | None = None) -> None:
    """Record that a worker is alive. Best-effort: never breaks the loop.

    A heartbeat that could crash the worker would be worse than no heartbeat --
    the whole point is observing the worker, not adding a way for it to die.
    """
    if path is None and _database_heartbeats():
        try:
            _write_heartbeat_row()
            return
        except Exception as exc:
            # Fall through to the file: a heartbeat is an observation, and
            # losing it must not stop the worker doing its actual job.
            logger.warning(
                "Could not record heartbeat in the database: %s", exc
            )

    target = path or heartbeat_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({"at": time.time(), "pid": os.getpid()}))
        os.replace(tmp, target)
    except OSError:
        pass


def _write_heartbeat_row() -> None:
    from sqlalchemy.dialects import postgresql  # noqa: PLC0415
    from sqlalchemy.orm import Session  # noqa: PLC0415

    from ..db.models import WorkerHeartbeat  # noqa: PLC0415
    from ..db.session import get_engine  # noqa: PLC0415

    engine = get_engine()
    now = datetime.now(timezone.utc)
    values = {
        "worker_id": _worker_id(),
        "last_seen_at": now,
        "pid": os.getpid(),
    }
    with Session(engine) as session:
        if engine.dialect.name == "postgresql":
            statement = postgresql.insert(WorkerHeartbeat).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=["worker_id"],
                set_={"last_seen_at": now, "pid": values["pid"]},
            )
            session.execute(statement)
        else:
            # SQLite in tests: merge is enough and needs no dialect support.
            session.merge(WorkerHeartbeat(**values))
        session.commit()


def _read_heartbeat_row() -> dict | None:
    from sqlalchemy import select  # noqa: PLC0415
    from sqlalchemy.orm import Session  # noqa: PLC0415

    from ..db.models import WorkerHeartbeat  # noqa: PLC0415
    from ..db.session import get_engine  # noqa: PLC0415

    with Session(get_engine()) as session:
        # The newest beat from any worker: the question is whether anything is
        # alive, not whether a particular process is.
        row = session.scalars(
            select(WorkerHeartbeat)
            .order_by(WorkerHeartbeat.last_seen_at.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        seen = row.last_seen_at
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        return {"at": seen.timestamp(), "pid": row.pid}


def _read_heartbeat_file(path: Path | None = None) -> dict | None:
    target = path or heartbeat_path()
    try:
        return json.loads(target.read_text())
    except (OSError, ValueError):
        return None


def read_heartbeat(path: Path | None = None) -> dict | None:
    """The last heartbeat, or None when no worker has ever run here."""
    record: dict | None = None
    if path is None and _database_heartbeats():
        try:
            record = _read_heartbeat_row()
        except Exception as exc:
            logger.warning(
                "Could not read heartbeat from the database: %s", exc
            )
            record = None
        if record is None:
            # No row yet, or unreadable: fall back rather than assert that
            # nothing has ever run, which is a stronger claim than we can make.
            record = _read_heartbeat_file(path)
    else:
        record = _read_heartbeat_file(path)

    if record is None:
        return None
    at = record.get("at")
    if not isinstance(at, (int, float)):
        return None
    age = max(0.0, time.time() - at)
    return {
        "at": at,
        "pid": record.get("pid"),
        "age_seconds": round(age, 1),
        # Stale rather than dead: a worker mid-extraction on a slow model can
        # exceed the poll interval, so this is a signal to show, not a verdict.
        "alive": age <= HEARTBEAT_STALE_SECONDS,
    }


# The worker beats this often while it is busy. It has to be well inside
# HEARTBEAT_STALE_SECONDS, or the gap between two beats is itself enough to
# make the worker look dead.
HEARTBEAT_INTERVAL_SECONDS = 10.0


def start_heartbeat(
    interval: float = HEARTBEAT_INTERVAL_SECONDS, write=None
) -> threading.Event:
    """Beat on a timer for as long as the returned event is unset.

    The loop only wrote a heartbeat between messages, so an extraction that
    took longer than HEARTBEAT_STALE_SECONDS -- which every extraction does,
    at around a minute -- made the API tell everyone watching that processing
    was paused and the worker had gone, while it was in fact mid-invoice.
    """
    beat = write or write_heartbeat
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(interval):
            beat()

    # Daemon: a heartbeat must never be the reason the process will not exit.
    threading.Thread(target=loop, daemon=True, name="heartbeat").start()
    return stop


class AttemptLog:
    """How many times a document has been tried, where everyone can see it.

    Pub/Sub only populates `delivery_attempt` when the subscription has a
    dead-letter policy. This one has none, so every redelivery arrives looking
    like attempt 1: the three-attempt cap never fires and a document that can
    never succeed is re-extracted every ack deadline, forever, at the price of
    a model call each time. Locally the same document parks after three tries,
    because the local queue counts for itself -- which is why no test saw it.

    The count lives beside the case artifacts because that store is the one
    thing the API container and the worker container share. Readers strip it:
    it is bookkeeping, not pipeline output.
    """

    KEY = "attempts"

    def __init__(self, cases, key: str = KEY):
        self.cases = cases
        # Re-match jobs count under their own key: the document's record is
        # what the API reads to say whether an upload failed.
        self.KEY = key

    def _ctx(self, tenant_id: str, document_id: str) -> RunContext:
        return RunContext(tenant_id=tenant_id, case_id=document_id)

    def count(self, tenant_id: str, document_id: str) -> int:
        record = self._read(tenant_id, document_id)
        value = record.get("count")
        return value if isinstance(value, int) else 0

    def _read(self, tenant_id: str, document_id: str) -> dict:
        artifacts = (
            self.cases.get_artifacts(self._ctx(tenant_id, document_id)) or {}
        )
        record = artifacts.get(self.KEY)
        return record if isinstance(record, dict) else {}

    def record_failure(
        self, tenant_id: str, document_id: str, error: str, exhausted: bool
    ) -> int:
        count = self.count(tenant_id, document_id) + 1
        self.cases.put_artifact(
            self._ctx(tenant_id, document_id),
            self.KEY,
            {
                "count": count,
                "last_error": error[:2000],
                "at": time.time(),
                # What the API reports as "failed". Without it a document the
                # worker gave up on reads as "received, but not queued" --
                # advice to retry, with no hint that anything went wrong.
                "exhausted": exhausted,
            },
        )
        return count

    def clear(self, tenant_id: str, document_id: str) -> None:
        """A retry that worked leaves no failure behind."""
        if self.count(tenant_id, document_id) == 0:
            return
        self.cases.put_artifact(
            self._ctx(tenant_id, document_id),
            self.KEY,
            {"count": 0, "exhausted": False, "at": time.time()},
        )


class Worker:
    def __init__(
        self,
        queue: Queue,
        claims: ClaimStore,
        handler: Callable[[Message], dict] | None = None,
        audit=None,
        max_attempts: int = MAX_ATTEMPTS,
        attempts: AttemptLog | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.clock = clock
        self._last_scan: float | None = None
        self.queue = queue
        self.claims = claims
        self.handler = handler or self._default_handler
        self.audit = audit
        self.max_attempts = max_attempts
        # Without one, attempt counting falls back to whatever the queue
        # reports -- correct locally, and permanently 1 on Pub/Sub.
        self.attempts = attempts

    # -- one message -------------------------------------------------------

    def handle(self, message: Message) -> str:
        """Process one message. Returns what happened, for the caller to log.

        Every path ends in exactly one of ack, nack or dead-letter. A path that
        ended in none of them would leave the message to be redelivered on lease
        expiry, which looks like a hang.
        """
        if message.payload.get("type") == REMATCH:
            # No document and no claim: re-matching twice is idempotent.
            return self._handle_rematch(message)

        tenant_id = str(message.payload.get("tenant_id") or "")
        document_id = str(message.payload.get("document_id") or "")

        if not tenant_id or not document_id:
            # Nothing here can be retried into correctness.
            self._dead_letter(
                message, "Message is missing tenant_id or document_id"
            )
            return "dead-lettered:malformed"

        if self.claims.state(tenant_id, document_id) == "done":
            # The common redelivery: we finished, the ack was lost.
            self.queue.ack(message)
            return "skipped:already-done"

        if not self.claims.claim(tenant_id, document_id):
            # Someone else holds it. Their outcome decides; drop this copy
            # rather than running a second extraction alongside theirs.
            self.queue.ack(message)
            return "skipped:claimed-elsewhere"

        try:
            result = self.handler(message)
        except PermanentFailure as exc:
            self.claims.complete(tenant_id, document_id, "failed")
            self._dead_letter(message, f"Permanent failure: {exc}")
            return "dead-lettered:permanent"
        except Exception as exc:
            self.claims.release(tenant_id, document_id)
            attempt = self._count_failure(tenant_id, document_id, message, exc)
            logger.warning(
                "Attempt %s/%s failed for %s: %s",
                attempt,
                self.max_attempts,
                document_id,
                exc,
            )
            if attempt >= self.max_attempts:
                self._dead_letter(
                    message,
                    f"Failed {attempt} times: {exc}\n{traceback.format_exc()}",
                )
                return "dead-lettered:exhausted"
            self.queue.nack(message)
            return "retry"

        self._clear_failures(tenant_id, document_id)
        self.claims.complete(tenant_id, document_id, "done")
        # Ack last: until this point a crash means redelivery, which is correct.
        self.queue.ack(message)
        self._emit(tenant_id, document_id, "document_processed", result or {})
        return "done"

    def _handle_rematch(self, message: Message) -> str:
        """Match one held case again with the tenant's book and settings."""
        tenant_id = str(message.payload.get("tenant_id") or "")
        case_id = str(message.payload.get("case_id") or "")
        if not tenant_id or not case_id:
            self._dead_letter(
                message,
                "Re-match message is missing tenant_id or case_id",
                event="rematch_dead_lettered",
            )
            return "dead-lettered:malformed"
        log = (
            AttemptLog(self.attempts.cases, key=REMATCH_ATTEMPTS)
            if self.attempts is not None
            else None
        )
        try:
            from .. import rematch, tenant_settings  # noqa: PLC0415
            from ..api import service  # noqa: PLC0415
            from ..shared_libraries.acting import (  # noqa: PLC0415
                general_invoice_agent as agent,
            )

            config = tenant_settings.apply_to_pipeline(tenant_id)
            rematch.rematch_case(
                RunContext(
                    tenant_id=tenant_id,
                    case_id=case_id,
                    run_id=message.id,
                    actor="worker",
                ),
                service.get_cases(),
                # The tenant's file when it has no orders in the database.
                config.get("purchase_order_book")
                or agent._load_purchase_orders(),
                vendors=agent._load_vendors(),
                config=config,
                audit=self.audit or service.get_audit(),
            )
        except Exception as exc:
            attempt = self._count_failure(
                tenant_id, case_id, message, exc, log=log
            )
            if attempt >= self.max_attempts:
                self._dead_letter(
                    message,
                    f"Failed {attempt} times: {exc}",
                    event="rematch_dead_lettered",
                )
                return "dead-lettered:exhausted"
            self.queue.nack(message)
            return "retry"
        self._clear_failures(tenant_id, case_id, log=log)
        self.queue.ack(message)
        return "rematched"

    # -- loop --------------------------------------------------------------

    def run_once(self, max_messages: int = 10) -> list[str]:
        return [
            self.handle(message) for message in self.queue.pull(max_messages)
        ]

    def run_forever(self, poll_seconds: float = 5.0, sleep=time.sleep) -> None:
        logger.info("Worker started.")
        start_heartbeat()
        while True:
            # Written every loop, before and after work, so the API can tell
            # "no worker running" from "worker busy with a slow extraction".
            # Without it the UI cannot explain the single most confusing thing
            # about this system: an upload that sits queued forever because
            # nobody started a worker looks identical to a failed upload.
            write_heartbeat()
            try:
                outcomes = self.run_once()
            except Exception:
                logger.exception("Pull failed; continuing.")
                outcomes = []
            write_heartbeat()
            self._escalate_if_due()
            if not outcomes:
                sleep(poll_seconds)

    # -- internals ---------------------------------------------------------

    def _escalate_if_due(self) -> None:
        now = self.clock()
        if (
            self._last_scan is not None
            and now - self._last_scan < ESCALATION_SCAN_SECONDS
        ):
            return
        self._last_scan = now
        try:
            from .. import approval_flow  # noqa: PLC0415
            from ..api import service  # noqa: PLC0415

            repo = service.get_approvals()
            if repo is not None:
                approval_flow.escalate_overdue(
                    repo, self.audit, now=datetime.now(timezone.utc)
                )
        except Exception:
            logger.exception("Overdue-approval scan failed; continuing.")

    def _default_handler(self, message: Message) -> dict:
        from ..api import service  # noqa: PLC0415

        payload = message.payload
        tenant_id = payload["tenant_id"]
        documents = service.get_documents()
        document = documents.find_by_sha(tenant_id, payload.get("sha256", ""))
        if document is None:
            raise PermanentFailure(
                f"No document {payload.get('document_id')} in tenant {tenant_id}"
            )
        ctx = RunContext(
            tenant_id=tenant_id,
            case_id=payload.get("case_id") or document["id"],
            run_id=message.id,
            actor="worker",
        )
        return process_document(
            ctx, document, service.get_blobs(tenant_id), service.get_cases()
        )

    def _count_failure(
        self,
        tenant_id: str,
        document_id: str,
        message: Message,
        exc: Exception,
        log: AttemptLog | None = None,
    ) -> int:
        """This attempt's number, from the most trustworthy source available.

        The queue's own count is kept as a floor: on the local queue it is
        accurate, and taking the higher of the two means neither source can
        undercount a document into retrying forever.
        """
        reported = message.delivery_attempt or 1
        if log is None:
            log = self.attempts
        if log is None:
            return reported
        try:
            counted = log.record_failure(
                tenant_id,
                document_id,
                str(exc),
                exhausted=max(
                    reported,
                    log.count(tenant_id, document_id) + 1,
                )
                >= self.max_attempts,
            )
        except Exception as store_error:
            # A counter that could fail the handler would turn a retriable
            # error into a lost invoice.
            logger.warning(
                "Could not record the failed attempt for %s: %s",
                document_id,
                store_error,
            )
            return reported
        return max(reported, counted)

    def _clear_failures(
        self, tenant_id: str, document_id: str, log: AttemptLog | None = None
    ) -> None:
        if log is None:
            log = self.attempts
        if log is None:
            return
        try:
            log.clear(tenant_id, document_id)
        except Exception as store_error:
            logger.warning(
                "Could not clear the failure record for %s: %s",
                document_id,
                store_error,
            )

    def _dead_letter(
        self,
        message: Message,
        reason: str,
        event: str = "document_dead_lettered",
    ) -> None:
        dead_letter = getattr(self.queue, "dead_letter", None)
        if dead_letter is not None:
            dead_letter(message, reason)
        else:  # pragma: no cover - every queue here implements it
            self.queue.ack(message)
        self._emit(
            str(message.payload.get("tenant_id") or ""),
            str(
                message.payload.get("document_id")
                or message.payload.get("case_id")
                or ""
            ),
            event,
            {"reason": reason, "message_id": message.id},
        )

    def _emit(
        self, tenant_id: str, document_id: str, event: str, payload: dict
    ) -> None:
        if self.audit is None or not tenant_id:
            return
        self.audit.emit(
            # The id goes in the context, not only the payload: `entity_id` is
            # the column a reader filters on, and it was null on every event
            # the worker wrote -- so "what happened to this document" could
            # not be asked of the trail at all.
            RunContext(
                tenant_id=tenant_id, case_id=document_id, actor="worker"
            ),
            event,
            {"document_id": document_id, **payload},
        )


def _artifact_map() -> dict[str, str]:
    """Logical key -> filename, as every reader of a case understands it."""
    from ..core.config import ARTIFACT_MAP  # noqa: PLC0415

    return dict(ARTIFACT_MAP)


def enqueue_document(queue: Queue, ctx: RunContext, result: Any) -> str:
    """Publish the work item for one ingested document."""
    return queue.publish(
        {
            "type": DOCUMENT_INGESTED,
            "tenant_id": ctx.tenant_id,
            "document_id": result.document_id,
            "sha256": result.sha256,
            "storage_key": result.storage_key,
        },
        tenant_id=ctx.tenant_id,
    )


def enqueue_rematch(
    queue: Queue, tenant_id: str, case_id: str, po_number: str
) -> str:
    """Publish a re-match of one held case after a goods receipt."""
    return queue.publish(
        {
            "type": REMATCH,
            "tenant_id": tenant_id,
            "case_id": case_id,
            "po_number": po_number,
        },
        tenant_id=tenant_id,
    )
