"""payment_hold: an open hold-type finding, from an engine in enforcing, stops payment."""

from datetime import datetime, timezone
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


@pytest.fixture
def evaluated(env):
    """The invoice has been checked by every engine that can hold."""
    url, _, _ = env
    stamp(url, ["payment", "provenance", "gst_itc"])
    return env


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


def test_open_hold_in_enforcing_holds(env, evaluated):
    url, store, node = env
    add(store, node, "T-PAYHOLD")
    message = hold(url, {"payment": "enforcing"})
    assert "T-PAYHOLD" in message and "T-PAYHOLD summary" in message


def test_advisory_or_rejected_does_not_hold(env, evaluated):
    url, store, node = env
    fid = add(store, node, "T-PAYHOLD")
    add(store, node, "T-PAYNOTE")
    assert hold(url, {"payment": "advisory"}) is None
    store.transition(HUMAN, fid, "triaged", actor="c@x.test")
    store.transition(HUMAN, fid, "rejected", actor="c@x.test", reason="wrong")
    assert hold(url, {"payment": "enforcing"}) is None


def test_engine_failed_hold_follows_the_failed_engines_mode(env, evaluated):
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


def test_a_hold_is_seen_without_its_engine_being_imported(env, evaluated):
    """The API process never imports engine packages: the finding itself says
    it holds and which engine owns it (stamped by evaluate)."""
    url, store, node = env
    add(store, node, "PAY-NEVER-REGISTERED", engine="payment", hold=True)
    assert "PAY-NEVER-REGISTERED" not in finding_model.HOLD_TYPES
    assert "PAY-NEVER-REGISTERED" in hold(url, {"payment": "enforcing"})


def test_gate_asks_nothing_when_no_engine_is_enforcing(env, monkeypatch):
    url, store, node = env
    add(store, node, "T-PAYHOLD")

    def explode(*a, **k):
        raise AssertionError("queried findings with every engine off")

    monkeypatch.setattr(gate.FindingStore, "list", explode)
    assert hold(url, {"payment": "advisory"}) is None


def test_the_newest_invoice_row_of_a_case_decides(env, evaluated):
    url, store, node = env
    with tenant_session(T, url) as s:
        s.add(
            models.Document(
                tenant_id=T, id="d0", sha256="b" * 64, storage_key="k0"
            )
        )
        s.flush()
        s.add(
            models.Invoice(
                tenant_id=T,
                id="inv-0",
                document_id="d0",
                case_id="case-1",
                total_inc_tax=Decimal("1"),
                status="done",
                created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        )
    add(store, node, "T-PAYHOLD")
    assert hold(url, {"payment": "enforcing"}) is not None


def test_a_hold_survives_the_invoice_node_being_re_versioned(env, evaluated):
    """A sync that changes the invoice's attrs mints a new node id; the
    finding must move with it or the gate stops seeing the hold."""
    url, store, node = env
    add(store, node, "T-PAYHOLD")
    new = GraphStore(url).upsert_node(
        CTX,
        kind="invoice",
        natural_key="invoices:inv-1",
        ref_table="invoices",
        ref_id="inv-1",
        attrs={"decision": "approve"},
    )
    assert new != node
    assert hold(url, {"payment": "enforcing"}) is not None
    [f] = store.list(T, type="T-PAYHOLD")
    assert f["transaction_node"] == new


def stamp(url, engines, natural_key="trust_evaluation:invoices:inv-1"):
    GraphStore(url).upsert_node(
        CTX,
        kind="trust_evaluation",
        natural_key=natural_key,
        attrs=dict.fromkeys(engines, "2030-01-01T00:00:00+00:00"),
    )


def test_an_invoice_no_enforcing_engine_has_checked_yet_is_held(env):
    url, _, _ = env
    message = hold(url, {"payment": "enforcing"})
    assert (
        message and "until the trust checks" in message and "payment" in message
    )


def test_an_invoice_its_enforcing_engines_checked_is_not_held(env):
    url, _, _ = env
    stamp(url, ["payment"])
    assert hold(url, {"payment": "enforcing"}) is None
    # Another hold engine switched to enforcing has not checked it yet.
    assert (
        hold(url, {"payment": "enforcing", "gst_itc": "enforcing"}) is not None
    )


def test_a_batch_run_after_the_invoice_arrived_counts(env):
    url, _, _ = env
    stamp(url, ["payment"], natural_key="trust_evaluation:tenant")
    assert hold(url, {"payment": "enforcing"}) is None


def test_a_batch_run_before_the_invoice_arrived_does_not_count(env):
    url, _, _ = env
    GraphStore(url).upsert_node(
        CTX,
        kind="trust_evaluation",
        natural_key="trust_evaluation:tenant",
        attrs={"payment": "2000-01-01T00:00:00+00:00"},
    )
    assert hold(url, {"payment": "enforcing"}) is not None


def test_an_insight_engine_in_enforcing_does_not_wait(env):
    url, _, _ = env
    assert hold(url, {"leakage": "enforcing"}) is None


def test_an_invoice_not_yet_in_the_graph_is_held(env):
    url, _, _ = env
    with tenant_session(T, url) as s:
        s.add(
            models.Document(
                tenant_id=T, id="d9", sha256="9" * 64, storage_key="k9"
            )
        )
        s.flush()
        s.add(
            models.Invoice(
                tenant_id=T,
                id="inv-9",
                document_id="d9",
                case_id="case-9",
                total_inc_tax=Decimal("5"),
                status="done",
            )
        )
    message = gate.payment_hold(
        T, "case-9", url=url, settings_store=Store({"payment": "enforcing"})
    )
    assert message and "until the trust checks" in message
