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
from datetime import UTC

import pytest
from fastapi.testclient import TestClient
from fresh_schema import fresh_schema

from invoice_processing.adapters import (
    FsCaseRepository,
    JsonDocumentRepository,
    JsonlAuditSink,
    JsonSupportingDocumentRepository,
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
    monkeypatch.setattr(
        service,
        "_supporting_documents",
        JsonSupportingDocumentRepository(
            tmp_path / "supporting_documents.json", documents
        ),
    )
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

    def test_an_invoice_is_retried_as_an_invoice(self, env):
        """Sent to the invoice queue from PO review, it must not be diverted
        back there by a retry; an unclassified document is not forced."""
        doc_id = add_document(env, sha="r5")
        other = add_document(env, sha="r6")
        service.get_documents().set_kind(TENANT, doc_id, "invoice")
        for d in (doc_id, other):
            env["client"]("approver").post(f"/api/documents/{d}/retry")
        forced = {
            m.payload["document_id"]: m.payload.get("force_invoice")
            for m in env["queue"].pull(max_messages=5)
        }
        assert forced == {doc_id: True, other: None}

    def test_a_database_without_scans_retries_unforced(self, env, monkeypatch):
        """No supporting-documents table: nothing was ever sent over."""
        doc_id = add_document(env, sha="r7")
        monkeypatch.setattr(service, "get_supporting_documents", lambda: None)
        r = env["client"]("approver").post(f"/api/documents/{doc_id}/retry")
        assert r.status_code == 200, r.text
        [message] = env["queue"].pull()
        assert message.payload.get("force_invoice") is None

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

    def test_a_queue_that_times_out_answers_503(self, env, monkeypatch):
        """A stalled broker gets "try again", not a bare 500."""
        doc_id = add_document(env, sha="r8")

        def stalled(payload, **attributes):
            raise TimeoutError

        monkeypatch.setattr(env["queue"], "publish", stalled)
        r = env["client"]("approver").post(f"/api/documents/{doc_id}/retry")
        assert r.status_code == 503
        assert "retry" in r.json()["detail"].lower()

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
            "next_cursor": None,
        }

    def test_events_carry_the_request_id_and_the_ip_keys(self, env):
        doc_id = add_document(env, sha="au5")
        env["client"]("approver").post(f"/api/documents/{doc_id}/retry")
        event = env["client"]().get("/api/audit").json()["events"][0]
        assert event["request_id"]
        assert set(event) >= {"id", "actor_type", "request_id", "ip"}

    def test_a_window_excludes_what_is_outside_it(self, env):
        doc_id = add_document(env, sha="au6")
        env["client"]("approver").post(f"/api/documents/{doc_id}/retry")
        past = "2000-01-01T00:00:00Z"
        assert (
            env["client"]().get(f"/api/audit?until={past}").json()["count"] == 0
        )
        assert (
            env["client"]().get(f"/api/audit?since={past}").json()["count"] >= 1
        )
        assert (
            env["client"]()
            .get(f"/api/audit?since=2099-01-01T00:00:00Z&until={past}")
            .json()["count"]
            == 0
        )

    def test_a_malformed_cursor_is_refused(self, env):
        assert (
            env["client"]().get("/api/audit?cursor=nonsense").status_code == 422
        )

    def test_pages_through_a_database_trail_by_cursor(
        self, env, scratch_db, monkeypatch
    ):
        """The Postgres path end to end: serialisation, full-page rule, cursor."""
        from datetime import datetime, timedelta  # noqa: PLC0415

        from invoice_processing.db.models import (  # noqa: PLC0415
            AuditEvent,
            Tenant,
        )
        from invoice_processing.db.repositories import (  # noqa: PLC0415
            PgAuditSink,
        )
        from invoice_processing.db.session import (  # noqa: PLC0415
            tenant_session,
        )

        url = scratch_db("audit_api.db")
        fresh_schema(url)
        with tenant_session(TENANT, url) as s:
            s.add(Tenant(id=TENANT, slug="acme", name="Acme"))
        t0 = datetime(2026, 10, 1, tzinfo=UTC)
        with tenant_session(TENANT, url) as s:
            for i in range(3):
                s.add(
                    AuditEvent(
                        id=f"e{i}",
                        tenant_id=TENANT,
                        occurred_at=t0 + timedelta(hours=i),
                        action="x",
                    )
                )
        monkeypatch.setattr(service, "_audit", PgAuditSink(url))

        client = env["client"]()
        first = client.get("/api/audit?limit=2").json()
        assert [e["id"] for e in first["events"]] == ["e2", "e1"]
        assert first["next_cursor"]
        assert set(first["events"][0]) >= {
            "id",
            "actor_type",
            "request_id",
            "ip",
        }
        rest = client.get(
            "/api/audit", params={"limit": 2, "cursor": first["next_cursor"]}
        ).json()
        assert [e["id"] for e in rest["events"]] == ["e0"]
        assert rest["next_cursor"] is None

    def test_a_pasted_cursor_whose_plus_became_a_space_still_pages(
        self, env, monkeypatch
    ):
        """Postgres stamps carry "+00:00"; pasted unencoded into a URL the
        "+" arrives as a space. A cursor the server issued must still page."""
        from datetime import datetime, timedelta  # noqa: PLC0415
        from types import SimpleNamespace  # noqa: PLC0415

        t0 = datetime(2026, 10, 1, tzinfo=UTC)
        events = [
            SimpleNamespace(
                id=f"e{i}",
                occurred_at=t0 + timedelta(hours=i),
                actor="a",
                actor_type="user",
                action="x",
                entity_type=None,
                entity_id=None,
                after=None,
                before=None,
                request_id=None,
                ip=None,
            )
            for i in range(3)
        ][::-1]

        class Fake:
            def query(self, tenant_id, limit=100, before=None, **k):
                rows = [
                    e
                    for e in events
                    if before is None or (e.occurred_at, e.id) < before
                ]
                return rows[:limit]

        monkeypatch.setattr(service, "_audit", Fake())
        client = env["client"]()
        first = client.get("/api/audit?limit=2").json()
        assert "+00:00" in first["next_cursor"]
        pasted = first["next_cursor"].replace("+", " ")
        rest = client.get(f"/api/audit?limit=2&cursor={pasted}")
        assert rest.status_code == 200, rest.text
        assert [e["id"] for e in rest.json()["events"]] == ["e0"]

    def test_a_cursor_is_not_answered_from_the_local_file_when_the_database_fails(
        self, env, monkeypatch
    ):
        class Broken:
            def query(self, *a, **k):
                raise RuntimeError("db down")

        monkeypatch.setattr(service, "_audit", Broken())
        cursor = "2026-10-01T00:00:00+00:00|e1"
        response = env["client"]().get("/api/audit", params={"cursor": cursor})
        assert response.status_code == 503
        assert "needs a database" in response.json()["detail"]

    def test_an_admin_can_export_a_window_as_csv(self, env):
        doc_id = add_document(env, sha="au7")
        env["client"]("approver").post(f"/api/documents/{doc_id}/retry")
        r = env["client"]("admin").get(
            "/api/audit/export?since=2000-01-01T00:00:00Z&until=2099-01-01T00:00:00Z"
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert r.headers["content-disposition"].startswith(
            'attachment; filename="audit-'
        )
        lines = r.text.splitlines()
        assert (
            lines[0]
            == "id,occurred_at,actor,actor_type,action,entity_type,entity_id,request_id,ip,before,after"
        )
        assert any("document_retried" in line for line in lines[1:])
        actions = [
            e["action"]
            for e in env["client"]().get("/api/audit").json()["events"]
        ]
        assert "audit_exported" in actions

    def test_an_empty_window_still_has_a_header(self, env):
        r = env["client"]("admin").get(
            "/api/audit/export?since=2000-01-01T00:00:00Z&until=2000-01-02T00:00:00Z"
        )
        assert r.status_code == 200
        assert r.text.splitlines() == [
            "id,occurred_at,actor,actor_type,action,entity_type,entity_id,request_id,ip,before,after"
        ]

    def test_only_an_admin_can_export(self, env):
        for role in ("viewer", "clerk", "approver"):
            r = env["client"](role).get(
                "/api/audit/export?since=2000-01-01T00:00:00Z&until=2099-01-01T00:00:00Z"
            )
            assert r.status_code == 403, role

    def test_an_export_with_a_dead_database_is_a_clean_503(
        self, env, monkeypatch
    ):
        emitted = []

        class Broken:
            def emit(self, *a, **k):
                emitted.append(a)

            def query(self, *a, **k):
                raise RuntimeError("db down")

        monkeypatch.setattr(service, "_audit", Broken())
        r = env["client"]("admin").get(
            "/api/audit/export?since=2000-01-01T00:00:00Z&until=2099-01-01T00:00:00Z"
        )
        assert r.status_code == 503
        assert "needs a database" in r.json()["detail"]
        assert emitted == []  # a refused export is not recorded as a pull

    def test_an_export_pages_and_quotes_nested_values(self, env, monkeypatch):
        import csv  # noqa: PLC0415
        import io  # noqa: PLC0415
        from datetime import datetime, timedelta  # noqa: PLC0415
        from types import SimpleNamespace  # noqa: PLC0415

        t0 = datetime(2026, 10, 1, tzinfo=UTC)
        events = [
            SimpleNamespace(
                id=f"e{i:04d}",
                occurred_at=t0 + timedelta(seconds=i),
                actor="a,b",
                actor_type="user",
                action="x",
                entity_type="invoice",
                entity_id="1",
                after={"k": [1, 2], "n": {"m": 'q,"r'}},
                before=None,
                request_id="r",
                ip="1.2.3.4",
            )
            for i in range(1500)
        ][::-1]  # newest first, as the sink returns

        class Fake:
            def emit(self, *a, **k):
                pass

            def query(
                self, tenant_id, limit=100, since=None, until=None, before=None
            ):
                rows = events
                if before is not None:
                    rows = [e for e in events if (e.occurred_at, e.id) < before]
                return rows[:limit]

        monkeypatch.setattr(service, "_audit", Fake())
        r = env["client"]("admin").get(
            "/api/audit/export?since=2000-01-01T00:00:00Z&until=2099-01-01T00:00:00Z"
        )
        assert r.status_code == 200
        rows = list(csv.reader(io.StringIO(r.text)))
        assert len(rows) == 1501
        data_rows = rows[1:]
        # Across the 1000-row page boundary: nothing repeated, nothing out
        # of order.
        assert len({row[0] for row in data_rows}) == 1500
        stamps = [row[1] for row in data_rows]
        assert stamps == sorted(stamps, reverse=True)
        assert data_rows[999][0] == "e0500" and data_rows[1000][0] == "e0499"
        assert rows[1][2] == "a,b"
        assert json.loads(rows[1][10])["n"]["m"] == 'q,"r'
        assert rows[1][9] == ""

    def test_spreadsheet_formulas_in_cells_are_defused(self, env, monkeypatch):
        import csv  # noqa: PLC0415
        import io  # noqa: PLC0415
        from datetime import datetime  # noqa: PLC0415
        from types import SimpleNamespace  # noqa: PLC0415

        def ev(i, **kw):
            base = dict(  # noqa: C408
                id=f"e{i}",
                occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
                actor="a",
                actor_type="user",
                action="x",
                entity_type="invoice",
                entity_id="1",
                after=None,
                before=None,
                request_id="r",
                ip="1.2.3.4",
            )
            base.update(kw)
            return SimpleNamespace(**base)

        events = [
            ev(1, actor='=HYPERLINK("http://x")'),
            ev(2, entity_id="@SUM(1)"),
            ev(3, action="\tx", request_id="-1+1", ip="+1"),
        ]

        class Fake:
            def emit(self, *a, **k):
                pass

            def query(self, *a, **k):
                return events

        monkeypatch.setattr(service, "_audit", Fake())
        r = env["client"]("admin").get(
            "/api/audit/export?since=2000-01-01T00:00:00Z&until=2099-01-01T00:00:00Z"
        )
        rows = list(csv.reader(io.StringIO(r.text)))
        assert rows[1][2] == '\'=HYPERLINK("http://x")'
        assert rows[2][6] == "'@SUM(1)"
        assert rows[3][4] == "'\tx"
        assert rows[3][7] == "'-1+1"
        assert rows[3][8] == "'+1"

    def test_a_failure_after_the_first_page_aborts_the_download(
        self, env, monkeypatch, caplog
    ):
        from datetime import datetime, timedelta  # noqa: PLC0415
        from types import SimpleNamespace  # noqa: PLC0415

        t0 = datetime(2026, 10, 1, tzinfo=UTC)
        full = [
            SimpleNamespace(
                id=f"e{i:04d}",
                occurred_at=t0 + timedelta(seconds=i),
                actor="a",
                actor_type="user",
                action="x",
                entity_type="invoice",
                entity_id="1",
                after=None,
                before=None,
                request_id="r",
                ip=None,
            )
            for i in range(1000)
        ]
        calls = []

        class Flaky:
            def emit(self, *a, **k):
                pass

            def query(self, *a, **k):
                calls.append(1)
                if len(calls) > 1:
                    raise RuntimeError("db died mid-export")
                return full

        monkeypatch.setattr(service, "_audit", Flaky())
        with pytest.raises(Exception):  # noqa: B017
            env["client"]("admin").get(
                "/api/audit/export?since=2000-01-01T00:00:00Z&until=2099-01-01T00:00:00Z"
            )
        assert "Audit export failed" in caplog.text

    def test_an_export_whose_record_cannot_be_written_is_a_clean_503(
        self, env, monkeypatch
    ):
        """The data is readable but the pull cannot be recorded: refuse it,
        rather than a 500 or an unrecorded download."""

        class CannotRecord:
            def emit(self, *a, **k):
                raise RuntimeError("audit write failed")

            def query(self, *a, **k):
                return []

        monkeypatch.setattr(service, "_audit", CannotRecord())
        r = env["client"]("admin").get(
            "/api/audit/export?since=2000-01-01T00:00:00Z&until=2099-01-01T00:00:00Z"
        )
        assert r.status_code == 503
        assert "needs a database" in r.json()["detail"]
        assert not r.headers["content-type"].startswith("text/csv")

    def test_a_naive_window_is_read_as_utc(self, env, monkeypatch):
        """JSONL treats a naive time as UTC; Postgres would read it in the
        session's zone. Both entry points hand the sink the same instant."""
        seen = []

        class Recording:
            def emit(self, *a, **k):
                pass

            def query(self, tenant_id, **k):
                seen.append((k["since"], k["until"]))
                return []

        monkeypatch.setattr(service, "_audit", Recording())
        client = env["client"]("admin")
        for naive, zoned in (
            (
                "/api/audit?since=2026-10-01T09:00:00&until=2026-10-02T09:00:00",
                "/api/audit?since=2026-10-01T09:00:00Z&until=2026-10-02T09:00:00Z",
            ),
            (
                "/api/audit/export?since=2026-10-01T09:00:00&until=2026-10-02T09:00:00",
                "/api/audit/export?since=2026-10-01T09:00:00Z&until=2026-10-02T09:00:00Z",
            ),
        ):
            seen.clear()
            assert client.get(naive).status_code == 200
            assert client.get(zoned).status_code == 200
            (n_since, n_until), (z_since, z_until) = seen
            for n, z in ((n_since, z_since), (n_until, z_until)):
                assert n.tzinfo is not None, naive
                assert n == z

    @pytest.mark.slow
    def test_the_local_file_export_has_no_row_cap(
        self, env, monkeypatch, tmp_path
    ):
        from invoice_processing import paths  # noqa: PLC0415

        monkeypatch.setattr(paths, "SESSIONS_DIR", tmp_path)
        line = json.dumps(
            {"timestamp": "2026-10-01T00:00:00+00:00", "event_type": "x"}
        )
        (tmp_path / "audit.jsonl").write_text(
            "\n".join([line] * 100_005), encoding="utf-8"
        )

        class NoQuery:
            def emit(self, *a, **k):
                pass

        monkeypatch.setattr(service, "_audit", NoQuery())
        r = env["client"]("admin").get(
            "/api/audit/export?since=2000-01-01T00:00:00Z&until=2099-01-01T00:00:00Z"
        )
        assert len(r.text.splitlines()) == 100_006


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
