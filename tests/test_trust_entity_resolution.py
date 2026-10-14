"""Two records are one vendor when the evidence says so; a human sees the doubtful ones."""

import pytest

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.graph import resolve
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="sync")


def keys(**attrs):
    return resolve.natural_keys(attrs)


def test_gstin_match_scores_one_and_pan_is_derived():
    a = keys(gstin="27AAPFU0939F1ZV", name="Acme Pvt Ltd")
    assert a["pan"] == "AAPFU0939F"
    assert resolve.score(a, keys(gstin="27AAPFU0939F1ZV", name="ACME")) == (
        1.0,
        "gstin",
    )


def test_name_plus_domain_auto_links_but_name_alone_is_proposed():
    a = keys(name="Acme Private Limited", email_domain="acme.in")
    b = keys(name="ACME PVT. LTD", email_domain="acme.in")
    s, method = resolve.score(a, b)
    assert resolve.band(s) == "auto"
    assert "name" in method and "email_domain" in method
    s2, _ = resolve.score(
        keys(name="Acme Pvt Ltd"), keys(name="acme private limited")
    )
    assert resolve.band(s2) == "proposed"


def test_below_070_never_links():
    s, _ = resolve.score(keys(name="Acme Steel Works"), keys(name="Acme Tools"))
    assert resolve.band(s) == "none"


def test_conflicting_gstins_never_link_even_with_same_name():
    a = keys(gstin="27AAPFU0939F1ZV", name="Acme Pvt Ltd")
    b = keys(gstin="29AABCU9603R1ZX", name="Acme Pvt Ltd")
    assert resolve.score(a, b) == (0.0, "gstin-conflict")


@pytest.fixture
def env(scratch_db):
    url = scratch_db("links.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    return GraphStore(url), LinkStore(url)


def vendor(graph, key, **attrs):
    return graph.upsert_node(
        CTX, kind="vendor", natural_key=key, natural_keys=keys(**attrs)
    )


def test_run_links_and_proposes_and_resolved_id_follows_accepted_links(env):
    graph, links = env
    a = vendor(graph, "v:a", gstin="27AAPFU0939F1ZV", name="Acme Pvt Ltd")
    b = vendor(graph, "v:b", gstin="27AAPFU0939F1ZV", name="ACME")
    c = vendor(graph, "v:c", name="acme private limited")
    out = resolve.run(CTX, graph, links, decider=FakeDecider({"same": True}))
    assert out == {"auto": 1, "proposed": 2}
    assert links.resolved_id(T, a) == links.resolved_id(T, b) == min(a, b)
    assert links.resolved_id(T, c) == c
    proposed = links.open_links(T, status="proposed")
    assert len(proposed) == 2
    assert all("model says: yes" in (p["reason"] or "") for p in proposed)
    accepted = links.decide(
        CTX, proposed[0]["id"], accept=True, actor="controller@x.test"
    )
    assert accepted["status"] == "accepted"
    assert links.resolved_id(T, c) == min(a, b, c)
    # Running again changes nothing.
    assert resolve.run(CTX, graph, links, decider=FakeDecider({})) == {
        "auto": 0,
        "proposed": 0,
    }


def test_reversal_keeps_both_nodes_and_is_audited(env):
    graph, links = env
    a = graph.upsert_node(CTX, kind="vendor", natural_key="v:a")
    b = graph.upsert_node(CTX, kind="vendor", natural_key="v:b")
    link = links.propose_or_link(CTX, a, b, 1.0, "gstin")
    assert link["status"] == "auto"
    out = links.reverse(
        CTX, link["id"], actor="controller@x.test", reason="different branches"
    )
    assert out["reversed_at"] is not None and links.resolved_id(T, b) == b
    assert len(graph.nodes(T, kind="vendor")) == 2
    with tenant_session(T, links.url) as s:
        count = (
            s.query(models.AuditEvent)
            .filter_by(action="entity_link_reversed")
            .count()
        )
        assert count == 1
