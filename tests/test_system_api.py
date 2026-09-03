"""System visibility endpoints.

These exist to answer the question the review workbench could not: *I uploaded
an invoice — where is it?* Ingestion and processing are deliberately separate,
so a document sits queued until a worker picks it up. That gap was invisible,
and a queued document looked identical to a failed upload.

The tests are weighted towards the states that were previously unobservable —
queued, processing, failed — and towards the one signal that explains all of
them: whether a worker is running at all.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from invoice_processing.adapters import (
    FsCaseRepository,
    JsonDocumentRepository,
    JsonlAuditSink,
)
from invoice_processing.api import auth, service, system
from invoice_processing.api.service import app
from invoice_processing.db.models import Base
from invoice_processing.db.session import get_engine
from invoice_processing.ports import RunContext
from invoice_processing.worker import runner
from invoice_processing.worker.queue import LocalQueue, PubSubQueue
from invoice_processing.worker.runner import (
    ClaimStore,
    read_heartbeat,
    write_heartbeat,
)

TENANT = "tenant-acme"


def claims_for(role):
    return {
        "sub": f"u-{role}",
        "email": f"{role}@acme.test",
        "email_verified": True,
        "tenant_id": TENANT,
        "role": role,
    }


class FakeVerifier:
    def verify(self, token):
        if token not in ("viewer", "clerk", "approver", "admin"):
            raise auth.AuthError("Invalid token")
        return claims_for(token)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A complete, isolated pipeline: registry, queue, claims, audit, cases."""
    documents = JsonDocumentRepository(tmp_path / "documents.json")
    queue = LocalQueue(tmp_path / "queue")
    cases = FsCaseRepository(tmp_path / "cases", tenant_id=TENANT)
    audit = JsonlAuditSink(tmp_path / "audit.jsonl")

    monkeypatch.setattr(service, "_documents", documents)
    monkeypatch.setattr(service, "_cases", cases)
    monkeypatch.setattr(service, "_audit", audit)
    monkeypatch.setattr(service, "get_queue", lambda: queue)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)

    # Point the claim store and heartbeat at the temp tree.
    monkeypatch.setattr(service.paths, "DATA_DIR", tmp_path)
    # Injected like every other adapter: `.env` carries CLOUD_SQL_INSTANCE, so
    # an un-injected store resolves to Postgres and the test would be talking
    # to a different claim store than the API is.
    claims = ClaimStore(tmp_path / "claims")
    monkeypatch.setattr(service, "_claim_store", claims)
    monkeypatch.setattr(service.paths, "SESSIONS_DIR", tmp_path)

    return {
        "documents": documents,
        "queue": queue,
        "cases": cases,
        "audit": audit,
        "claims": claims,
        "tmp": tmp_path,
        "client": lambda role="admin": TestClient(
            app, headers={"Authorization": f"Bearer {role}"}
        ),
    }


def add_document(env, filename="invoice.pdf", sha="abc123"):
    return env["documents"].create(
        TENANT,
        storage_key=f"documents/{sha}.pdf",
        sha256=sha,
        filename=filename,
        source="upload",
        byte_size=1024,
    )


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class TestWhoAmI:
    """The UI hides links a role cannot use; the server refuses them anyway."""

    @pytest.mark.parametrize("role", ["viewer", "clerk", "approver", "admin"])
    def test_reports_the_callers_role(self, env, role):
        body = env["client"](role).get("/api/me").json()
        assert body["role"] == role
        assert body["tenant_id"] == TENANT
        assert body["email"] == f"{role}@acme.test"

    def test_refuses_an_unauthenticated_caller(self, env):
        """A UI that renders a nav for an anonymous caller has already leaked
        the shape of the product to someone who should see a sign-in page."""
        response = TestClient(app).get("/api/me")
        assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Worker liveness — the signal that explains everything else
# ---------------------------------------------------------------------------


