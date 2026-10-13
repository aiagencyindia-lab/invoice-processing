"""A job's lines and audit rows name the request that caused it."""

import logging

from invoice_processing import observability as obs
from invoice_processing.api import auth
from invoice_processing.worker import runner
from invoice_processing.worker.queue import LocalQueue, Message


class Claims:
    def __init__(self):
        self.done = set()

    def state(self, tenant_id, document_id):
        return "done" if (tenant_id, document_id) in self.done else None

    def claim(self, tenant_id, document_id):
        return True

    def complete(self, tenant_id, document_id, state):
        if state == "done":
            self.done.add((tenant_id, document_id))

    def release(self, tenant_id, document_id):
        pass


class Audit:
    def __init__(self):
        self.events = []

    def emit(self, ctx, event, payload):
        self.events.append((ctx, event, payload))


def _worker(tmp_path, handler, **kw):
    queue = LocalQueue(tmp_path / "q")
    return runner.Worker(
        queue=queue, claims=Claims(), handler=handler, audit=Audit(), **kw
    ), queue


def _message(**payload):
    return Message(
        id="m-1",
        payload={
            "type": runner.DOCUMENT_INGESTED,
            "tenant_id": "acme",
            "document_id": "doc-1",
            **payload,
        },
    )


def test_enqueue_carries_the_request_id(tmp_path):
    queue = LocalQueue(tmp_path / "q")

    class Result:
        document_id, sha256, storage_key = "doc-1", "sha", "key"

    ctx = runner.RunContext(tenant_id="acme", run_id="req-42", actor="api")
    runner.enqueue_document(queue, ctx, Result())
    runner.enqueue_rematch(queue, "acme", "case-1", "PO-1", request_id="req-42")
    payloads = [m.payload for m in queue.pull(max_messages=2)]
    assert [p["request_id"] for p in payloads] == ["req-42", "req-42"]


def test_the_job_line_and_the_audit_row_carry_the_request_id(tmp_path, caplog):
    seen = {}

    def handler(message):
        seen["ids"] = (
            auth.current_request_id.get(""),
            obs.current_tenant_id.get(""),
            obs.current_document_id.get(""),
        )
        return {"decision": "ACCEPT"}

    worker, queue = _worker(tmp_path, handler)
    queue.publish(_message(request_id="req-42").payload, tenant_id="acme")
    [message] = queue.pull()
    with caplog.at_level("INFO", logger="invoice_processing.worker.runner"):
        outcome = worker.handle(message)
    assert outcome == "done"
    assert seen["ids"] == ("req-42", "acme", "doc-1")
    [job] = [r for r in caplog.records if r.getMessage() == "job"]
    assert job.outcome == "done" and job.duration_ms >= 0 and job.attempt == 1
    assert job.request_id == "req-42" and job.document_id == "doc-1"
    [(ctx, event, _)] = worker.audit.events
    assert event == "document_processed" and ctx.run_id == "req-42"
    assert auth.current_request_id.get("") == ""  # reset after the job


def test_an_old_message_without_request_id_still_runs(tmp_path, caplog):
    worker, queue = _worker(tmp_path, lambda m: {})
    queue.publish(_message().payload, tenant_id="acme")
    [message] = queue.pull()
    with caplog.at_level("INFO", logger="invoice_processing.worker.runner"):
        assert worker.handle(message) == "done"
    [job] = [r for r in caplog.records if r.getMessage() == "job"]
    assert job.request_id == message.id


def test_a_dead_letter_keeps_the_traceback_out_of_the_audit_row(
    tmp_path, caplog, monkeypatch
):
    captured = []
    monkeypatch.setattr(
        runner.sentry_sdk,
        "capture_exception",
        lambda e=None: captured.append(e),
    )

    def handler(message):
        raise RuntimeError("extractor exploded")

    worker, queue = _worker(tmp_path, handler, max_attempts=1)
    queue.publish(_message(request_id="req-42").payload, tenant_id="acme")
    [message] = queue.pull()
    with caplog.at_level("INFO", logger="invoice_processing.worker.runner"):
        assert worker.handle(message) == "dead-lettered:exhausted"
    [(ctx, event, payload)] = worker.audit.events
    assert event == "document_dead_lettered" and ctx.run_id == "req-42"
    assert payload["reason"].startswith("RuntimeError: extractor exploded")
    assert "Traceback" not in payload["reason"]
    failed = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert failed and "extractor exploded" in failed[0].exc_text
    assert len(captured) == 1 and isinstance(captured[0], RuntimeError)


def test_a_step_line_names_the_step_that_took_the_time(caplog):
    """on_step fires when a step starts, so the time since the last call
    belongs to the previous step; the line says which."""
    published = []

    class Cases:
        def put_artifact(self, ctx, kind, record):
            published.append(record)

    ctx = runner.RunContext(tenant_id="acme", case_id="c-1")
    publish = runner._step_publisher(ctx, Cases())
    with caplog.at_level("INFO", logger="invoice_processing.worker.runner"):
        publish({"step": 1, "key": "classification", "label": "Classifier"})
        publish({"step": 2, "key": "extraction", "label": "Extractor"})
    first, second = [
        r for r in caplog.records if r.getMessage() == "pipeline step"
    ]
    assert first.step == "classification" and first.previous_step == ""
    assert second.step == "extraction"
    assert second.previous_step == "classification"
    assert second.previous_step_ms >= 0
    assert len(published) == 2
