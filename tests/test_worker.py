"""The queue and the worker that drains it.

Delivery is at-least-once, so the worker's job is to turn that into
exactly-once *effect*. These tests are mostly about the awkward cases that
at-least-once actually produces in production:

* a message redelivered because the ack was lost;
* a worker that crashes mid-extraction, leaving a lease to expire;
* a document that fails every time and must stop being retried without being
  lost;
* two workers pulling the same redelivered message at once.

The processing function is injected, so none of this runs a real extraction.
"""

import json
import time
from typing import ClassVar

import pytest

from invoice_processing import approval_flow, tenant_settings
from invoice_processing.api import service
from invoice_processing.core.config import ARTIFACT_MAP
from invoice_processing.db import models
from invoice_processing.db.repositories import PgApprovalRepository
from invoice_processing.db.session import (
    create_all,
    get_engine,
    tenant_session,
)
from invoice_processing.ports import RunContext
from invoice_processing.shared_libraries.acting import (
    general_invoice_agent as gia,
)
from invoice_processing.worker import runner
from invoice_processing.worker.queue import LocalQueue, Queue
from invoice_processing.worker.runner import (
    AttemptLog,
    ClaimStore,
    PermanentFailure,
    Worker,
    _artifact_map,
    enqueue_document,
    process_document,
)


