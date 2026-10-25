"""Graph sync and trust evaluation ride the existing queue, and never run twice by mistake."""

import sys
import types

import pytest
from fresh_schema import fresh_schema

from invoice_processing.api import service
from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.worker.queue import LocalQueue
from invoice_processing.worker.runner import (
    TRUST_AGENT_INJECTION,
    TRUST_AUDIT_RUN,
    TRUST_EVALUATE,
    TRUST_GRAPH_SYNC,
    ClaimStore,
    Worker,
    enqueue_agent_injection,
    enqueue_trust_audit_run,
    enqueue_trust_evaluate,
    enqueue_trust_graph_sync,
)

T = "tenant-acme"


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("trust_worker.db")
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
        s.add(
            models.Vendor(
                tenant_id=T,
                id="ven-1",
                name="Acme",
                normalized_name="acme",
                tax_id="27AAPFU0939F1ZV",
            )
        )
    monkeypatch.setenv("DATABASE_URL", url)
    graph = GraphStore(url)
    monkeypatch.setattr(service, "_graph_store", graph)
    queue = LocalQueue(tmp_path / "q")
    worker = Worker(
        queue=queue,
        claims=ClaimStore(tmp_path / "claims"),
        handler=lambda m: {"decision": "ACCEPT"},
    )
    return queue, worker, graph


def test_sync_job_builds_the_graph_and_is_idempotent_on_redelivery(env):
    queue, worker, graph = env
    enqueue_trust_graph_sync(queue, T, "req-1")
    assert worker.run_once() == ["trust-synced"]
    n = len(graph.nodes(T))
    assert n >= 1
    enqueue_trust_graph_sync(queue, T, "req-2")
    assert worker.run_once() == ["trust-synced"]
    assert len(graph.nodes(T)) == n


def test_malformed_trust_message_is_dead_lettered(env):
    queue, worker, _ = env
    queue.publish({"type": TRUST_GRAPH_SYNC})
    assert worker.run_once() == ["dead-lettered:malformed"]
    assert len(queue.dead_letters()) == 1


def test_processed_document_enqueues_an_evaluation(env):
    queue, worker, _ = env
    queue.publish(
        {
            "type": "document.ingested",
            "tenant_id": T,
            "document_id": "doc-9",
            "sha256": "x",
            "storage_key": "k",
        }
    )
    assert worker.run_once() == ["done"]
    [msg] = queue.pull()
    assert msg.payload["type"] == TRUST_EVALUATE
    assert msg.payload["document_id"] == "doc-9"


def test_evaluate_skips_a_document_with_no_invoice_and_runs_no_engine(env):
    queue, worker, _ = env
    enqueue_trust_evaluate(queue, T, "doc-without-invoice", "req")
    assert worker.run_once() == ["trust-evaluated"]
    with tenant_session(T) as s:  # url=None reads DATABASE_URL (the fixture)
        assert s.query(models.TrustFinding).count() == 0


def test_sync_without_a_database_is_dead_lettered(env, monkeypatch):
    queue, worker, _ = env
    monkeypatch.setattr(service, "_graph_store", None)
    monkeypatch.setattr(service, "_database_configured", lambda: False)
    enqueue_trust_graph_sync(queue, T, "req")
    assert worker.run_once() == ["dead-lettered:no-database"]


def _audit_package(monkeypatch, calls):
    """The audit branch's batch module, stubbed: the scaffold ships without it."""
    mod = types.ModuleType("invoice_processing.trust.audit.batch")

    def run_batch(ctx, run_id, *, graph, links):
        calls.append((ctx.tenant_id, run_id, ctx.actor))
        return {"status": "done"}

    mod.run_batch = run_batch
    monkeypatch.setitem(
        sys.modules, "invoice_processing.trust.audit.batch", mod
    )


def test_audit_run_job_calls_the_batch_runner_for_its_run(env, monkeypatch):
    queue, worker, _ = env
    calls = []
    _audit_package(monkeypatch, calls)
    enqueue_trust_audit_run(queue, T, "run-1", "req-1")
    assert worker.run_once() == ["trust-audited"]
    assert calls == [(T, "run-1", "worker")]
    assert worker.run_once() == []