class TestSystemStatus:
    def test_reports_no_worker_when_none_has_run(self, env):
        body = env["client"]().get("/api/system/status").json()
        assert body["worker"]["alive"] is False
        assert body["worker"]["ever_run"] is False

    def test_reports_a_live_worker(self, env):
        write_heartbeat(env["tmp"] / "worker_heartbeat.json")
        body = env["client"]().get("/api/system/status").json()
        assert body["worker"]["alive"] is True
        assert body["worker"]["last_seen_seconds"] < 5

    def test_a_stale_heartbeat_is_not_alive(self, env):
        """A worker that died an hour ago must not read as running."""
        path = env["tmp"] / "worker_heartbeat.json"
        path.write_text(json.dumps({"at": time.time() - 3600, "pid": 1}))
        body = env["client"]().get("/api/system/status").json()
        assert body["worker"]["alive"] is False
        assert body["worker"]["ever_run"] is True
        assert body["worker"]["last_seen_seconds"] > 3000

    def test_reports_queue_depth_and_dead_letters(self, env):
        env["queue"].publish({"tenant_id": TENANT, "document_id": "d1"})
        body = env["client"]().get("/api/system/status").json()
        assert body["queue"]["depth"] == 1
        assert body["queue"]["dead_letters"] == 0

    def test_reports_storage_mode(self, env, monkeypatch):
        monkeypatch.delenv("GCS_BUCKET", raising=False)
        body = env["client"]().get("/api/system/status").json()
        assert body["storage"]["documents"] == "local"

    def test_a_broken_probe_does_not_500_the_dashboard(self, env, monkeypatch):
        """A status page that fails when one probe does is worse than a gap."""

        class BrokenQueue:
            def depth(self):
                raise RuntimeError("queue unreadable")

            def dead_letters(self):
                raise RuntimeError("queue unreadable")

        monkeypatch.setattr(service, "get_queue", BrokenQueue)
        body = env["client"]().get("/api/system/status").json()
        assert body["queue"]["depth"] is None

    def test_every_role_can_read_status(self, env):
        for role in ("viewer", "clerk", "approver", "admin"):
            assert (
                env["client"](role).get("/api/system/status").status_code == 200
            )

    def test_requires_authentication(self, env):
        response = TestClient(app).get("/api/system/status")
        assert response.status_code == 401


class TestHeartbeat:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "hb.json"
        write_heartbeat(path)
        record = read_heartbeat(path)
        assert record["alive"] is True

    def test_missing_file_reads_as_never_run(self, tmp_path):
        assert read_heartbeat(tmp_path / "absent.json") is None

    def test_corrupt_file_reads_as_never_run(self, tmp_path):
        path = tmp_path / "hb.json"
        path.write_text("{not json")
        assert read_heartbeat(path) is None

    def test_writing_never_raises(self, tmp_path):
        """Observability must not become a way for the worker to die."""
        write_heartbeat(tmp_path / "nope" / "deep" / "hb.json")


# ---------------------------------------------------------------------------
# Document stages
# ---------------------------------------------------------------------------


class TestListingCost:
    """Both listings read every case in one round trip.

    A read per case is free against a local file and costs about 300ms each
    against Cloud SQL, so the documents list and the review queue each took
    five and a half seconds in production and got slower with every invoice
    added. Counting the reads is the only way that stays fixed.
    """

    def _count_reads(self, env, monkeypatch, path: str) -> int:
        """Reads counted at the port, where a database round trip happens.

        Wrapping the repository rather than patching its methods: the local
        adapter answers a batch read by looping over its own single read, and
        counting that loop would measure file opens instead of round trips.
        """
        reads = []

        class Counting:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def get_artifacts(self, ctx):
                reads.append("one")
                return self._inner.get_artifacts(ctx)

            def get_many_artifacts(self, tenant_id, case_ids):
                reads.append("many")
                return self._inner.get_many_artifacts(tenant_id, case_ids)

        monkeypatch.setattr(
            service, "get_cases", lambda: Counting(env["cases"])
        )
        assert env["client"]().get(path).status_code == 200
        return len(reads)

    def test_the_document_list_reads_once_for_the_page(self, env, monkeypatch):
        for n in range(4):
            add_document(env, filename=f"{n}.pdf", sha=f"cost-{n}")

        assert self._count_reads(env, monkeypatch, "/api/documents") == 1

    def test_the_review_queue_reads_once_for_the_page(self, env, monkeypatch):
        for n in range(4):
            env["cases"].put_artifact(
                RunContext(tenant_id=TENANT, case_id=f"cost-case-{n}"),
                "extraction",
                {"invoice": {"vendor_name": f"Vendor {n}"}},
            )

        assert self._count_reads(env, monkeypatch, "/api/invoices") == 1


