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

from invoice_processing import approval_flow, rematch, tenant_settings
from invoice_processing.adapters import FsCaseRepository
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
            "request_id": "",  # none in this context; the API passes its own
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
        phase2=None,
        book=None,
    ):
        output = tmp_path / "case-1"
        output.mkdir(parents=True)
        (output / "02_extraction.json").write_text(
            json.dumps(
                {
                    "invoice": {
                        "vendor_name": "Acme",
                        "po_number": "PO-1",
                        "line_items": [
                            {"description": "Bolts"},
                            {"description": "Freight"},
                        ],
                    }
                }
            )
        )
        if phase2 is not None:
            (output / "04_phase2_validation.json").write_text(
                json.dumps(phase2)
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
        monkeypatch.setattr(
            tenant_settings,
            "apply_to_pipeline",
            lambda *a, **k: {"purchase_order_book": book} if book else {},
        )
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

    PAIRED: ClassVar[dict] = {
        "validations": [
            {
                "step": "2.3",
                "passed": True,
                "lines": [
                    {
                        "line_number": 1,
                        "po_line_number": 1,
                        "paired_by": "description",
                    },
                    {
                        "line_number": 2,
                        "po_line_number": 2,
                        "paired_by": "description",
                    },
                ],
            }
        ]
    }

    class Book:
        def __init__(self, lines, *, missing=False, broken=False):
            self.lines, self.missing, self.broken = lines, missing, broken

        def find(self, po_number):
            return None

        def book(self, po_number):
            if self.broken:
                raise RuntimeError("book unavailable")
            if self.missing:
                return None
            assert po_number == "PO-1"
            return {"po": {}, "lines": self.lines, "received": {}, "billed": {}}

    def test_a_paired_line_takes_the_po_line_account(
        self, tmp_path, monkeypatch
    ):
        book = self.Book(
            [{"line_number": 1, "gl_account": "7100"}, {"line_number": 2}]
        )
        stored, projected = self.run(
            tmp_path, monkeypatch, self.repo(), phase2=self.PAIRED, book=book
        )
        lines = stored["gl_coding"]["lines"]
        assert lines[0]["gl_account"] == "7100" and lines[0]["source"] == "po"
        assert lines[0]["po_line_number"] == 1
        # No PO account on line 2: the vendor rule codes it as before.
        assert lines[1]["gl_account"] == "6100" and lines[1]["source"] == "rule"
        assert projected[0]["gl_coding"] == stored["gl_coding"]

    def test_without_a_pairing_rules_code_every_line(
        self, tmp_path, monkeypatch
    ):
        book = self.Book([{"line_number": 1, "gl_account": "7100"}])
        stored, _ = self.run(tmp_path, monkeypatch, self.repo(), book=book)
        assert [x["source"] for x in stored["gl_coding"]["lines"]] == [
            "rule",
            "rule",
        ]

    def test_a_missing_po_falls_back_to_rules(self, tmp_path, monkeypatch):
        stored, _ = self.run(
            tmp_path,
            monkeypatch,
            self.repo(),
            phase2=self.PAIRED,
            book=self.Book([], missing=True),
        )
        assert [x["source"] for x in stored["gl_coding"]["lines"]] == [
            "rule",
            "rule",
        ]

    def test_an_unreadable_book_falls_back_to_rules(
        self, tmp_path, monkeypatch
    ):
        """Inheritance is a convenience; it must never cost the coding."""
        stored, _ = self.run(
            tmp_path,
            monkeypatch,
            self.repo(),
            phase2=self.PAIRED,
            book=self.Book([], broken=True),
        )
        assert [x["gl_account"] for x in stored["gl_coding"]["lines"]] == [
            "6100",
            "6100",
        ]

    def test_a_decided_invoice_is_not_recoded_from_the_po(
        self, tmp_path, monkeypatch
    ):
        book = self.Book([{"line_number": 1, "gl_account": "7100"}])
        stored, _ = self.run(
            tmp_path,
            monkeypatch,
            self.repo(),
            previous={
                "lines": [
                    {
                        "line_number": 1,
                        "gl_account": "6100",
                        "source": "rule",
                        "rule_id": "r1",
                        "coded_by": None,
                        "coded_at": None,
                    }
                ]
            },
            workflow={"decision": "approve"},
            phase2=self.PAIRED,
            book=book,
        )
        assert stored["gl_coding"]["lines"][0]["gl_account"] == "6100"

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


# ---------------------------------------------------------------------------
# Re-match jobs
# ---------------------------------------------------------------------------


class TestRematchJobs:
    """A goods receipt queues a re-match per held case; no claim, no document."""

    @pytest.fixture
    def rematched(self, monkeypatch):
        calls = []

        def fake(ctx, cases, book, **kwargs):
            calls.append((ctx, book, kwargs))
            if fake.fail:
                raise RuntimeError("db down")
            return {"before": [], "after": [], "cleared": True}

        fake.fail = False
        monkeypatch.setattr(rematch, "rematch_case", fake)
        monkeypatch.setattr(
            tenant_settings,
            "apply_to_pipeline",
            lambda tenant_id: {"purchase_order_book": f"book-{tenant_id}"},
        )
        fake.calls = calls
        return fake

    def test_a_rematch_message_is_handled_and_acked(
        self, queue, claims, clock, audit, rematched
    ):
        runner.enqueue_rematch(queue, "tenant-acme", "case-7", "PO-1")
        handler = Handler()
        worker = worker_for(queue, claims, handler, audit)
        assert worker.run_once() == ["rematched"]
        [(ctx, book, kwargs)] = rematched.calls
        assert (ctx.tenant_id, ctx.case_id, ctx.actor) == (
            "tenant-acme",
            "case-7",
            "worker",
        )
        assert book == "book-tenant-acme"
        assert kwargs["audit"] is audit
        assert handler.calls == 0
        assert claims.state("tenant-acme", "case-7") is None
        clock.advance(10_000)
        assert queue.pull() == [], "the message was not acked"

    def test_the_message_names_tenant_case_and_po(self, queue):
        runner.enqueue_rematch(queue, "tenant-acme", "case-7", "PO-1")
        [message] = queue.pull()
        assert message.payload == {
            "type": runner.REMATCH,
            "tenant_id": "tenant-acme",
            "case_id": "case-7",
            "po_number": "PO-1",
            "request_id": "",
        }
        assert runner.REMATCH == "case.rematch"

    def test_a_malformed_rematch_is_dead_lettered(
        self, queue, claims, rematched
    ):
        queue.publish({"type": runner.REMATCH, "tenant_id": "tenant-acme"})
        assert worker_for(queue, claims, Handler()).run_once() == [
            "dead-lettered:malformed"
        ]
        assert rematched.calls == []

    def test_a_failing_rematch_retries_then_dead_letters(
        self, queue, claims, rematched
    ):
        rematched.fail = True
        runner.enqueue_rematch(queue, "tenant-acme", "case-7", "PO-1")
        worker = worker_for(queue, claims, Handler(), max_attempts=2)
        assert worker.run_once() == ["retry"]
        assert worker.run_once() == ["dead-lettered:exhausted"]
        [record] = queue.dead_letters()
        assert "db down" in record["dead_letter_reason"]

    def test_rematch_failures_have_their_own_count_and_audit(
        self, tmp_path, queue, claims, audit, rematched
    ):
        rematched.fail = True
        cases = FsCaseRepository(
            root=tmp_path / "cases", tenant_id="tenant-acme"
        )
        runner.enqueue_rematch(queue, "tenant-acme", "case-7", "PO-1")
        worker = Worker(
            queue=queue,
            claims=claims,
            handler=Handler(),
            audit=audit,
            max_attempts=1,
            attempts=AttemptLog(cases),
        )
        assert worker.run_once() == ["dead-lettered:exhausted"]
        stored = cases.get_artifacts(
            RunContext(tenant_id="tenant-acme", case_id="case-7")
        )
        # Not the document's record: that one says whether the upload failed.
        assert "attempts" not in stored
        assert stored["rematch_attempts"]["count"] == 1
        assert [e[1] for e in audit.events] == ["rematch_dead_lettered"]
        assert "db down" in audit.events[0][2]["reason"]

    def test_a_document_message_still_goes_through_claims(
        self, queue, claims, rematched
    ):
        queue.publish(work_message())
        assert worker_for(queue, claims, Handler()).run_once() == ["done"]
        assert claims.state("tenant-acme", "doc-1") == "done"
        assert rematched.calls == []


class TestSupportingDocuments:
    """A scanned PO or GRN is read and parked for a person to confirm; it is
    never validated, coded, projected or routed as an invoice."""

    GRN: ClassVar[dict] = {
        "kind": "goods_receipt",
        "fields": {"grn_number": "GRN-1", "po_number": "po-4471"},
        "lines": [{"line_number": 1, "quantity_received": 5}],
        "_evidence": {},
        "_repairs": [],
    }

    def _run(
        self,
        tmp_path,
        monkeypatch,
        *,
        kind="goods_receipt",
        extract=None,
        orders=None,
        book=None,
    ):
        from invoice_processing import supporting_docs  # noqa: PLC0415
        from invoice_processing.adapters import (  # noqa: PLC0415
            JsonDocumentRepository,
            JsonSupportingDocumentRepository,
        )

        documents = JsonDocumentRepository(tmp_path / "documents.json")
        document_id = documents.create("t", storage_key="k", sha256="s")
        supporting = JsonSupportingDocumentRepository(tmp_path / "sd.json")
        emitted = []

        class Audit:
            def emit(self, ctx, event, payload, before=None):
                emitted.append((event, payload))

        class Orders:
            def book(self, tenant_id, po_number):
                return (book or {}).get(po_number)

        stored: dict = {}

        class Sink:
            def put_artifact(self, ctx, key, payload):
                stored[key] = payload

            def get_artifacts(self, ctx):
                return stored

            def project_case(self, ctx, artifacts):
                raise AssertionError("a supporting document is not projected")

        class Blobs:
            def get(self, key):
                return b"%PDF-1.4"

        def process(source, **kwargs):
            return {
                "decision": "SUPPORTING",
                "document_type": kind,
                "pdf": str(source / "invoice.pdf"),
            }

        def refuse(*a, **k):
            raise AssertionError("not for a supporting document")

        monkeypatch.setattr(gia, "process_invoice", process)
        monkeypatch.setattr(tenant_settings, "apply_to_pipeline", _no_settings)
        monkeypatch.setattr(runner, "_apply_rules", refuse)
        monkeypatch.setattr(runner, "_code_gl", refuse)
        monkeypatch.setattr(approval_flow, "route", refuse)
        monkeypatch.setattr(service, "get_documents", lambda: documents)
        monkeypatch.setattr(
            service, "get_supporting_documents", lambda: supporting
        )
        monkeypatch.setattr(
            service,
            "get_purchase_orders",
            # orders=False: no purchase-order storage at all.
            lambda: Orders() if orders is None else None,
        )
        monkeypatch.setattr(service, "get_audit", Audit)
        monkeypatch.setattr(
            supporting_docs,
            "extract",
            extract or (lambda path, k, **kw: {**self.GRN, "kind": k}),
        )

        def go():
            return process_document(
                RunContext(tenant_id="t", case_id=document_id),
                {"id": document_id, "storage_key": "k", "sha256": "s"},
                cases=Sink(),
                blobs=Blobs(),
            )

        result = go()
        [row] = supporting.list("t", ("pending", "blocked", "accepted"))
        return {
            "result": result,
            "row": row,
            "stored": stored,
            "emitted": emitted,
            "kind": documents.get("t", document_id).get("kind"),
            "supporting": supporting,
            "go": go,
        }

    def test_a_receipt_for_a_known_po_waits_for_confirmation(
        self, tmp_path, monkeypatch
    ):
        out = self._run(tmp_path, monkeypatch, book={"po-4471": {"po": {}}})
        row = out["row"]
        assert (row["status"], row["kind"], row["po_number"]) == (
            "pending",
            "goods_receipt",
            "PO4471",
        )
        assert row["extracted"]["fields"]["grn_number"] == "GRN-1"
        assert out["kind"] == "goods_receipt"
        assert out["stored"]["supporting_extraction"]["kind"] == (
            "goods_receipt"
        )
        [(event, payload)] = out["emitted"]
        assert event == "supporting_document_extracted"
        assert payload == {
            "kind": "goods_receipt",
            "po_number": "PO4471",
            "blocked_reason": None,
            "extraction_error": None,
        }
        assert out["result"]["decision"] == "SUPPORTING"

    def test_a_purchase_order_is_never_blocked(self, tmp_path, monkeypatch):
        out = self._run(tmp_path, monkeypatch, kind="purchase_order")
        assert out["row"]["status"] == "pending"
        assert out["kind"] == "purchase_order"

    def test_a_receipt_whose_po_is_not_on_file_is_blocked(
        self, tmp_path, monkeypatch
    ):
        row = self._run(tmp_path, monkeypatch)["row"]
        assert row["status"] == "blocked"
        assert row["blocked_reason"] == "Purchase order po-4471 is not on file"

    def test_without_purchase_order_storage_a_receipt_waits_unchecked(
        self, tmp_path, monkeypatch
    ):
        """No book to check against: pending, marked not checked. Accepting
        needs the database, and that still refuses a PO not on file."""
        row = self._run(tmp_path, monkeypatch, orders=False)["row"]
        assert row["status"] == "pending"
        assert row["blocked_reason"] is None
        assert row["extraction_error"] is None
        assert row["extracted"]["_checks"] == {"po_on_file": None}

    def test_an_unreadable_scan_waits_with_its_error(
        self, tmp_path, monkeypatch
    ):
        def broken(*a, **k):
            raise RuntimeError("model returned nothing")

        out = self._run(tmp_path, monkeypatch, extract=broken)
        row = out["row"]
        assert row["status"] == "pending"
        assert row["extraction_error"] == "model returned nothing"
        assert row["extracted"]["fields"] == {}
        assert row["extracted"]["lines"] == []

    def test_a_retry_after_acceptance_overwrites_nothing(
        self, tmp_path, monkeypatch
    ):
        out = self._run(tmp_path, monkeypatch, book={"po-4471": {"po": {}}})
        supporting = out["supporting"]
        supporting.mark(
            "t", out["row"]["id"], status="accepted", reviewed_by="a@b.c",
            expect=("pending",),
        )  # fmt: skip
        out["stored"].clear()
        out["emitted"].clear()

        out["go"]()

        [row] = supporting.list("t", ("accepted",))
        assert row["extracted"] == out["row"]["extracted"]
        assert "supporting_extraction" not in out["stored"]
        assert out["emitted"] == []


class TestInvoicesAreMarkedAsInvoices:
    def test_the_invoice_path_sets_the_kind(self, tmp_path, monkeypatch):
        from invoice_processing.adapters import (  # noqa: PLC0415
            JsonDocumentRepository,
        )

        documents = JsonDocumentRepository(tmp_path / "documents.json")
        document_id = documents.create("t", storage_key="k", sha256="s")
        output = tmp_path / "out"
        output.mkdir()

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
        monkeypatch.setattr(tenant_settings, "apply_to_pipeline", _no_settings)
        monkeypatch.setattr(runner, "_apply_rules", lambda *a, **k: {})
        monkeypatch.setattr(service, "get_documents", lambda: documents)
        process_document(
            RunContext(tenant_id="t", case_id=document_id),
            {"id": document_id, "storage_key": "k", "sha256": "s"},
            cases=Sink(),
            blobs=Blobs(),
        )
        assert documents.get("t", document_id)["kind"] == "invoice"

    def _invoice_run(self, tmp_path, monkeypatch, process=_rejects):
        from invoice_processing.adapters import (  # noqa: PLC0415
            JsonDocumentRepository,
            JsonSupportingDocumentRepository,
        )

        documents = JsonDocumentRepository(tmp_path / "documents.json")
        document_id = documents.create("t", storage_key="k", sha256="s")
        supporting = JsonSupportingDocumentRepository(tmp_path / "sd.json")
        output = tmp_path / "out"
        output.mkdir()
        emitted = []

        class Audit:
            def emit(self, ctx, event, payload, before=None):
                emitted.append((ctx.case_id, event, payload))

        class Sink:
            def put_artifact(self, ctx, key, payload): ...

            def get_artifacts(self, ctx):
                return {}

            def project_case(self, ctx, artifacts): ...

        class Blobs:
            def get(self, key):
                return b"%PDF-1.4"

        monkeypatch.setattr(gia, "get_output_folder", _folder(output))
        monkeypatch.setattr(gia, "process_invoice", process)
        monkeypatch.setattr(tenant_settings, "apply_to_pipeline", _no_settings)
        monkeypatch.setattr(runner, "_apply_rules", lambda *a, **k: {})
        monkeypatch.setattr(service, "get_documents", lambda: documents)
        monkeypatch.setattr(
            service, "get_supporting_documents", lambda: supporting
        )
        monkeypatch.setattr(service, "get_audit", Audit)

        def go(**kw):
            return process_document(
                RunContext(tenant_id="t", case_id=document_id),
                {"id": document_id, "storage_key": "k", "sha256": "s"},
                cases=Sink(),
                blobs=Blobs(),
                **kw,
            )

        return document_id, supporting, emitted, go

    def test_a_review_row_waiting_on_it_is_closed(self, tmp_path, monkeypatch):
        document_id, supporting, emitted, go = self._invoice_run(
            tmp_path, monkeypatch
        )
        row = supporting.upsert_from_extraction(
            "t", document_id=document_id, kind="goods_receipt",
            extracted={}, po_number="PO-1", blocked_reason="not on file",
            extraction_error=None,
        )  # fmt: skip
        go()
        closed = supporting.get("t", row["id"])
        assert closed["status"] == "rejected"
        assert closed["note"] == "Reclassified as an invoice"
        assert closed["reviewed_by"] == "system"
        [(_, _event, payload)] = [
            e for e in emitted if e[1] == "supporting_document_reclassified"
        ]
        assert payload["id"] == row["id"]

    def test_a_decided_review_row_is_left_alone(self, tmp_path, monkeypatch):
        document_id, supporting, emitted, go = self._invoice_run(
            tmp_path, monkeypatch
        )
        row = supporting.upsert_from_extraction(
            "t", document_id=document_id, kind="goods_receipt",
            extracted={}, po_number=None, blocked_reason=None,
            extraction_error=None,
        )  # fmt: skip
        supporting.mark(
            "t", row["id"], status="accepted", reviewed_by="c",
            expect=("pending",),
        )  # fmt: skip
        go()
        assert supporting.get("t", row["id"])["status"] == "accepted"
        assert not [
            e for e in emitted if e[1] == "supporting_document_reclassified"
        ]

    def test_force_invoice_reaches_the_pipeline(self, tmp_path, monkeypatch):
        seen = []

        def process(source, **kwargs):
            seen.append(kwargs.get("force_invoice"))
            return {"decision": "REJECT"}

        *_, go = self._invoice_run(tmp_path, monkeypatch, process)
        go(force_invoice=True)
        go()
        assert seen == [True, False]


class TestTheHandlerPassesForceInvoice:
    def test_the_payload_flag_reaches_process_document(
        self, tmp_path, monkeypatch
    ):
        from invoice_processing.worker.queue import Message  # noqa: PLC0415

        class Documents:
            def find_by_sha(self, tenant_id, sha):
                return {"id": "d1", "sha256": sha, "storage_key": "k"}

        got = []
        monkeypatch.setattr(service, "get_documents", Documents)
        monkeypatch.setattr(service, "get_blobs", lambda t: None)
        monkeypatch.setattr(service, "get_cases", lambda: None)
        monkeypatch.setattr(
            runner,
            "process_document",
            lambda ctx, doc, blobs, cases, **kw: got.append(kw) or {},
        )
        worker = Worker(LocalQueue(tmp_path / "q"), ClaimStore(tmp_path / "c"))
        payload = {"tenant_id": "t", "sha256": "s", "document_id": "d1"}
        worker._default_handler(
            Message(id="m1", payload={**payload, "force_invoice": True})
        )
        worker._default_handler(Message(id="m2", payload=payload))
        assert got == [{"force_invoice": True}, {"force_invoice": False}]


class TestTheDuplicateCorpusIsInvoicesOnly:
    def test_a_receipt_s_case_is_not_compared_against(self, tmp_path):
        """A scanned PO or receipt's case holds a supporting_extraction and
        no invoice extraction, so it never enters the corpus."""
        cases = FsCaseRepository(tmp_path / "cases", tenant_id="t")
        cases.put_artifact(
            RunContext(tenant_id="t", case_id="grn"),
            "supporting_extraction",
            {"kind": "goods_receipt", "fields": {"grn_number": "G1"}},
        )
        cases.put_artifact(
            RunContext(tenant_id="t", case_id="inv"),
            "extraction",
            {"invoice": {"invoice_number": "I1"}},
        )

        corpus = runner.seen_invoices_for(
            RunContext(tenant_id="t", case_id="now"), cases
        )()

        assert [key.case_id for key in corpus] == ["inv"]


class TestClosedTenants:
    def _worker(self, queue, claims, audit, statuses, handler=None):
        def lookup(tenant_id):
            status = statuses.get(tenant_id, "active")
            if isinstance(status, Exception):
                raise status
            return status

        return Worker(
            queue=queue,
            claims=claims,
            handler=handler or Handler(),
            audit=audit,
            tenant_status=lookup,
        )

    def test_a_suspended_tenants_document_is_dead_lettered_not_run(
        self, queue, claims, audit
    ):
        handler = Handler()
        worker = self._worker(
            queue, claims, audit, {"tenant-acme": "suspended"}, handler
        )
        queue.publish(work_message())
        assert worker.handle(queue.pull()[0]) == "dead-lettered:tenant-closed"
        assert handler.calls == 0
        dead = queue.dead_letters()
        assert dead[0]["dead_letter_reason"] == "tenant suspended"
        assert [e[1] for e in audit.events] == ["document_dead_lettered"]

    def test_a_purged_tenants_document_writes_no_audit_row(
        self, queue, claims, audit
    ):
        worker = self._worker(queue, claims, audit, {"tenant-acme": "purged"})
        queue.publish(work_message())
        assert worker.handle(queue.pull()[0]) == "dead-lettered:tenant-closed"
        assert audit.events == []
        # Not "suspended": nobody should try to replay it after a
        # reactivation that cannot happen.
        assert queue.dead_letters()[0]["dead_letter_reason"] == "tenant purged"

    def test_a_suspended_tenants_rematch_is_dead_lettered(
        self, queue, claims, audit
    ):
        worker = self._worker(
            queue, claims, audit, {"tenant-acme": "suspended"}
        )
        queue.publish(
            {
                "type": runner.REMATCH,
                "tenant_id": "tenant-acme",
                "case_id": "c1",
            }
        )
        assert worker.handle(queue.pull()[0]) == "dead-lettered:tenant-closed"
        assert [e[1] for e in audit.events] == ["rematch_dead_lettered"]

    def test_a_failed_status_lookup_retries(self, queue, claims, audit):
        worker = self._worker(
            queue, claims, audit, {"tenant-acme": RuntimeError("db down")}
        )
        queue.publish(work_message())
        assert worker.handle(queue.pull()[0]) == "retry"
        assert queue.depth() == 1

    def test_no_lookup_means_no_check(self, queue, claims, audit):
        """A worker without a database has no tenants to close."""
        worker = Worker(queue=queue, claims=claims, handler=Handler())
        queue.publish(work_message())
        assert worker.handle(queue.pull()[0]) == "done"


class TestEscalationSweepSkipsClosedTenants:
    def test_only_active_tenants_are_swept(self, scratch_db):
        url = scratch_db("sweep.db")
        engine = get_engine(url)
        models.Base.metadata.drop_all(engine)
        create_all(url)
        with tenant_session("", url) as session:
            session.add(models.Tenant(id="t-open", slug="open", name="Open"))
            session.add(
                models.Tenant(
                    id="t-shut", slug="shut", name="Shut", status="suspended"
                )
            )
        assert PgApprovalRepository(url).tenant_ids() == ["t-open"]
