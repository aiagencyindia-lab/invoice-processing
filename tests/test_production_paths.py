"""The paths that run in production and were never exercised.

Each subsystem here had a tested local twin and an untested real one, which is
the most misleading shape a test suite can take: `LocalQueue` is what the tests
covered, `PubSubQueue` is what runs; the Xero fake's contract was asserted, the
real transport's conformance to it was not; the worker's retry and idempotency
were covered with an injected stub handler, so `process_document` -- the
function that actually moves an invoice through the pipeline -- ran in no test
at all.

Nothing here reaches a network. Pub/Sub takes injected clients, the Xero
transport takes a stubbed `requests.request`, and the extraction call is
monkeypatched; what is under test is the adapter code around them.
"""

import json
import pathlib
from types import SimpleNamespace

import pytest

from invoice_processing import alf_stage
from invoice_processing.api import service
from invoice_processing.connectors.xero import HttpTransport, Transport
from invoice_processing.ports import RunContext
from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    _money,
)
from invoice_processing.tenant_settings import JsonTenantSettings
from invoice_processing.worker import queue as queue_module
from invoice_processing.worker import runner
from invoice_processing.worker.queue import (
    LocalQueue,
    Message,
    PubSubQueue,
    Queue,
    get_queue,
)

# ---------------------------------------------------------------------------
# Pub/Sub
# ---------------------------------------------------------------------------


class FakeFuture:
    def __init__(self, value):
        self._value = value
        self.timeout = None

    def result(self, timeout=None):
        self.timeout = timeout
        return self._value


class FakePublisher:
    def __init__(self):
        self.published = []

    def publish(self, topic, data, **attributes):
        self.published.append((topic, data, attributes))
        self.future = FakeFuture("msg-1")
        return self.future


class FakeSubscriber:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.acked = []
        self.deadlines = []

    def pull(self, request=None, timeout=None):
        return SimpleNamespace(received_messages=self.messages)

    def acknowledge(self, request=None):
        self.acked.extend(request["ack_ids"])

    def modify_ack_deadline(self, request=None):
        self.deadlines.append(
            (request["ack_ids"], request["ack_deadline_seconds"])
        )


def received(payload, ack_id="a-1", attempt=1, attributes=None):
    return SimpleNamespace(
        ack_id=ack_id,
        delivery_attempt=attempt,
        message=SimpleNamespace(
            message_id="m-1",
            data=json.dumps(payload).encode(),
            attributes=attributes or {},
        ),
    )


