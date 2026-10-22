"""Graph rows are never updated in place; the past stays queryable."""

from datetime import UTC, datetime, timedelta

import pytest
from fresh_schema import fresh_schema

from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore

T = "tenant-acme"
T0 = datetime(2026, 10, 1, tzinfo=UTC)
T1 = T0 + timedelta(days=1)
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="t")


@pytest.fixture
def store(scratch_db):
    url = scratch_db("graph.db")
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    return GraphStore(url)


def vendor(store, name, at):
    return store.upsert_node(
        CTX, kind="vendor", natural_key="v1", attrs={"name": name}, at=at
    )


def test_upsert_same_attrs_is_idempotent(store):
    a = vendor(store, "Acme", T0)
    b = vendor(store, "Acme", T1)
    assert a == b and len(store.nodes(T, kind="vendor")) == 1


def test_changed_attrs_closes_old_row_and_as_of_sees_it(store):
    a = vendor(store, "Acme", T0)
    b = vendor(store, "Acme Ltd", T1)
    assert a != b
    assert [n["attrs"]["name"] for n in store.nodes(T, kind="vendor")] == [
        "Acme Ltd"
    ]
    assert [
        n["attrs"]["name"] for n in store.nodes(T, kind="vendor", as_of=T0)
    ] == ["Acme"]
    assert store.node(T, a)["valid_to"] is not None


def test_bank_attrs_are_masked_in_graph(store):
    nid = store.upsert_node(
        CTX,
        kind="vendor",
        natural_key="v1",
        attrs={"bank_account_number": "123456789012"},
    )
    assert store.node(T, nid)["attrs"]["bank_account_number"] == "…9012"


def test_edge_rejects_bad_provenance_and_confidence(store):
    a = store.upsert_node(CTX, kind="invoice", natural_key="i1")
    b = store.upsert_node(CTX, kind="vendor", natural_key="v1")
    with pytest.raises(ValueError, match="provenance"):
        store.upsert_edge(
            CTX,
            src=a,
            dst=b,
            kind="issued_by",
            source_system="v1",
            provenance="guess",
            confidence=1,
            observed_at=T0,
        )
    with pytest.raises(ValueError, match="confidence"):
        store.upsert_edge(
            CTX,
            src=a,
            dst=b,
            kind="issued_by",
            source_system="v1",
            provenance="system_record",
            confidence=2,
            observed_at=T0,
        )


def test_edges_are_idempotent_and_filterable(store):
    a = store.upsert_node(CTX, kind="invoice", natural_key="i1")
    b = store.upsert_node(CTX, kind="vendor", natural_key="v1")
    kw = {
        "src": a,
        "dst": b,
        "kind": "issued_by",
        "source_system": "v1",
        "provenance": "system_record",
        "confidence": 1,
        "observed_at": T0,
    }
    assert store.upsert_edge(CTX, **kw) == store.upsert_edge(CTX, **kw)
    assert [e["dst"] for e in store.edges(T, src=a, kind="issued_by")] == [b]
    assert store.nodes_by_ref(T, "invoices", ["x"]) == {}


def test_other_tenant_sees_nothing(store):
    store.upsert_node(CTX, kind="vendor", natural_key="v1")
    assert store.nodes("tenant-globex", kind="vendor") == []


def test_reversioning_a_node_repoints_its_entity_links(store):
    links = LinkStore(store.url)
    a = store.upsert_node(
        CTX, kind="vendor", natural_key="va", attrs={"name": "A"}, at=T0
    )
    b = store.upsert_node(
        CTX, kind="vendor", natural_key="vb", attrs={"name": "B"}, at=T0
    )
    links.propose_or_link(CTX, a, b, 1.0, "gstin")
    a2 = store.upsert_node(
        CTX, kind="vendor", natural_key="va", attrs={"name": "A Ltd"}, at=T1
    )
    assert a2 != a
    [link] = links.open_links(T)
    assert {link["node_a"], link["node_b"]} == {a2, b}
    assert links.resolved_id(T, b) == min(a2, b)
