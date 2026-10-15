"""payment_hold: an open hold-type finding, from an engine in enforcing, stops payment."""

from decimal import Decimal

import pytest

from invoice_processing.api import service
from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust import gate
from invoice_processing.trust.findings import model as finding_model
from invoice_processing.trust.findings.model import Citation, FindingSpec
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.store import GraphStore

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="engine")
HUMAN = RunContext(tenant_id=T, case_id="", run_id="r", actor="c@x.test")


class Store:
    def __init__(self, modes):
        self.modes = modes

    def get(self, tenant_id):
        return {"trust_engine_modes": self.modes}


@pytest.fixture
def env(scratch_db, monkeypatch):
    url = scratch_db("gate.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
        s.flush()
        s.add(
            models.Document(
                tenant_id=T, id="d1", sha256="a" * 64, storage_key="k"
            )
        )
        s.flush()
        s.add(
            models.Invoice(
                tenant_id=T,
                id="inv-1",
                document_id="d1",
                case_id="case-1",
                total_inc_tax=Decimal("10"),
                status="done",
            )
        )
    graph = GraphStore(url)
    node = graph.upsert_node(
        CTX,
        kind="invoice",
        natural_key="invoices:inv-1",
        ref_table="invoices",
        ref_id="inv-1",
    )
    monkeypatch.setattr(service, "_graph_store", graph)
    finding_model.register_type(
        "T-PAYHOLD", "hold", engine="payment", hold=True
    )
    finding_model.register_type("T-PAYNOTE", "note", engine="payment")
    return url, FindingStore(url), node


def add(store, node, code, **details):
    fid, _ = store.upsert(
        CTX,
        FindingSpec(
            type=code,
            transaction_node=node,
            dedupe_key=f"{code}:{node}",
            impact=None,
            currency=None,
            confidence=1.0,
            engine_version="t",
            rule_version="r",
            model_version="none",
            summary=f"{code} summary",
            details=details,
            citations=(Citation(node),),
        ),
    )
    return fid


def hold(url, modes):
    return gate.payment_hold(T, "case-1", url=url, settings_store=Store(modes))


def test_no_graph_store_means_no_hold(env, monkeypatch):
    url, store, node = env
    add(store, node, "T-PAYHOLD")
    monkeypatch.setattr(service, "_graph_store", None)
    monkeypatch.setattr(service, "_database_configured", lambda: False)
    assert hold(url, {"payment": "enforcing"}) is None


def test_open_hold_in_enforcing_holds(env):
    url, store, node = env
    add(store, node, "T-PAYHOLD")
    message = hold(url, {"payment": "enforcing"})
    assert "T-PAYHOLD" in message and "T-PAYHOLD summary" in message


def test_advisory_or_rejected_does_not_hold(env):
    url, store, node = env
    fid = add(store, node, "T-PAYHOLD")
    add(store, node, "T-PAYNOTE")
    assert hold(url, {"payment": "advisory"}) is None
    store.transition(HUMAN, fid, "triaged", actor="c@x.test")
    store.transition(HUMAN, fid, "rejected", actor="c@x.test", reason="wrong")
    assert hold(url, {"payment": "enforcing"}) is None


def test_engine_failed_hold_follows_the_failed_engines_mode(env):
    url, store, node = env
    add(store, node, "TRUST-ENGINE-FAILED", engine="payment")
    assert hold(url, {"payment": "enforcing"}) is not None
    assert hold(url, {"payment": "shadow"}) is None


def test_unknown_case_or_unsynced_invoice_does_not_hold(env):
    url, _, _ = env
    assert (
        gate.payment_hold(
            T, "nope", url=url, settings_store=Store({"payment": "enforcing"})
        )
        is None
    )
