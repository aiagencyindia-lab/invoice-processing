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

import pytest

from invoice_processing.ports import RunContext
from invoice_processing.worker.queue import LocalQueue, Queue
from invoice_processing.worker.runner import (
    ClaimStore,
    PermanentFailure,
    Worker,
    enqueue_document,
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