def test_audit_run_without_the_audit_package_is_dead_lettered(env, monkeypatch):
    queue, worker, _ = env
    monkeypatch.setitem(
        sys.modules, "invoice_processing.trust.audit.batch", None
    )
    enqueue_trust_audit_run(queue, T, "run-2")
    assert worker.run_once() == ["dead-lettered:no-database"]
    [dead] = queue.dead_letters()
    assert "audit package" in str(dead)


def test_audit_run_message_without_a_run_id_is_malformed(env):
    queue, worker, _ = env
    queue.publish({"type": TRUST_AUDIT_RUN, "tenant_id": T})
    assert worker.run_once() == ["dead-lettered:malformed"]


def test_a_failing_batch_run_is_retried_then_dead_lettered(env, monkeypatch):
    queue, worker, _ = env
    mod = types.ModuleType("invoice_processing.trust.audit.batch")

    def run_batch(ctx, run_id, *, graph, links):
        raise RuntimeError("db gone")

    mod.run_batch = run_batch
    monkeypatch.setitem(
        sys.modules, "invoice_processing.trust.audit.batch", mod
    )
    enqueue_trust_audit_run(queue, T, "run-3")
    outcomes = [worker.run_once() for _ in range(worker.max_attempts)]
    assert outcomes[0] == ["retry"] and outcomes[-1] == [
        "dead-lettered:exhausted"
    ]


def test_the_webhook_sweep_runs_once_per_interval(tmp_path, monkeypatch):
    from invoice_processing.trust.webhooks import deliver  # noqa: PLC0415
    from invoice_processing.worker import runner  # noqa: PLC0415

    calls = []
    monkeypatch.setattr(
        deliver, "due", lambda now, **kw: calls.append(now) or 0
    )
    now = {"t": 1000.0}
    worker = Worker(
        queue=LocalQueue(tmp_path / "q"),
        claims=ClaimStore(tmp_path / "claims"),
        handler=lambda m: {},
        clock=lambda: now["t"],
    )
    worker._deliver_webhooks_if_due()
    worker._deliver_webhooks_if_due()
    assert len(calls) == 1
    now["t"] += runner.WEBHOOK_SCAN_SECONDS + 1
    worker._deliver_webhooks_if_due()
    assert len(calls) == 2


def test_a_failing_webhook_sweep_does_not_stop_the_worker(
    tmp_path, monkeypatch
):
    from invoice_processing.trust.webhooks import deliver  # noqa: PLC0415

    def boom(now, **kw):
        raise RuntimeError("smtp? no, http")

    monkeypatch.setattr(deliver, "due", boom)
    worker = Worker(
        queue=LocalQueue(tmp_path / "q"),
        claims=ClaimStore(tmp_path / "claims"),
        handler=lambda m: {},
    )
    worker._deliver_webhooks_if_due()  # logged, not raised


def test_the_agent_injection_job_classifies_and_acks(env, monkeypatch):
    from invoice_processing.trust.agents import later  # noqa: PLC0415

    queue, worker, _ = env
    calls = []
    monkeypatch.setattr(service, "get_agent_store", lambda: "store")
    monkeypatch.setattr(service, "get_finding_store", lambda: "findings")
    monkeypatch.setattr(
        later,
        "classify",
        lambda t, a, *, store, findings: calls.append((t, a, store, findings)),
    )
    enqueue_agent_injection(queue, T, "act-1", "req-1")
    assert worker.run_once() == ["agent-injection-classified"]
    assert calls == [(T, "act-1", "store", "findings")]
    assert worker.run_once() == []


def test_a_malformed_agent_injection_job_is_dead_lettered(env):
    queue, worker, _ = env
    queue.publish({"type": TRUST_AGENT_INJECTION, "tenant_id": T})
    assert worker.run_once() == ["dead-lettered:malformed"]
    assert len(queue.dead_letters()) == 1