class TestDocumentStages:
    def test_a_queued_document_reads_as_queued(self, env):
        add_document(env, sha="q1")
        env["queue"].publish(
            {"tenant_id": TENANT, "document_id": "d", "sha256": "q1"}
        )

        [doc] = env["client"]().get("/api/documents").json()["documents"]
        assert doc["stage"] == "queued"
        assert "waiting for a worker" in doc["explanation"]

    def test_a_claimed_document_reads_as_processing(self, env):
        doc_id = add_document(env, sha="p1")
        env["claims"].claim(TENANT, doc_id)

        [doc] = env["client"]().get("/api/documents").json()["documents"]
        assert doc["stage"] == "processing"

    def test_processing_beats_queued(self, env):
        """A worker picks the message up before the claim is visible."""
        doc_id = add_document(env, sha="b1")
        env["queue"].publish(
            {"tenant_id": TENANT, "document_id": doc_id, "sha256": "b1"}
        )
        env["claims"].claim(TENANT, doc_id)

        [doc] = env["client"]().get("/api/documents").json()["documents"]
        assert doc["stage"] == "processing"

    def test_a_dead_lettered_document_reads_as_failed(self, env):
        add_document(env, sha="f1")
        env["queue"].publish(
            {"tenant_id": TENANT, "document_id": "d", "sha256": "f1"}
        )
        [message] = env["queue"].pull()
        env["queue"].dead_letter(message, "bad pdf")

        [doc] = env["client"]().get("/api/documents").json()["documents"]
        assert doc["stage"] == "failed"
        assert "Retry" in doc["explanation"]

    def test_a_document_the_worker_gave_up_on_reads_as_failed(self, env):
        """On Pub/Sub the queue's dead-letter list is always empty -- there is
        no dead-letter topic -- so the worker's own record is the only thing
        that can explain a document it stopped retrying. Without it the row
        reads "received, but not queued", which invites a retry of something
        that has already failed three times."""
        doc_id = add_document(env, sha="x1")
        env["cases"].put_artifact(
            RunContext(tenant_id=TENANT, case_id=doc_id),
            "attempts",
            {"count": 3, "exhausted": True, "last_error": "unreadable pdf"},
        )

        [doc] = env["client"]().get("/api/documents").json()["documents"]
        assert doc["stage"] == "failed"
        assert "Retry" in doc["explanation"]

    def test_a_document_still_being_retried_does_not_read_as_failed(self, env):
        doc_id = add_document(env, sha="x2")
        env["cases"].put_artifact(
            RunContext(tenant_id=TENANT, case_id=doc_id),
            "attempts",
            {"count": 1, "exhausted": False},
        )

        [doc] = env["client"]().get("/api/documents").json()["documents"]
        assert doc["stage"] != "failed"

    def test_an_attempt_record_alone_is_not_a_processed_invoice(self, env):
        """Same trap as the step counter: bookkeeping stored beside the
        artifacts must never read as pipeline output."""
        doc_id = add_document(env, sha="x3")
        env["cases"].put_artifact(
            RunContext(tenant_id=TENANT, case_id=doc_id),
            "attempts",
            {"count": 1, "exhausted": False},
        )

        [doc] = env["client"]().get("/api/documents").json()["documents"]
        assert doc["stage"] != "done"

    def test_a_retry_that_worked_stops_reading_as_failed(self, env):
        """The dead-letter record of the attempt that failed is never cleared,
        so without this a document that a retry processed successfully went on
        offering Retry while its invoice sat in the workbench."""
        doc_id = add_document(env, sha="r1")
        env["queue"].publish(
            {"tenant_id": TENANT, "document_id": doc_id, "sha256": "r1"}
        )
        [message] = env["queue"].pull()
        env["queue"].dead_letter(message, "first attempt died")
        env["cases"].put_artifact(
            RunContext(tenant_id=TENANT, case_id=doc_id),
            "extraction",
            {"invoice": {"vendor_name": "Retried Pty Ltd"}},
        )

        [doc] = env["client"]().get("/api/documents").json()["documents"]
        assert doc["stage"] == "done"

    def test_a_processed_document_reads_as_done(self, env):
        doc_id = add_document(env, sha="d1")
        env["cases"].put_artifact(
            RunContext(tenant_id=TENANT, case_id=doc_id),
            "extraction",
            {"invoice": {"vendor_name": "Acme"}},
        )
        [doc] = env["client"]().get("/api/documents").json()["documents"]
        assert doc["stage"] == "done"

    def test_a_document_in_flight_reports_the_step_it_is_on(self, env):
        """ "Processing" alone reads as "stuck" once a run passes a minute."""
        doc_id = add_document(env, sha="s1")
        env["claims"].claim(TENANT, doc_id)
        env["cases"].put_artifact(
            RunContext(tenant_id=TENANT, case_id=doc_id),
            "progress",
            {
                "step": 3,
                "total": 9,
                "key": "phase1",
                "label": "Initial intake checks",
            },
        )

        [doc] = env["client"]().get("/api/documents").json()["documents"]
        assert doc["stage"] == "processing"
        assert doc["progress"]["step"] == 3
        assert doc["explanation"] == "Step 3 of 9: Initial intake checks."

    def test_a_reported_step_reads_as_processing_without_a_claim(self, env):
        """The claim store is local disk, so a deployed API never sees the
        claim its worker holds. Reported progress is the only evidence it has
        that an extraction is under way -- without this a live invoice reads
        as "received, but not queued. Retry to process it"."""
        doc_id = add_document(env, sha="s3")
        env["cases"].put_artifact(
            RunContext(tenant_id=TENANT, case_id=doc_id),
            "progress",
            {
                "step": 2,
                "total": 9,
                "key": "extraction",
                "label": "Extracting invoice fields",
                "at": time.time(),
            },
        )

        [doc] = env["client"]().get("/api/documents").json()["documents"]
        assert doc["stage"] == "processing"
        assert doc["explanation"] == "Step 2 of 9: Extracting invoice fields."

    def test_a_step_left_by_a_dead_worker_stops_reading_as_processing(
        self, env
    ):
        """A document stuck on "processing" hides its own Retry button, so a
        worker that died mid-invoice would strand it there forever."""
        doc_id = add_document(env, sha="s4")
        env["cases"].put_artifact(
            RunContext(tenant_id=TENANT, case_id=doc_id),
            "progress",
            {
                "step": 2,
                "total": 9,
                "key": "extraction",
                "label": "Extracting invoice fields",
                "at": time.time() - (system.IN_FLIGHT_SECONDS + 60),
            },
        )

        [doc] = env["client"]().get("/api/documents").json()["documents"]
        assert doc["stage"] != "processing"

    def test_progress_alone_does_not_make_a_document_done(self, env):
        """Progress is a step counter, not a stage artifact. Counting it as one
        would mark a document processed the moment work started on it."""
        doc_id = add_document(env, sha="s2")
        env["cases"].put_artifact(
            RunContext(tenant_id=TENANT, case_id=doc_id),
            "progress",
            {"step": 1, "total": 9, "key": "classification", "label": "x"},
        )

        [doc] = env["client"]().get("/api/documents").json()["documents"]
        assert doc["stage"] != "done"

    def test_an_orphan_document_is_not_silently_done(self, env):
        """Received but never queued must not read as processed."""
        add_document(env, sha="o1")
        [doc] = env["client"]().get("/api/documents").json()["documents"]
        assert doc["stage"] == "unknown"
        assert "Retry" in doc["explanation"]

    def test_counts_are_returned_for_the_dashboard(self, env):
        add_document(env, filename="a.pdf", sha="a1")
        add_document(env, filename="b.pdf", sha="b2")
        body = env["client"]().get("/api/documents").json()
        assert body["total"] == 2
        assert sum(body["counts"].values()) == 2

    def test_newest_first(self, env):
        add_document(env, filename="old.pdf", sha="s1")
        add_document(env, filename="new.pdf", sha="s2")
        docs = env["client"]().get("/api/documents").json()["documents"]
        assert docs[0]["filename"] == "new.pdf"

    def test_a_viewer_cannot_list_documents(self, env):
        assert env["client"]("viewer").get("/api/documents").status_code == 403

    def test_an_empty_registry_is_not_an_error(self, env):
        body = env["client"]().get("/api/documents").json()
        assert body == {"documents": [], "counts": {}, "total": 0}


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class TestRetry:
    def test_requeues_a_failed_document(self, env):
        doc_id = add_document(env, sha="r1")
        body = (
            env["client"]("approver")
            .post(f"/api/documents/{doc_id}/retry")
            .json()
        )
        assert body["stage"] == "queued"
        assert env["queue"].depth() == 1

    def test_releases_the_claim_first(self, env):
        """Otherwise the worker's own idempotency guard skips the new message."""
        doc_id = add_document(env, sha="r2")
        env["claims"].claim(TENANT, doc_id)

        env["client"]("approver").post(f"/api/documents/{doc_id}/retry")
        assert env["claims"].state(TENANT, doc_id) is None

    def test_the_message_carries_the_full_hash(self, env):
        """A truncated hash makes the worker's document lookup fail."""
        doc_id = add_document(env, sha="a" * 64)
        env["client"]("approver").post(f"/api/documents/{doc_id}/retry")
        [message] = env["queue"].pull()
        assert message.payload["sha256"] == "a" * 64

    def test_retry_is_audited(self, env):
        doc_id = add_document(env, sha="r3")
        env["client"]("approver").post(f"/api/documents/{doc_id}/retry")
        events = env["client"]().get("/api/audit").json()["events"]
        assert any(e["action"] == "document_retried" for e in events)

    def test_a_clerk_cannot_retry(self, env):
        """Retry re-runs extraction: it costs a model call and can change a decision."""
        doc_id = add_document(env, sha="r4")
        assert (
            env["client"]("clerk")
            .post(f"/api/documents/{doc_id}/retry")
            .status_code
            == 403
        )

    def test_unknown_document_is_404(self, env):
        assert (
            env["client"]("approver")
            .post("/api/documents/nope/retry")
            .status_code
            == 404
        )


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class TestAudit:
    def test_returns_events_newest_first(self, env):
        doc_id = add_document(env, sha="au1")
        env["client"]("approver").post(f"/api/documents/{doc_id}/retry")
        body = env["client"]().get("/api/audit").json()
        assert body["count"] >= 1
        assert body["events"][0]["action"] == "document_retried"

    def test_events_carry_the_time_and_subject_they_happened_to(self, env):
        """Reader and writer must agree on the keys.

        They did not: `JsonlAuditSink.emit` writes `timestamp` while the reader
        looked for `at`, so every event on the local path rendered with a blank
        time -- an audit trail that cannot say when is not an audit trail.
        """
        doc_id = add_document(env, sha="au2")
        env["client"]("approver").post(f"/api/documents/{doc_id}/retry")
        event = env["client"]().get("/api/audit").json()["events"][0]
        assert event["at"], "audit event has no timestamp"
        assert event["entity_id"] == doc_id

    def test_an_event_says_what_the_value_was(self, env):
        """ "Changed from what?" is the question a trail exists to answer.

        Both sinks have stored `before` since the trail learnt to record it.
        This endpoint returned only the new value, so the Activity page could
        say a field was corrected and never what it held before -- the half
        that makes a correction reviewable.
        """
        env["audit"].emit(
            RunContext(tenant_id=TENANT, case_id="case_001", actor="a@b.test"),
            "fields_corrected",
            {"values": {"po_number": "PO-9999"}},
            before={"values": {"po_number": "PO-8891"}},
        )

        event = env["client"]().get("/api/audit").json()["events"][0]
        assert event["before"] == {"values": {"po_number": "PO-8891"}}
        assert event["payload"] == {"values": {"po_number": "PO-9999"}}

    def test_an_event_with_no_previous_state_says_so(self, env):
        """An upload had no "before". Null, not a missing key: the reader has
        to tell "nothing changed into this" from "we did not record it"."""
        doc_id = add_document(env, sha="au3")
        env["client"]("approver").post(f"/api/documents/{doc_id}/retry")

        event = env["client"]().get("/api/audit").json()["events"][0]
        assert "before" in event
        assert event["before"] is None

    def test_every_role_can_read_the_trail(self, env):
        """A viewer who cannot see why an invoice was rejected cannot answer
        a supplier's question."""
        for role in ("viewer", "clerk", "approver", "admin"):
            assert env["client"](role).get("/api/audit").status_code == 200

    def test_limit_is_bounded(self, env):
        assert env["client"]().get("/api/audit?limit=99999").status_code == 200

    def test_an_empty_trail_is_not_an_error(self, env):
        assert env["client"]().get("/api/audit").json() == {
            "events": [],
            "count": 0,
        }