@pytest.fixture
def pubsub(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    publisher, subscriber = FakePublisher(), FakeSubscriber()
    queue = PubSubQueue(
        topic="t", subscription="s", publisher=publisher, subscriber=subscriber
    )
    return queue, publisher, subscriber


class TestPubSubQueue:
    """Zero tests before this file; it is what runs in production."""

    def test_satisfies_the_same_port_as_the_local_queue(self, pubsub):
        queue, _, _ = pubsub
        assert isinstance(queue, Queue)

    def test_publish_encodes_json_and_returns_the_message_id(self, pubsub):
        queue, publisher, _ = pubsub
        assert queue.publish({"tenant_id": "t-1"}, tenant_id="t-1") == "msg-1"

        topic, data, attributes = publisher.published[0]
        assert topic == "projects/proj/topics/t"
        assert json.loads(data.decode()) == {"tenant_id": "t-1"}
        assert attributes == {"tenant_id": "t-1"}

    def test_publish_waits_at_most_five_seconds_for_the_broker(self, pubsub):
        """Every enqueue runs on a request; a stalled broker must not hold it
        for 30 s. A timeout raises, and callers log it and carry on."""
        queue, publisher, _ = pubsub
        queue.publish({"tenant_id": "t-1"})
        assert (
            publisher.future.timeout
            == 5
            == queue_module.PUBLISH_TIMEOUT_SECONDS
        )

    def test_pull_decodes_into_messages(self, pubsub):
        queue, _, subscriber = pubsub
        subscriber.messages = [received({"document_id": "d-1"}, attempt=2)]

        [message] = queue.pull()
        assert isinstance(message, Message)
        assert message.payload["document_id"] == "d-1"
        assert message.delivery_attempt == 2
        assert message.ack_id == "a-1"

    def test_an_undecodable_payload_does_not_raise(self, pubsub):
        """A malformed message must be dead-letterable, not crash the puller."""
        queue, _, subscriber = pubsub
        bad = received({})
        bad.message.data = b"\xff not json"
        subscriber.messages = [bad]

        [message] = queue.pull()
        assert message.payload == {}

    def test_a_missing_delivery_attempt_defaults_to_one(self, pubsub):
        """Pub/Sub omits it unless a dead-letter policy exists; 0 would disable retries."""
        queue, _, subscriber = pubsub
        item = received({"x": 1})
        item.delivery_attempt = None
        subscriber.messages = [item]
        assert queue.pull()[0].delivery_attempt == 1

    def test_ack(self, pubsub):
        queue, _, subscriber = pubsub
        queue.ack(Message(id="m", payload={}, ack_id="a-9"))
        assert subscriber.acked == ["a-9"]

    def test_nack_zeroes_the_deadline_for_immediate_redelivery(self, pubsub):
        queue, _, subscriber = pubsub
        queue.nack(Message(id="m", payload={}, ack_id="a-9"))
        assert subscriber.deadlines == [(["a-9"], 0)]

    def test_dead_letter_acks_so_redelivery_stops(self, pubsub):
        """Pub/Sub's own policy preserves the message; the ack stops the loop."""
        queue, _, subscriber = pubsub
        queue.dead_letter(Message(id="m", payload={}, ack_id="a-9"), "poison")
        assert subscriber.acked == ["a-9"]

    def test_a_missing_project_fails_loudly(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT"):
            PubSubQueue(topic="t")


class TestQueueSelection:
    def test_local_queue_without_pubsub_configuration(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("PUBSUB_TOPIC", raising=False)
        assert isinstance(get_queue(tmp_path / "q"), LocalQueue)

    def test_both_variables_are_required_to_select_pubsub(
        self, tmp_path, monkeypatch
    ):
        """Half-configured must not silently publish nowhere."""
        monkeypatch.setenv("PUBSUB_TOPIC", "t")
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        assert isinstance(get_queue(tmp_path / "q"), LocalQueue)


# ---------------------------------------------------------------------------
# Xero transport
# ---------------------------------------------------------------------------


class TestHttpTransport:
    """The Protocol was defined so tests could fake it; nothing implemented it."""

    def test_satisfies_the_protocol_the_fake_stands_in_for(self):
        assert isinstance(HttpTransport(), Transport)

    def test_returns_status_and_parsed_json(self, monkeypatch):
        captured = {}

        def fake_request(method, url, **kwargs):
            captured.update(method=method, url=url, **kwargs)
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True})

        monkeypatch.setattr("requests.request", fake_request)
        status, body = HttpTransport().request(
            "POST", "https://api.xero.com/x", headers={"A": "b"}, json={"k": 1}
        )
        assert (status, body) == (200, {"ok": True})
        assert captured["method"] == "POST"
        assert captured["headers"] == {"A": "b"}

    def test_a_non_json_body_comes_back_as_text(self, monkeypatch):
        def fake_request(method, url, **kwargs):
            def boom():
                raise ValueError("not json")

            return SimpleNamespace(
                status_code=502, json=boom, text="Bad Gateway"
            )

        monkeypatch.setattr("requests.request", fake_request)
        assert HttpTransport().request("GET", "https://x", headers={}) == (
            502,
            "Bad Gateway",
        )

    def test_error_statuses_are_returned_not_raised(self, monkeypatch):
        """The client interprets 429/5xx for retry; raising would bypass that."""

        def fake_request(method, url, **kwargs):
            return SimpleNamespace(
                status_code=429, json=lambda: {"detail": "rate limited"}
            )

        monkeypatch.setattr("requests.request", fake_request)
        status, _ = HttpTransport().request("GET", "https://x", headers={})
        assert status == 429

    def test_a_timeout_is_always_supplied(self, monkeypatch):
        """A request with no timeout can hang the worker indefinitely."""
        captured = {}

        def fake_request(method, url, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(status_code=200, json=lambda: {})

        monkeypatch.setattr("requests.request", fake_request)
        HttpTransport(timeout=12.0).request("GET", "https://x", headers={})
        assert captured["timeout"] == 12.0


# ---------------------------------------------------------------------------
# The worker's real handler
# ---------------------------------------------------------------------------


class RecordingCases:
    def __init__(self, project_raises=False):
        self.written = {}
        self.projected = None
        self.project_raises = project_raises

    def put_artifact(self, ctx, key, payload):
        self.written[key] = payload

    def get_artifacts(self, ctx):
        return dict(self.written)

    def project_case(self, ctx, artifacts):
        if self.project_raises:
            raise RuntimeError("projection failed")
        self.projected = artifacts


class StubBlobs:
    def __init__(self, data=b"%PDF-1.4 fake"):
        self.data = data
        self.requested = []

    def get(self, key):
        self.requested.append(key)
        return self.data


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    """Stand in for the real extraction, which needs Vertex and minutes."""
    calls = {}
    output = tmp_path / "out" / "case_x"
    output.mkdir(parents=True)
    (output / "02_extraction.json").write_text(
        '{"invoice": {"vendor_name": "A"}}'
    )
    (output / "08_decision.json").write_text('{"final_decision": "ACCEPT"}')
    (output / "broken.json").write_text("{not json")

    def fake_process_invoice(
        source_folder, on_step=None, seen_invoices=None, force_invoice=False
    ):
        calls["source"] = source_folder
        # The worker reports progress through this; a double that ignored it
        # would let the reporting break without a test noticing.
        if on_step is not None:
            on_step(
                {"step": 1, "total": 9, "key": "classification", "label": "x"}
            )
        calls["pdf_exists"] = (source_folder / "invoice.pdf").exists()
        return {"decision": "ACCEPT"}

    monkeypatch.setattr(
        "invoice_processing.shared_libraries.acting.general_invoice_agent.process_invoice",
        fake_process_invoice,
    )
    monkeypatch.setattr(
        "invoice_processing.shared_libraries.acting.general_invoice_agent.get_output_folder",
        lambda case_id: output,
    )
    # The pipeline now loads per-tenant settings before extracting, so the test
    # needs a store. Left unset it resolves to Postgres against a schema-less
    # test database -- and that failure is deliberate: if settings cannot be
    # read, processing with the wrong organisation names would reject invoices
    # for belonging to someone else, which is worse than retrying.
    monkeypatch.setattr(
        service,
        "_tenant_settings",
        JsonTenantSettings(tmp_path / "settings.json"),
    )
    return calls, output


class TestProcessDocument:
    """The function that moves an invoice through the pipeline. Previously untested."""

    def test_materialises_the_blob_and_runs_the_pipeline(self, pipeline):
        calls, _ = pipeline
        blobs, cases = StubBlobs(), RecordingCases()
        ctx = RunContext(tenant_id="t-1", case_id="case_x")

        result = runner.process_document(
            ctx, {"id": "d-1", "storage_key": "documents/abc.pdf"}, blobs, cases
        )

        assert blobs.requested == ["documents/abc.pdf"]
        assert calls["pdf_exists"], "the pipeline was given no PDF to read"
        assert result["decision"] == "ACCEPT"

    def test_the_tenants_rules_are_applied_before_projection(
        self, pipeline, monkeypatch
    ):
        """The connection this whole feature is: a rule reaching a decision.

        `process_document` ran the pipeline, stored what it produced and
        projected it, and never once consulted the rules an expert had taught
        -- so a rule could be written, validated, impact-assessed and approved
        and change nothing at all. Calling `_apply_rules` in a test of its own
        would not have caught that; only asking `process_document` does.
        """
        seen = {}

        class Rules:
            def load(self, tenant_id):
                seen["tenant"] = tenant_id
                return {"rules": [{"id": "ALF-1"}]}, 3

        monkeypatch.setattr(service, "get_rules", Rules)
        monkeypatch.setattr(
            alf_stage,
            "run",
            lambda artifacts, doc, **kw: {
                "alf": {"rules_applied": [], "mode": "shadow", "seen": doc}
            },
        )

        blobs, cases = StubBlobs(), RecordingCases()
        runner.process_document(
            RunContext(tenant_id="t-1", case_id="case_x"),
            {"id": "d-1", "storage_key": "documents/abc.pdf"},
            blobs,
            cases,
        )

        assert seen["tenant"] == "t-1", "rules were not loaded for this tenant"
        assert "alf" in cases.written, "the rule stage never ran"
        # And the projection saw it, so a corrected decision reaches the
        # column the sheet and the queue read.
        assert "alf" in (cases.projected or {})

    def test_artifacts_are_written_through_the_port(self, pipeline):
        """Not straight to disk: the port is where the tenant boundary lives.

        The keys are the logical ones every reader uses. This asserted the
        file stems -- "02_extraction" -- and so pinned the defect that made a
        deployed invoice unreadable: the filesystem repository maps a stem
        back on the way out, the Postgres one does not, and the assertion
        matched whichever the test happened to use.
        """
        cases, blobs = RecordingCases(), StubBlobs()
        runner.process_document(
            RunContext(tenant_id="t-1", case_id="case_x"),
            {"id": "d-1", "storage_key": "k"},
            blobs,
            cases,
        )
        assert "extraction" in cases.written
        assert "decision" in cases.written

    def test_an_unparseable_artifact_is_skipped_not_fatal(self, pipeline):
        """One bad file must not discard the whole case's results."""
        cases = RecordingCases()
        result = runner.process_document(
            RunContext(tenant_id="t-1", case_id="case_x"),
            {"id": "d-1", "storage_key": "k"},
            StubBlobs(),
            cases,
        )
        assert "broken" not in cases.written
        assert result["artifacts"] == 2

    def test_the_workspace_is_cleaned_up(self, pipeline):
        """Otherwise a long-running worker fills its disk with temp cases."""
        calls = pipeline[0]
        runner.process_document(
            RunContext(tenant_id="t-1", case_id="case_x"),
            {"id": "d-1", "storage_key": "k"},
            StubBlobs(),
            RecordingCases(),
        )
        assert not calls["source"].exists()

    def test_the_case_id_falls_back_to_the_document_id(self, pipeline):
        result = runner.process_document(
            RunContext(tenant_id="t-1"),
            {"id": "doc-42", "storage_key": "k"},
            StubBlobs(),
            RecordingCases(),
        )
        assert result["case_id"] == "doc-42"


class TestMissingAmountsDoNotCrash:
    """The first real-world invoice crashed the output generator.

    `invoice.get("tax_amount", 0)` returns the default only when the key is
    ABSENT. The extraction schema emits every field, using None where the model
    could not read one -- so the default never applied and `f"{None:,.2f}"`
    raised TypeError. Three sample invoices all had tax lines, so nothing
    caught it until a real supplier invoice with no tax arrived.
    """

    @pytest.mark.parametrize("value", [None, "", "n/a", [], {}])
    def test_an_unreadable_amount_renders_empty_not_zero(self, value):
        """ "0.00" would assert the tax is zero on a document we could not read.

        This JSON feeds the ERP export, so a fabricated zero is worse than a
        blank: one is a wrong number someone pays against, the other is a
        visible gap.
        """

        assert _money(value) == ""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "0.00"),
            (0.0, "0.00"),
            (71664.0, "71,664.00"),
            (1234.5, "1,234.50"),
        ],
    )
    def test_real_amounts_still_format(self, value, expected):
        """A genuine zero must still render as 0.00 -- it is a read value."""

        assert _money(value) == expected

    def test_booleans_are_not_treated_as_numbers(self):
        """bool is a subclass of int; True would otherwise render as 1.00."""

        assert _money(True) == ""