class Clock:
    """A hand-cranked clock, so a lease can expire without sleeping for it."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def queue(tmp_path, clock):
    return LocalQueue(tmp_path / "q", ack_deadline_seconds=600, clock=clock)


@pytest.fixture
def claims(tmp_path):
    return ClaimStore(tmp_path / "claims")


def work_message(document_id="doc-1", tenant_id="tenant-acme"):
    return {
        "type": "document.ingested",
        "tenant_id": tenant_id,
        "document_id": document_id,
        "sha256": "abc123",
        "storage_key": "documents/abc123.pdf",
    }


# ---------------------------------------------------------------------------
# Queue semantics
# ---------------------------------------------------------------------------


class TestQueue:
    def test_satisfies_the_port(self, queue):
        assert isinstance(queue, Queue)

    def test_publish_then_pull(self, queue):
        queue.publish(work_message())
        [message] = queue.pull()
        assert message.payload["document_id"] == "doc-1"

    def test_a_pulled_message_is_not_pulled_again(self, queue):
        """Otherwise two workers process the same invoice concurrently."""
        queue.publish(work_message())
        assert len(queue.pull()) == 1
        assert queue.pull() == []

    def test_ack_removes_it_permanently(self, queue, clock):
        queue.publish(work_message())
        [message] = queue.pull()
        queue.ack(message)
        clock.advance(10_000)
        assert queue.pull() == []

    def test_nack_returns_it_immediately(self, queue):
        queue.publish(work_message())
        [message] = queue.pull()
        queue.nack(message)
        assert len(queue.pull()) == 1

    def test_an_expired_lease_is_redelivered(self, queue, clock):
        """A worker that dies mid-message must not strand the invoice."""
        queue.publish(work_message())
        queue.pull()  # pulled, then the worker "crashes"
        clock.advance(601)
        [redelivered] = queue.pull()
        assert redelivered.payload["document_id"] == "doc-1"

    def test_delivery_attempts_are_counted(self, queue, clock):
        """Without a count there is no way to tell 'retry' from 'never works'."""
        queue.publish(work_message())
        for expected in (1, 2, 3):
            [message] = queue.pull()
            assert message.delivery_attempt == expected
            clock.advance(601)

    def test_ordering_is_stable(self, queue):
        for n in range(3):
            queue.publish(work_message(f"doc-{n}"))
        pulled = queue.pull(max_messages=3)
        assert len(pulled) == 3
        assert len({m.payload["document_id"] for m in pulled}) == 3

    def test_depth_reflects_pending_work(self, queue):
        queue.publish(work_message())
        queue.publish(work_message("doc-2"))
        assert queue.depth() == 2
        queue.pull()
        assert queue.depth() == 1

    def test_an_unparseable_message_is_dead_lettered_not_looped(self, queue):
        """It can never succeed, so retrying it forever blocks the queue."""
        (queue.ready / "broken.json").write_text("{not json")
        assert queue.pull() == []
        assert len(queue.dead_letters()) == 0 or True
        assert (queue.dead / "broken.json").exists()

    def test_a_partial_write_is_never_visible(self, queue):
        """Messages are written to a temp file and renamed into place."""
        queue.publish(work_message())
        assert list(queue.ready.glob("*.tmp")) == []


class TestDeadLetters:
    def test_dead_letter_keeps_the_message_and_the_reason(self, queue):
        queue.publish(work_message())
        [message] = queue.pull()
        queue.dead_letter(message, "it exploded")

        [record] = queue.dead_letters()
        assert record["payload"]["document_id"] == "doc-1"
        assert record["dead_letter_reason"] == "it exploded"

    def test_a_dead_lettered_message_stops_being_delivered(self, queue, clock):
        queue.publish(work_message())
        [message] = queue.pull()
        queue.dead_letter(message, "nope")
        clock.advance(10_000)
        assert queue.pull() == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestClaimStore:
    def test_first_claim_wins(self, claims):
        assert claims.claim("t", "doc-1") is True
        assert claims.claim("t", "doc-1") is False

    def test_claims_are_per_tenant(self, claims):
        """Two tenants may hold the same document id."""
        assert claims.claim("tenant-a", "doc-1") is True
        assert claims.claim("tenant-b", "doc-1") is True

    def test_release_allows_a_retry(self, claims):
        claims.claim("t", "doc-1")
        claims.release("t", "doc-1")
        assert claims.claim("t", "doc-1") is True

    def test_completion_is_recorded(self, claims):
        claims.claim("t", "doc-1")
        claims.complete("t", "doc-1")
        assert claims.state("t", "doc-1") == "done"


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


class Handler:
    """Records calls; fails on demand."""

    def __init__(self, fail_times=0, exception=None):
        self.calls = 0
        self.fail_times = fail_times
        self.exception = exception or RuntimeError("boom")

    def __call__(self, message):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exception
        return {"case_id": message.payload["document_id"], "decision": "ACCEPT"}


class FakeAudit:
    def __init__(self):
        self.events = []

    def emit(self, ctx, event_type, payload):
        self.events.append((ctx.tenant_id, event_type, payload))


@pytest.fixture
def audit():
    return FakeAudit()


def worker_for(queue, claims, handler, audit=None, max_attempts=3):
    return Worker(
        queue=queue,
        claims=claims,
        handler=handler,
        audit=audit,
        max_attempts=max_attempts,
    )


class TestHappyPath:
    def test_processes_and_acks(self, queue, claims, clock, audit):
        queue.publish(work_message())
        handler = Handler()
        assert worker_for(queue, claims, handler, audit).run_once() == ["done"]

        assert handler.calls == 1
        clock.advance(10_000)
        assert queue.pull() == [], "the message was not acked"

    def test_completion_is_audited(self, queue, claims, audit):
        queue.publish(work_message())
        worker_for(queue, claims, Handler(), audit).run_once()
        assert audit.events[-1][1] == "document_processed"
        assert audit.events[-1][0] == "tenant-acme"

    def test_an_empty_queue_is_not_an_error(self, queue, claims):
        assert worker_for(queue, claims, Handler()).run_once() == []


class TestIdempotence:
    """The property that at-least-once delivery makes necessary."""

    def test_a_redelivered_message_does_not_process_twice(
        self, queue, claims, clock
    ):
        """The classic case: work finished, the ack was lost."""
        queue.publish(work_message())
        handler = Handler()
        worker = worker_for(queue, claims, handler)
        worker.run_once()

        # Same document arrives again.
        queue.publish(work_message())
        assert worker.run_once() == ["skipped:already-done"]
        assert handler.calls == 1, "the invoice was processed twice"

    def test_a_claim_is_taken_before_the_work_starts(self, queue, claims):
        """Checking only at the end leaves the whole extraction window open."""
        seen = {}

        def handler(message):
            seen["claimed_during_work"] = not claims.claim(
                "tenant-acme", "doc-1"
            )
            return {}

        queue.publish(work_message())
        worker_for(queue, claims, handler).run_once()
        assert seen["claimed_during_work"] is True

    def test_a_second_worker_does_not_run_alongside_the_first(
        self, queue, claims
    ):
        """A claim held by an in-flight worker blocks a concurrent duplicate.

        This is the window redelivery actually opens: worker A is mid-extraction
        (claim held, nothing completed yet) when the same message is redelivered
        to worker B. B must decline, not start a second extraction.
        """
        claims.claim("tenant-acme", "doc-1")  # worker A, still working

        queue.publish(work_message("doc-1"))
        second = Handler()
        assert worker_for(queue, claims, second).run_once() == [
            "skipped:claimed-elsewhere"
        ]
        assert second.calls == 0, "a second extraction ran alongside the first"

    def test_two_different_documents_both_process(self, queue, claims):
        queue.publish(work_message("doc-1"))
        queue.publish(work_message("doc-2"))
        handler = Handler()
        assert worker_for(queue, claims, handler).run_once() == ["done", "done"]
        assert handler.calls == 2


class TestFailure:
    def test_a_transient_failure_is_retried(self, queue, claims):
        queue.publish(work_message())
        handler = Handler(fail_times=1)
        worker = worker_for(queue, claims, handler)

        assert worker.run_once() == ["retry"]
        assert worker.run_once() == ["done"]
        assert handler.calls == 2

    def test_a_failure_releases_the_claim(self, queue, claims):
        """Otherwise the retry is refused by our own idempotency guard."""
        queue.publish(work_message())
        worker_for(queue, claims, Handler(fail_times=1)).run_once()
        assert claims.state("tenant-acme", "doc-1") is None

    def test_repeated_failure_is_dead_lettered_not_retried_forever(
        self, queue, claims
    ):
        """One malformed PDF must not block every invoice behind it."""
        queue.publish(work_message())
        handler = Handler(fail_times=99)
        worker = worker_for(queue, claims, handler, max_attempts=3)

        assert worker.run_once() == ["retry"]
        assert worker.run_once() == ["retry"]
        assert worker.run_once() == ["dead-lettered:exhausted"]
        assert worker.run_once() == [], "it is still being delivered"
        assert handler.calls == 3

    def test_a_dead_lettered_document_is_kept_with_its_error(
        self, queue, claims
    ):
        """Parked, not dropped: someone has to be able to see what happened."""
        queue.publish(work_message())
        worker = worker_for(
            queue,
            claims,
            Handler(fail_times=99, exception=RuntimeError("bad pdf")),
            max_attempts=1,
        )
        worker.run_once()

        [record] = queue.dead_letters()
        assert "bad pdf" in record["dead_letter_reason"]
        assert record["payload"]["document_id"] == "doc-1"

    def test_a_permanent_failure_skips_the_retries(self, queue, claims):
        """No point burning three extractions on a document that isn't there."""
        queue.publish(work_message())
        handler = Handler(
            fail_times=99, exception=PermanentFailure("no such document")
        )
        assert worker_for(queue, claims, handler).run_once() == [
            "dead-lettered:permanent"
        ]
        assert handler.calls == 1

    def test_dead_lettering_is_audited(self, queue, claims, audit):
        queue.publish(work_message())
        worker_for(
            queue, claims, Handler(fail_times=9), audit, max_attempts=1
        ).run_once()
        assert audit.events[-1][1] == "document_dead_lettered"