class BareQueue:
    """A queue with only the four methods the worker needs, and no more.

    `PubSubQueue` was exactly this shape in production, which is how the
    status endpoint learnt to 500.
    """

    def publish(self, payload, **attributes):
        return "id"

    def pull(self, max_messages=1):
        return []

    def ack(self, message):
        pass

    def nack(self, message):
        pass


def _bare_queue():
    return BareQueue()


class TestStatusSurvivesTheQueueItHas:
    """The status endpoint is what a user checks when things look wrong.

    It took the whole endpoint down in production: `PubSubQueue` implements
    neither `depth` nor `dead_letters`, and `_safe(queue.depth, ...)` looked
    the attribute up before the guard could do anything. A 500 from the one
    endpoint that explains outages is the worst place to have one.
    """

    def test_a_queue_missing_depth_does_not_500(self, env, monkeypatch):
        monkeypatch.setattr(service, "get_queue", _bare_queue)
        response = env["client"]().get("/api/system/status")
        assert response.status_code == 200
        body = response.json()
        # Unknown, and reported as unknown rather than as zero -- "nothing
        # waiting" is a much more reassuring claim than "we cannot see".
        assert body["queue"]["depth"] is None
        assert body["queue"]["dead_letters"] is None

    def test_documents_still_lists_with_such_a_queue(self, env, monkeypatch):
        add_document(env, sha="bare1")
        monkeypatch.setattr(service, "get_queue", _bare_queue)
        response = env["client"]("clerk").get("/api/documents")
        assert response.status_code == 200
        assert response.json()["total"] == 1