class TestTheCaseIsProjected:
    """The artifacts answer "what happened"; the projection answers "which
    invoices are waiting", which no reader can ask of a JSON blob."""

    def test_the_finished_case_is_projected_once(self, pipeline, tmp_path):
        cases = RecordingCases()
        runner.process_document(
            RunContext(tenant_id="t-1", case_id="case_x"),
            {"id": "d-1", "storage_key": "k"},
            StubBlobs(),
            cases,
        )
        assert cases.projected is not None, "the case was never projected"
        # Under the keys readers use, not the filenames.
        assert "extraction" in cases.projected
        assert "decision" in cases.projected
        # The step counter is written through the same store but is not an
        # artifact, and a projection that treated it as one would file a
        # progress record as pipeline output.
        assert "progress" in cases.written
        assert "progress" not in cases.projected

    def test_the_pipeline_is_given_this_tenants_corpus(self, monkeypatch):
        """The wiring, not the pieces: the worker must hand the pipeline a
        reader bound to this tenant. Left to its default the pipeline scans a
        local directory shared by every tenant the worker serves."""
        seen = {}

        def fake_process_invoice(
            source_folder, on_step=None, seen_invoices=None, force_invoice=False
        ):
            seen["provider"] = seen_invoices
            return {"decision": "ACCEPT"}

        monkeypatch.setattr(
            "invoice_processing.shared_libraries.acting.general_invoice_agent.process_invoice",
            fake_process_invoice,
        )
        monkeypatch.setattr(
            "invoice_processing.shared_libraries.acting.general_invoice_agent.get_output_folder",
            lambda case_id: pathlib.Path("/nonexistent"),
        )
        monkeypatch.setattr(
            service,
            "_tenant_settings",
            JsonTenantSettings(pathlib.Path("/tmp/s.json")),
        )

        cases = RecordingCases()
        cases.iter_case_ids = lambda tenant_id: iter(["other-case"])
        cases.get_many_artifacts = lambda tenant_id, ids: {
            "other-case": {
                "extraction": {"invoice": {"invoice_number": "INV-1"}}
            }
        }

        runner.process_document(
            RunContext(tenant_id="t-1", case_id="case_x"),
            {"id": "d-1", "storage_key": "k"},
            StubBlobs(),
            cases,
        )

        assert seen["provider"] is not None, (
            "the pipeline was left to scan local disk"
        )
        corpus = seen["provider"]()
        assert [key.case_id for key in corpus] == ["other-case"]

    def test_a_projection_failure_does_not_lose_the_run(self, pipeline):
        """The artifacts are the record. A view over them that could fail the
        run would be worse than no view."""
        result = runner.process_document(
            RunContext(tenant_id="t-1", case_id="case_x"),
            {"id": "d-1", "storage_key": "k"},
            StubBlobs(),
            RecordingCases(project_raises=True),
        )
        assert result["decision"] == "ACCEPT"