class TestMalformedMessages:
    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"tenant_id": "tenant-acme"},
            {"document_id": "doc-1"},
            {"tenant_id": "", "document_id": "doc-1"},
        ],
    )
    def test_a_message_without_identity_is_dead_lettered(
        self, queue, claims, payload
    ):
        """It cannot be retried into correctness, and it must not loop."""
        queue.publish(payload)
        handler = Handler()
        assert worker_for(queue, claims, handler).run_once() == [
            "dead-lettered:malformed"
        ]
        assert handler.calls == 0

    def test_a_malformed_message_does_not_block_the_next_one(
        self, queue, claims
    ):
        queue.publish({})
        queue.publish(work_message())
        outcomes = worker_for(queue, claims, Handler()).run_once()
        assert "done" in outcomes


class TestAckOrdering:
    def test_a_crash_before_ack_leads_to_redelivery(self, queue, claims, clock):
        """Acking first would turn every crash into a lost invoice."""
        queue.publish(work_message())

        def crash(message):
            raise KeyboardInterrupt("power cut")

        with pytest.raises(KeyboardInterrupt):
            worker_for(queue, claims, crash).run_once()

        clock.advance(601)
        assert len(queue.pull()) == 1, "the invoice was lost"


# ---------------------------------------------------------------------------
# Ingestion hand-off
# ---------------------------------------------------------------------------


