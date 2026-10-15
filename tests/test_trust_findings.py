"""Findings v2: cited, versioned, deduplicated, with a money ledger of every step."""

import os
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext, StatusChanged
from invoice_processing.trust.findings.model import (
    Citation,
    FindingSpec,
    IllegalTransition,
    can_transition,
)
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.store import GraphStore
from tests.pg_roles import bootstrapped_database

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="engine")
HUMAN = RunContext(
    tenant_id=T, case_id="", run_id="r2", actor="controller@x.test"
)


@pytest.fixture
def env(scratch_db):
    url = scratch_db("findings.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    graph = GraphStore(url)
    node = graph.upsert_node(CTX, kind="invoice", natural_key="i1")
    return url, FindingStore(url), node


def spec(node, impact="100.00", **over):
    base = {
        "type": "BENCH-IMPORT",
        "transaction_node": node,
        "dedupe_key": "BENCH-IMPORT:i1",
        "impact": Decimal(impact),
        "currency": "INR",
        "confidence": 0.9,
        "engine_version": "0.1",
        "rule_version": "r1",
        "model_version": "fake",
        "summary": "imported",
        "details": {},
        "citations": (Citation(node, "row 2"),),
    }
    base.update(over)
    return FindingSpec(**base)


def test_upsert_without_citation_is_refused(env):
    url, store, node = env
    with pytest.raises(ValueError, match="citation"):
        store.upsert(CTX, spec(node, citations=()))
    with tenant_session(T, url) as s:
        assert s.query(models.TrustFinding).count() == 0


def test_upsert_creates_then_refreshes_by_dedupe_key(env):
    _, store, node = env
    fid, how = store.upsert(CTX, spec(node))
    assert how == "created"
    fid2, how2 = store.upsert(CTX, spec(node, impact="120.00"))
    assert (fid2, how2) == (fid, "refreshed")
    got = store.get(T, fid)
    assert got["impact"] == "120.0000" and got["status"] == "detected"
    assert [e["to_status"] for e in got["events"]] == ["detected"]


def test_rerun_refreshes_but_never_reopens_a_decided_finding(env):
    _, store, node = env
    fid, _ = store.upsert(CTX, spec(node))
    store.transition(HUMAN, fid, "triaged", actor="controller@x.test")
    store.transition(HUMAN, fid, "accepted", actor="controller@x.test")
    assert store.upsert(CTX, spec(node, impact="999.00")) == (fid, "untouched")
    assert store.get(T, fid)["impact"] == "100.0000"


def test_illegal_transition_is_refused_and_rejection_needs_reason(env):
    _, store, node = env
    fid, _ = store.upsert(CTX, spec(node))
    with pytest.raises(IllegalTransition):
        store.transition(HUMAN, fid, "claimed", actor="x")
    store.transition(HUMAN, fid, "triaged", actor="x")
    with pytest.raises(ValueError, match="reason"):
        store.transition(HUMAN, fid, "rejected", actor="x")
    assert not can_transition("recovered", "detected")


def test_transition_writes_event_and_audit_rows_atomically(env):
    url, store, node = env
    fid, _ = store.upsert(CTX, spec(node))
    out = store.transition(
        HUMAN,
        fid,
        "triaged",
        actor="controller@x.test",
        amount=Decimal("100.00"),
    )
    assert out["status"] == "triaged" and out["previous"] == "detected"
    with tenant_session(T, url) as s:
        events = (
            s.query(models.TrustFindingEvent)
            .order_by(models.TrustFindingEvent.created_at)
            .all()
        )
        assert [(e.from_status, e.to_status) for e in events] == [
            (None, "detected"),
            ("detected", "triaged"),
        ]
        audit = (
            s.query(models.AuditEvent)
            .filter_by(action="trust_finding_triaged")
            .one()
        )
        assert audit.entity_type == "trust" and audit.entity_id == fid


def test_concurrent_change_is_reported_not_overwritten(env):
    _, store, node = env
    fid, _ = store.upsert(CTX, spec(node))
    store.transition(HUMAN, fid, "triaged", actor="a")
    with pytest.raises(StatusChanged):
        store.transition(HUMAN, fid, "triaged", actor="b", expect="detected")


def test_list_filters_by_status_and_type(env):
    _, store, node = env
    store.upsert(CTX, spec(node))
    listed = store.list(T, status="detected", type="BENCH-IMPORT")
    assert [f["type"] for f in listed] == ["BENCH-IMPORT"]
    assert store.list("tenant-globex") == []


pg_only = pytest.mark.skipif(
    not (os.getenv("TEST_DATABASE_URL") or "").startswith("postgresql"),
    reason="the citation trigger exists only on Postgres",
)


@pg_only
def test_db_refuses_a_finding_without_a_citation():
    """The Python writer refuses; so does the database, for any other writer."""
    # The refusal lands at commit, which tenant_session performs on exit.
    with (
        pytest.raises(DatabaseError, match="no citation"),
        bootstrapped_database() as db,
        tenant_session(T, db.owner_url) as s,
    ):
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
        s.flush()
        s.execute(
            text(
                "insert into graph_nodes (id, tenant_id, kind, natural_key, "
                "valid_from, created_at, updated_at) values "
                "('n1', :t, 'invoice', 'i1', now(), now(), now())"
            ),
            {"t": T},
        )
        s.execute(
            text(
                "insert into trust_findings (id, tenant_id, type, "
                "transaction_node, status, dedupe_key, created_at, updated_at) "
                "values ('f1', :t, 'X', 'n1', 'detected', 'k', now(), now())"
            ),
            {"t": T},
        )
