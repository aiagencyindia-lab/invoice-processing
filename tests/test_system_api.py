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
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.ports import RunContext
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
    monkeypatch.setattr(service.paths, "SESSIONS_DIR", tmp_path)

    return {
        "documents": documents,
        "queue": queue,
        "cases": cases,
        "claims": ClaimStore(tmp_path / "claims"),
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

    def test_a_processed_document_reads_as_done(self, env):
        doc_id = add_document(env, sha="d1")
        env["cases"].put_artifact(
            RunContext(tenant_id=TENANT, case_id=doc_id),
            "extraction",
            {"invoice": {"vendor_name": "Acme"}},
        )
        [doc] = env["client"]().get("/api/documents").json()["documents"]
        assert doc["stage"] == "done"

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
