"""Findings v2: cited, versioned, deduplicated, with a money ledger of every step."""

import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext, StatusChanged
from invoice_processing.trust.findings import model as finding_model
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


def test_register_type_records_engine_and_hold(monkeypatch):
    monkeypatch.setattr(finding_model, "TYPES", dict(finding_model.TYPES))
    monkeypatch.setattr(
        finding_model, "TYPE_ENGINE", dict(finding_model.TYPE_ENGINE)
    )
    monkeypatch.setattr(
        finding_model, "HOLD_TYPES", set(finding_model.HOLD_TYPES)
    )
    finding_model.register_type("X-HOLD", "a hold", engine="payment", hold=True)
    finding_model.register_type("X-NOTE", "a note", engine="leakage")
    assert finding_model.TYPE_ENGINE["X-HOLD"] == "payment"
    assert finding_model.TYPE_ENGINE["X-NOTE"] == "leakage"
    assert "X-HOLD" in finding_model.HOLD_TYPES
    assert "X-NOTE" not in finding_model.HOLD_TYPES


def test_hold_types_lists_only_holds():
    assert "BENCH-IMPORT" not in finding_model.HOLD_TYPES
    assert finding_model.TYPE_ENGINE["BENCH-IMPORT"] == "bench"


def test_list_filters_by_transaction_node(env):
    url, store, node = env
    graph = GraphStore(url)
    other = graph.upsert_node(CTX, kind="invoice", natural_key="i2")
    store.upsert(CTX, spec(node))
    store.upsert(
        CTX,
        spec(other, dedupe_key="BENCH-IMPORT:i2", citations=(Citation(other),)),
    )
    assert [
        f["transaction_node"] for f in store.list(T, transaction_node=other)
    ] == [other]


@pytest.fixture
def book(env):
    """Two vendors, three invoices, findings of two types across them."""
    url, store, _ = env
    graph = GraphStore(url)
    ven_a = graph.upsert_node(CTX, kind="vendor", natural_key="vendors:a")
    ven_b = graph.upsert_node(CTX, kind="vendor", natural_key="vendors:b")
    invs = []
    for i, ven in enumerate((ven_a, ven_a, ven_b)):
        inv = graph.upsert_node(
            CTX, kind="invoice", natural_key=f"invoices:x{i}"
        )
        graph.upsert_edge(
            CTX,
            src=inv,
            dst=ven,
            kind="issued_by",
            source_system="v1",
            provenance="system_record",
            confidence=1.0,
            observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        invs.append(inv)
    ids = [
        store.upsert(CTX, spec(invs[0], "10", dedupe_key="A:x0", type="T-A"))[
            0
        ],
        store.upsert(CTX, spec(invs[1], "20", dedupe_key="B:x1", type="T-B"))[
            0
        ],
        store.upsert(CTX, spec(invs[2], "30", dedupe_key="A:x2", type="T-A"))[
            0
        ],
    ]
    return url, store, {"a": ven_a, "b": ven_b}, invs, ids


def test_list_filters_by_vendor_statuses_types_and_pages(book):
    _, store, vendors, _, ids = book
    assert {f["id"] for f in store.list(T, vendor_node=vendors["a"])} == {
        ids[0],
        ids[1],
    }
    assert [f["id"] for f in store.list(T, types=["T-A"])] == [ids[2], ids[0]]
    store.transition(HUMAN, ids[0], "triaged", actor="c@x.test")
    assert {f["id"] for f in store.list(T, statuses=["triaged"])} == {ids[0]}
    page = store.list(T, limit=2, offset=1)
    assert [f["id"] for f in page] == [ids[1], ids[0]]
    assert store.count(T, types=["T-A"]) == 2 and store.count(T) == 3


def test_list_filters_by_time_window(book):
    url, store, _, _, ids = book
    with tenant_session(T, url) as s:
        row = s.get(models.TrustFinding, ids[0])
        row.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    cutoff = datetime(2025, 6, 1, tzinfo=timezone.utc)
    assert {f["id"] for f in store.list(T, until=cutoff)} == {ids[0]}
    assert ids[0] not in {f["id"] for f in store.list(T, since=cutoff)}


def test_a_re_versioned_invoice_still_lists_under_its_vendor(book):
    url, store, vendors, invs, ids = book
    graph = GraphStore(url)
    new = graph.upsert_node(
        CTX,
        kind="invoice",
        natural_key="invoices:x2",
        attrs={"decision": "approve"},
    )
    assert new != invs[2]
    graph.upsert_edge(
        CTX,
        src=new,
        dst=vendors["b"],
        kind="issued_by",
        source_system="v1",
        provenance="system_record",
        confidence=1.0,
        observed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    assert [f["id"] for f in store.list(T, vendor_node=vendors["b"])] == [
        ids[2]
    ]


def test_summary_counts_by_status_type_and_vendor(book):
    _, store, vendors, _, ids = book
    store.transition(HUMAN, ids[1], "triaged", actor="c@x.test")
    out = store.summary(T)
    assert out["by_status"] == {"detected": 2, "triaged": 1}
    assert out["by_type"] == {"T-A": 2, "T-B": 1}
    assert out["vendors"] == [
        {"node": vendors["a"], "count": 2},
        {"node": vendors["b"], "count": 1},
    ]


def test_events_are_the_ledger_with_finding_context(book):
    _, store, _, invs, ids = book
    store.transition(HUMAN, ids[0], "triaged", actor="c@x.test")
    store.transition(HUMAN, ids[0], "accepted", actor="c@x.test")
    store.transition(HUMAN, ids[0], "claimed", actor="c@x.test")
    store.transition(
        HUMAN, ids[0], "recovered", actor="c@x.test", amount=Decimal("7.50")
    )
    [row] = store.events(T, to_status=("recovered", "prevented"))
    assert row["finding_id"] == ids[0] and row["type"] == "T-A"
    assert row["transaction_node"] == invs[0]
    assert row["amount"] == "7.5000" and row["actor"] == "c@x.test"
    assert row["impact"] == "10.0000" and row["currency"] == "INR"
    assert len(store.events(T)) == 7  # 3 detected + 4 transitions
