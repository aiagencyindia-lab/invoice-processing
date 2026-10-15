"""Graph sync and trust evaluation ride the existing queue, and never run twice by mistake."""

import pytest

from invoice_processing.api import service
from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.worker.queue import LocalQueue
from invoice_processing.worker.runner import (
    TRUST_EVALUATE,
    TRUST_GRAPH_SYNC,
    ClaimStore,
    Worker,
    enqueue_trust_evaluate,
    enqueue_trust_graph_sync,
)

T = "tenant-acme"


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("trust_worker.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
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