class TestPubSubQueueAnswersTheStatusQuestions:
    def test_it_reports_depth_as_unknown_not_zero(self):
        queue = PubSubQueue(project_id="p", topic="t", subscription="s")
        assert queue.depth() is None

    def test_it_has_no_dead_letters_without_a_dead_letter_topic(self):
        queue = PubSubQueue(project_id="p", topic="t", subscription="s")
        assert queue.dead_letters() == []


class TestHeartbeatCrossesContainers:
    """Liveness has to be visible from a process that is not the worker.

    It was a file under the data directory. That works with one process and
    not at all with two: the worker wrote it to its own container's disk while
    the API, in another, reported that no worker had ever run -- with invoices
    being processed seconds earlier. The banner said the opposite of the truth.
    """

    def test_a_beat_written_by_one_process_is_read_by_another(
        self, tmp_path, monkeypatch
    ):
        """Nothing shared but the database, which is the point."""
        url = f"sqlite:///{tmp_path / 'beats.db'}"
        monkeypatch.setenv("DATABASE_URL", url)
        Base.metadata.create_all(get_engine(url))

        # The writer's file lives somewhere the reader never looks, standing
        # in for two containers with separate disks.
        write_heartbeat()
        monkeypatch.setattr(
            runner, "heartbeat_path", lambda: tmp_path / "unreachable.json"
        )

        beat = read_heartbeat()
        assert beat is not None, "the reader could not see the writer's beat"
        assert beat["alive"] is True
        assert beat["age_seconds"] < 5

    def test_it_still_works_with_no_database(self, tmp_path, monkeypatch):
        """Local development has one process and no Postgres."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        target = tmp_path / "beat.json"
        write_heartbeat(target)
        assert read_heartbeat(target)["alive"] is True

    def test_a_database_failure_does_not_stop_the_worker(
        self, tmp_path, monkeypatch
    ):
        """A heartbeat is an observation. Losing it must not stop the work."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://nowhere/invalid")

        def explode():
            raise RuntimeError("database is down")

        monkeypatch.setattr(runner, "_write_heartbeat_row", explode)
        write_heartbeat()  # must not raise