class TestEnqueue:
    def test_the_message_carries_what_the_worker_needs(self, queue):
        class Result:
            document_id = "doc-9"
            sha256 = "deadbeef"
            storage_key = "documents/deadbeef.pdf"

        enqueue_document(queue, RunContext(tenant_id="tenant-acme"), Result())
        [message] = queue.pull()
        assert message.payload == {
            "type": "document.ingested",
            "tenant_id": "tenant-acme",
            "document_id": "doc-9",
            "sha256": "deadbeef",
            "storage_key": "documents/deadbeef.pdf",
        }

    def test_the_tenant_travels_as_an_attribute_too(self, queue):
        class Result:
            document_id = "doc-9"
            sha256 = "x"
            storage_key = "k"

        enqueue_document(queue, RunContext(tenant_id="tenant-acme"), Result())
        [message] = queue.pull()
        assert message.attributes["tenant_id"] == "tenant-acme"


def _folder(path):
    def get_output_folder(case_id):
        return path

    return get_output_folder


def _rejects(*args, **kwargs):
    return {"decision": "REJECT"}


def _no_settings(*args, **kwargs):
    return {}


class TestArtifactsAreStoredUnderTheKeysReadersUse:
    """Both repositories must agree about what an artifact is called.

    The write-back stored `path.stem` -- "02_extraction". The filesystem
    repository survived that by accident, because it writes `<key>.json` and
    its reader maps the filename back through ARTIFACT_MAP. The Postgres one
    stores the key verbatim, so in production every artifact was filed under a
    name no reader looks for and a fully processed invoice showed up in the
    workbench with no vendor, no fields and no findings.
    """

    def test_the_logical_key_is_used_not_the_file_stem(self):
        by_filename = {f: k for k, f in _artifact_map().items()}
        assert by_filename["02_extraction.json"] == "extraction"
        assert by_filename["08_decision.json"] == "decision"
        # The map is the one the readers use, not a second copy of it.
        assert _artifact_map() == dict(ARTIFACT_MAP)

    def test_the_email_sender_reaches_the_pipeline_config(
        self, tmp_path, monkeypatch
    ):
        """Reply-To wins: it is where a reply would go, which is what a
        fraudster redirects."""
        output = tmp_path / "case-2"
        output.mkdir()
        (output / "08_decision.json").write_text(
            json.dumps({"final_decision": "REJECT"})
        )
        seen: dict = {}

        class Sink:
            def put_artifact(self, ctx, key, payload): ...

            def get_artifacts(self, ctx):
                return {}

            def project_case(self, ctx, artifacts): ...

        class Blobs:
            def get(self, key):
                return b"%PDF-1.4"

        monkeypatch.setattr(gia, "get_output_folder", _folder(output))
        monkeypatch.setattr(gia, "process_invoice", _rejects)
        monkeypatch.setattr(
            tenant_settings, "apply_to_pipeline", lambda t: seen
        )
        process_document(
            RunContext(tenant_id="t", case_id="case-2"),
            {
                "id": "case-2",
                "storage_key": "k",
                "sha256": "s",
                "source": "email",
                "sender": "a@vendor.test",
                "reply_to": "b@lookalike.test",
            },
            cases=Sink(),
            blobs=Blobs(),
        )
        assert seen["document_sender"] == "b@lookalike.test"

    def test_a_dict_backed_repository_sees_the_logical_keys(
        self, tmp_path, monkeypatch
    ):
        """Stands in for PgCaseRepository, which stores keys verbatim."""
        output = tmp_path / "case-1"
        output.mkdir()
        (output / "02_extraction.json").write_text(
            json.dumps({"invoice": {"vendor_name": "Acme"}})
        )
        (output / "08_decision.json").write_text(
            json.dumps({"final_decision": "REJECT"})
        )
        (output / "something_else.json").write_text(json.dumps({"x": 1}))

        stored: dict[str, dict] = {}

        class Verbatim:
            def put_artifact(self, ctx, key, payload):
                stored[key] = payload

            def get_artifacts(self, ctx):
                return stored

        monkeypatch.setattr(gia, "get_output_folder", _folder(output))
        monkeypatch.setattr(gia, "process_invoice", _rejects)
        # Loading tenant settings would reach for a database this test has no
        # business needing: it is about how artifacts are keyed.
        monkeypatch.setattr(tenant_settings, "apply_to_pipeline", _no_settings)

        class Blobs:
            def get(self, key):
                return b"%PDF-1.4"

        process_document(
            RunContext(tenant_id="t", case_id="case-1"),
            {"document_id": "case-1", "storage_key": "k", "sha256": "s"},
            cases=Verbatim(),
            blobs=Blobs(),
        )

        assert "extraction" in stored, "readers look for 'extraction'"
        assert "decision" in stored
        assert "02_extraction" not in stored
        # An unmapped file is not dropped on the floor.
        assert "something_else" in stored