class TestPipelineFailuresReachTheQueue:
    """A crashed pipeline reported success, so the invoice was dropped.

    `process_invoice` catches its own exceptions and returns
    {"decision": "ERROR"} instead of raising, so a crash arrived at the worker
    looking exactly like a completed run: the message was acked, the claim
    marked done, and the document never retried -- with the queue reporting a
    clean drain.
    """

    def test_an_error_decision_raises_so_the_message_is_retried(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            service, "_tenant_settings", JsonTenantSettings(tmp_path / "s.json")
        )
        monkeypatch.setattr(
            "invoice_processing.shared_libraries.acting.general_invoice_agent.process_invoice",
            lambda source_folder, on_step=None, seen_invoices=None, force_invoice=False: {
                "decision": "ERROR",
                "error": "boom",
            },
        )
        monkeypatch.setattr(
            "invoice_processing.shared_libraries.acting.general_invoice_agent.get_output_folder",
            lambda case_id: pathlib.Path("/nonexistent"),
        )
        with pytest.raises(runner.ProcessingFailed, match="boom"):
            runner.process_document(
                RunContext(tenant_id="t-1", case_id="case_x"),
                {"id": "d-1", "storage_key": "k"},
                StubBlobs(),
                RecordingCases(),
            )

    def test_a_successful_decision_does_not_raise(self, pipeline):
        result = runner.process_document(
            RunContext(tenant_id="t-1", case_id="case_x"),
            {"id": "d-1", "storage_key": "k"},
            StubBlobs(),
            RecordingCases(),
        )
        assert result["decision"] == "ACCEPT"