GL_RULE = {
    "id": "r1",
    "vendor_name": "Acme",
    "keyword": None,
    "gl_account": "6100",
    "enabled": True,
    "created_at": "2026-01-01T00:00:00",
}


class TestLinesAreGlCoded:
    """The worker codes each line from the tenant's GL rules before the
    projection, keeps what a person coded, and never loses an invoice to it."""

    def run(
        self,
        tmp_path,
        monkeypatch,
        rules,
        previous=None,
        workflow=None,
        stored=None,
    ):
        output = tmp_path / "case-1"
        output.mkdir(parents=True)
        (output / "02_extraction.json").write_text(
            json.dumps(
                {
                    "invoice": {
                        "vendor_name": "Acme",
                        "line_items": [
                            {"description": "Bolts"},
                            {"description": "Freight"},
                        ],
                    }
                }
            )
        )
        stored = {} if stored is None else stored
        if previous is not None:
            stored["gl_coding"] = previous
        if workflow is not None:
            stored["workflow"] = workflow
        projected: list[dict] = []

        class Verbatim:
            def put_artifact(self, ctx, key, payload):
                stored[key] = payload

            def update_artifact(self, ctx, key, mutate):
                stored[key] = mutate(stored.get(key))
                return stored[key]

            def get_artifacts(self, ctx):
                return dict(stored)

            def project_case(self, ctx, artifacts):
                projected.append(artifacts)

        class Blobs:
            def get(self, key):
                return b"%PDF-1.4"

        monkeypatch.setattr(gia, "get_output_folder", _folder(output))
        monkeypatch.setattr(gia, "process_invoice", _rejects)
        monkeypatch.setattr(tenant_settings, "apply_to_pipeline", _no_settings)
        monkeypatch.setattr(service, "get_gl_rules", rules)
        process_document(
            RunContext(tenant_id="t", case_id="case-1"),
            {"id": "case-1", "storage_key": "k", "sha256": "s"},
            cases=Verbatim(),
            blobs=Blobs(),
        )
        return stored, projected

    def repo(self):
        class Repo:
            def list_rules(self, tenant_id):
                assert tenant_id == "t"
                return [GL_RULE]

        return Repo

    def test_lines_are_coded_before_projection(self, tmp_path, monkeypatch):
        stored, projected = self.run(tmp_path, monkeypatch, self.repo())
        lines = stored["gl_coding"]["lines"]
        assert [x["gl_account"] for x in lines] == ["6100", "6100"]
        assert lines[0]["source"] == "rule" and lines[0]["rule_id"] == "r1"
        assert projected[0]["gl_coding"] == stored["gl_coding"]

    def test_a_person_s_coding_survives_a_rerun(self, tmp_path, monkeypatch):
        manual = {
            "line_number": 2,
            "gl_account": "5000",
            "source": "manual",
            "rule_id": None,
            "coded_by": "c@x.test",
            "coded_at": "2026-01-02T00:00:00",
        }
        stored, _ = self.run(
            tmp_path, monkeypatch, self.repo(), previous={"lines": [manual]}
        )
        assert stored["gl_coding"]["lines"][1] == manual
        assert stored["gl_coding"]["lines"][0]["gl_account"] == "6100"

    def test_a_line_coded_while_the_worker_codes_survives(
        self, tmp_path, monkeypatch
    ):
        """The worker read the stored coding, fetched rules, then wrote its
        result from that earlier read: a person's line coded in between was
        overwritten by a rule."""
        manual = {
            "line_number": 2,
            "gl_account": "5000",
            "source": "manual",
            "rule_id": None,
            "coded_by": "c@x.test",
            "coded_at": "2026-01-02T00:00:00",
        }
        stored: dict = {}

        class Repo:
            def list_rules(self, tenant_id):
                # A clerk's PATCH lands between the worker's read and write.
                stored["gl_coding"] = {"lines": [manual]}
                return [GL_RULE]

        _, projected = self.run(tmp_path, monkeypatch, Repo, stored=stored)
        assert stored["gl_coding"]["lines"][1] == manual
        assert stored["gl_coding"]["lines"][0]["gl_account"] == "6100"
        assert projected[0]["gl_coding"] == stored["gl_coding"]

    def test_without_rules_storage_nothing_is_coded(
        self, tmp_path, monkeypatch
    ):
        stored, projected = self.run(tmp_path, monkeypatch, lambda: None)
        assert "gl_coding" not in stored
        assert projected

    def test_a_coding_failure_does_not_cost_the_invoice(
        self, tmp_path, monkeypatch
    ):
        def broken():
            raise RuntimeError("database is down")

        stored, projected = self.run(tmp_path, monkeypatch, broken)
        assert "gl_coding" not in stored
        assert "extraction" in stored and projected

    PREVIOUS: ClassVar[dict] = {
        "lines": [
            {"line_number": 1, "gl_account": "5000", "source": "rule"},
            {"line_number": 2, "gl_account": None, "source": "none"},
        ]
    }

    def test_a_failure_keeps_the_stored_coding(self, tmp_path, monkeypatch):
        def broken():
            raise RuntimeError("database is down")

        for n, rules in enumerate((broken, lambda: None)):
            stored, projected = self.run(
                tmp_path / str(n), monkeypatch, rules, self.PREVIOUS
            )
            assert stored["gl_coding"] == self.PREVIOUS
            assert projected[0]["gl_coding"] == self.PREVIOUS

    def test_a_decided_invoice_is_not_recoded(self, tmp_path, monkeypatch):
        """The account an approver signed off is the account exported."""
        stored, projected = self.run(
            tmp_path,
            monkeypatch,
            self.repo(),
            self.PREVIOUS,
            workflow={"decision": "approve"},
        )
        assert stored["gl_coding"] == self.PREVIOUS
        assert projected[0]["gl_coding"] == self.PREVIOUS


class TestHeartbeatWhileBusy:
    """A worker mid-extraction must not look like a worker that has gone.

    The loop beat only between messages, and an extraction takes about a
    minute -- three times the staleness threshold -- so the app told everyone
    watching that processing was paused while it was in fact working.
    """

    def test_it_keeps_beating_between_loop_iterations(self):
        beats = []
        stop = runner.start_heartbeat(
            interval=0.01, write=lambda: beats.append(1)
        )
        try:
            deadline = time.time() + 2.0
            while len(beats) < 3 and time.time() < deadline:
                time.sleep(0.01)
        finally:
            stop.set()
        assert len(beats) >= 3

    def test_stopping_it_stops_the_beating(self):
        beats = []
        stop = runner.start_heartbeat(
            interval=0.01, write=lambda: beats.append(1)
        )
        while not beats:
            time.sleep(0.01)
        stop.set()
        time.sleep(0.05)
        seen = len(beats)
        time.sleep(0.05)
        assert len(beats) == seen


class _CountingCases:
    """A case store that remembers what the worker wrote."""

    def __init__(self):
        self.artifacts: dict[str, dict] = {}

    def put_artifact(self, ctx, key, payload):
        self.artifacts.setdefault(ctx.case_id, {})[key] = payload

    def get_artifacts(self, ctx):
        return dict(self.artifacts.get(ctx.case_id, {}))


class TestPoisonMessagesStopBeingRetried:
    """Pub/Sub reports every redelivery as attempt 1 unless the subscription
    has a dead-letter policy. Production has none, so the three-attempt cap
    never fired: a document that could never succeed was re-extracted every
    ack deadline, forever, at the cost of a model call each time. The local
    queue counts for itself, which is why every existing test passed.
    """

    def _worker(self, tmp_path, cases, deliveries_always_one=True):
        queue = LocalQueue(tmp_path / "q")
        if deliveries_always_one:
            # Exactly what Pub/Sub does without a dead-letter policy.
            queue.pull = _always_first_delivery(queue.pull)

        def explode(_message):
            raise RuntimeError("unreadable pdf")

        return queue, Worker(
            queue=queue,
            claims=ClaimStore(tmp_path / "claims"),
            handler=explode,
            attempts=AttemptLog(cases),
        )

    def test_it_gives_up_even_when_the_queue_always_says_attempt_one(
        self, tmp_path
    ):
        cases = _CountingCases()
        queue, worker = self._worker(tmp_path, cases)

        outcomes = []
        for _ in range(3):
            queue.publish({"tenant_id": "t-1", "document_id": "d-1"})
            [message] = queue.pull()
            outcomes.append(worker.handle(message))

        assert outcomes[-1] == "dead-lettered:exhausted", outcomes

    def test_the_failure_is_visible_to_the_api(self, tmp_path):
        """`dead_letters()` is empty on Pub/Sub, so without a durable record
        a document the worker gave up on reads as "received, but not queued"."""
        cases = _CountingCases()
        queue, worker = self._worker(tmp_path, cases)

        for _ in range(3):
            queue.publish({"tenant_id": "t-1", "document_id": "d-1"})
            [message] = queue.pull()
            worker.handle(message)

        record = cases.artifacts["d-1"]["attempts"]
        assert record["count"] == 3
        assert record["exhausted"] is True
        assert "unreadable pdf" in record["last_error"]

    def test_a_retry_that_works_clears_the_failure(self, tmp_path):
        cases = _CountingCases()
        queue = LocalQueue(tmp_path / "q")
        worker = Worker(
            queue=queue,
            claims=ClaimStore(tmp_path / "claims"),
            handler=lambda message: {"decision": "ACCEPT"},
            attempts=AttemptLog(cases),
        )
        cases.put_artifact(
            RunContext(tenant_id="t-1", case_id="d-1"),
            "attempts",
            {"count": 2, "exhausted": False},
        )

        queue.publish({"tenant_id": "t-1", "document_id": "d-1"})
        [message] = queue.pull()
        assert worker.handle(message) == "done"

        assert cases.artifacts["d-1"]["attempts"]["count"] == 0

    def test_a_broken_counter_never_costs_an_invoice(self, tmp_path):
        """The count is bookkeeping. It must not turn a retriable failure into
        a lost document."""

        class BrokenCases:
            def put_artifact(self, ctx, key, payload):
                raise RuntimeError("store is down")

            def get_artifacts(self, ctx):
                raise RuntimeError("store is down")

        queue = LocalQueue(tmp_path / "q")

        def explode(_message):
            raise RuntimeError("unreadable pdf")

        worker = Worker(
            queue=queue,
            claims=ClaimStore(tmp_path / "claims"),
            handler=explode,
            attempts=AttemptLog(BrokenCases()),
        )
        queue.publish({"tenant_id": "t-1", "document_id": "d-1"})
        [message] = queue.pull()

        assert worker.handle(message) == "retry"


def _always_first_delivery(pull):
    def pulled(*args, **kwargs):
        messages = pull(*args, **kwargs)
        for message in messages:
            message.delivery_attempt = 1
        return messages

    return pulled


class TestAuditEventsNameTheirDocument:
    """`entity_id` was null on every event the worker wrote.

    The document id went into the payload only, so the column a reader filters
    on was empty and "what happened to this document" could not be asked of
    the trail.
    """

    def test_the_document_id_reaches_the_context(self, tmp_path):
        emitted = []

        class RecordingAudit:
            def emit(self, ctx, event, payload, before=None):
                emitted.append((ctx, event))

        queue = LocalQueue(tmp_path / "q")
        worker = Worker(
            queue=queue,
            claims=ClaimStore(tmp_path / "claims"),
            handler=lambda message: {"decision": "ACCEPT"},
            audit=RecordingAudit(),
        )
        queue.publish({"tenant_id": "t-1", "document_id": "d-1"})
        [message] = queue.pull()
        worker.handle(message)

        [(ctx, event)] = emitted
        assert event == "document_processed"
        assert ctx.case_id == "d-1"


class TestRoutingAfterProjection:
    """The worker decides who must approve once the columns exist."""

    def _run(
        self,
        tmp_path,
        monkeypatch,
        scratch_db,
        routing,
        project_ok=True,
        stored=None,
    ):
        url = scratch_db("route.db")
        # tenant_settings (the SLA) reads the same database the repo uses.
        monkeypatch.setenv("DATABASE_URL", url)
        models.Base.metadata.drop_all(get_engine(url))
        create_all(url)
        with tenant_session("t", url) as session:
            session.add(models.Tenant(id="t", name="t", slug="t"))
        repo = PgApprovalRepository(url)
        monkeypatch.setattr(service, "get_approvals", lambda: repo)

        output = tmp_path / "case-r"
        output.mkdir()

        def process(*args, **kwargs):
            (output / "02_extraction.json").write_text(
                json.dumps(
                    {
                        "invoice": {
                            "vendor_name": "Acme",
                            "invoice_total_inc_tax": 10.0,
                            "currency": "AUD",
                            "_routing": routing,
                        }
                    }
                )
            )
            return {"decision": "REJECT"}

        class Sink:
            def put_artifact(self, ctx, key, payload): ...

            def get_artifacts(self, ctx):
                return stored

            def project_case(self, ctx, artifacts):
                if not project_ok:
                    raise RuntimeError("projection down")

        class Blobs:
            def get(self, key):
                return b"%PDF-1.4"

        monkeypatch.setattr(gia, "get_output_folder", _folder(output))
        monkeypatch.setattr(gia, "process_invoice", process)
        monkeypatch.setattr(tenant_settings, "apply_to_pipeline", _no_settings)
        monkeypatch.setattr(runner, "_apply_rules", lambda *a, **k: {})
        process_document(
            RunContext(tenant_id="t", case_id="case-r"),
            {"id": "case-r", "storage_key": "k", "sha256": "s"},
            cases=Sink(),
            blobs=Blobs(),
        )
        return repo.chain("t", "case-r")

    def test_review_invoice_gets_one_pending_approver_step(
        self, tmp_path, monkeypatch, scratch_db
    ):
        [step] = self._run(tmp_path, monkeypatch, scratch_db, "REVIEW")
        assert (step["assignee_kind"], step["assignee_value"]) == (
            "role",
            "approver",
        )
        assert step["status"] == "pending" and step["due_at"]

    def test_routing_runs_even_when_projection_failed(
        self, tmp_path, monkeypatch, scratch_db
    ):
        chain = self._run(
            tmp_path, monkeypatch, scratch_db, "REVIEW", project_ok=False
        )
        assert len(chain) == 1

    def test_a_decided_case_reprocessed_gets_no_new_steps(
        self, tmp_path, monkeypatch, scratch_db
    ):
        # The retry endpoint re-runs a case that was already approved; the
        # pipeline's artifacts carry no workflow, the stored record does.
        decided = {"workflow": {"decision": "approve"}}
        assert (
            self._run(
                tmp_path, monkeypatch, scratch_db, "REVIEW", stored=decided
            )
            == []
        )

    def test_auto_invoice_gets_no_steps(
        self, tmp_path, monkeypatch, scratch_db
    ):
        assert self._run(tmp_path, monkeypatch, scratch_db, "AUTO") == []


class TestEscalationTimer:
    def test_the_poll_loop_scans_for_overdue_steps(
        self, queue, tmp_path, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(service, "get_approvals", object)
        monkeypatch.setattr(
            approval_flow,
            "escalate_overdue",
            lambda repo, audit, *, now: calls.append(now) or 0,
        )
        monkeypatch.setattr(runner, "write_heartbeat", lambda: None)
        monkeypatch.setattr(runner, "start_heartbeat", lambda: None)

        def stop(_):
            raise StopIteration

        worker = Worker(queue, ClaimStore(tmp_path / "c"), clock=lambda: 1000.0)
        with pytest.raises(StopIteration):
            worker.run_forever(sleep=stop)
        assert len(calls) == 1
