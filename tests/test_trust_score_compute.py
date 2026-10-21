"""score.compute: the trust_score node evaluate() writes after the engines."""

import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fresh_schema import fresh_schema

from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust import evaluate as ev
from invoice_processing.trust import score
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.engine import EngineContext
from invoice_processing.trust.findings.model import Citation, FindingSpec
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.types import DIMENSIONS

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="worker")
AT = datetime(2026, 10, 1, tzinfo=timezone.utc)


class Store:
    def __init__(self, settings):
        self.settings = settings

    def get(self, tenant_id):
        return self.settings


@pytest.fixture
def env(scratch_db, monkeypatch):
    url = scratch_db("score.db")
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
        s.flush()
        s.add(
            models.Document(
                tenant_id=T, id="doc-1", sha256="a" * 64, storage_key="k1"
            )
        )
        s.flush()
        s.add(
            models.Invoice(
                tenant_id=T,
                id="inv-1",
                document_id="doc-1",
                case_id="case-1",
                invoice_number="INV-1",
                vendor_name="Acme",
                total_inc_tax=Decimal("1000"),
                status="done",
                fields={"workflow": {"decision": "approve"}},
            )
        )
    graph = GraphStore(url)
    invoice = graph.upsert_node(
        CTX,
        kind="invoice",
        natural_key="invoices:inv-1",
        ref_table="invoices",
        ref_id="inv-1",
        attrs={
            "amount": "1000.00",
            "document_id": "doc-1",
            "document_sha256": "a" * 64,
        },
        at=AT,
    )
    monkeypatch.setattr(ev, "RUNNERS", {})
    return {
        "url": url,
        "graph": graph,
        "findings": FindingStore(url),
        "invoice": invoice,
    }


def ectx(env, modes=None, *, invoice=True, document=None, now=AT):
    return EngineContext(
        ctx=CTX,
        mode="advisory",
        document=document,
        invoice_node=env["invoice"] if invoice else None,
        graph=env["graph"],
        links=LinkStore(env["url"]),
        findings=env["findings"],
        providers=None,
        settings={"trust_engine_modes": modes or {"payment": "advisory"}},
        decider=None,
        blobs=None,
        url=env["url"],
        now=now,
    )


def spec(env, code="PAY-UNVERIFIED", engine="payment", **details):
    return FindingSpec(
        type=code,
        transaction_node=env["invoice"],
        dedupe_key=f"{code}:invoices:inv-1",
        impact=Decimal("1000.00"),
        currency="INR",
        confidence=1.0,
        engine_version=f"{engine}-1",
        rule_version=f"FR:{code}",
        model_version="none",
        summary="s",
        details={"engine": engine, "mode": "advisory", **details},
        citations=(Citation(env["invoice"]),),
    )


def node(env):
    return env["graph"].open_node(
        T, "trust_score", "trust_score:invoices:inv-1"
    )


def test_the_score_node_has_six_dimensions_and_its_versions(env):
    env["findings"].upsert(CTX, spec(env))
    score.compute(ectx(env, {"payment": "enforcing"}), {})
    n = node(env)
    a = n["attrs"]
    assert a["score_version"] == "score-1"
    assert set(a["dimensions"]) == set(DIMENSIONS)
    assert a["dimensions"]["identity"]["value"] == 0.0
    assert a["dimensions"]["identity"]["hold"] is True
    assert a["dimensions"]["authorization"]["value"] == 1.0
    assert a["holds"] == a["dimensions"]["identity"]["drivers"]
    assert a["versions"]["engines"] == {"payment": "payment-1"}
    assert a["versions"]["rules"] == {"PAY-UNVERIFIED": "FR:PAY-UNVERIFIED"}
    assert a["mode"]["payment"] == "enforcing" and a["mode"]["leakage"] == "off"
    assert a["computed_at"] == AT.isoformat()


def test_a_rerun_with_the_same_inputs_keeps_the_node(env):
    score.compute(ectx(env), {})
    first = node(env)
    score.compute(ectx(env, now=AT + timedelta(hours=1)), {})
    assert node(env)["id"] == first["id"]
    assert node(env)["attrs"]["computed_at"] == AT.isoformat()
    env["findings"].upsert(CTX, spec(env))
    later = AT + timedelta(hours=2)
    score.compute(ectx(env, now=later), {})
    second = node(env)
    assert second["id"] != first["id"]
    assert second["attrs"]["computed_at"] == later.isoformat()
    # The old version is still there for ?as_of.
    old = score.score_node(
        env["graph"],
        T,
        env["graph"].node(T, env["invoice"]),
        as_of=AT + timedelta(minutes=30),
    )
    assert old["id"] == first["id"]


def test_reality_and_authenticity_are_read_by_data_contract(env):
    g = env["graph"]
    rid = g.upsert_node(
        CTX,
        kind="reality_check",
        natural_key="reality:invoices:inv-1",
        attrs={"result": "PARTIAL", "engine_version": "reality-1"},
    )
    did = g.upsert_node(
        CTX,
        kind="document_version",
        natural_key=f"documents:doc-1:{'a' * 64}",
        attrs={"score": 0.85},
    )
    score.compute(
        ectx(env, {"reality": "advisory", "provenance": "shadow"}), {}
    )
    a = node(env)["attrs"]
    assert a["dimensions"]["reality"]["value"] == 0.6
    assert a["dimensions"]["authenticity"]["value"] == 0.85
    assert a["versions"]["reality"] == rid
    assert a["versions"]["provenance"] == did


def test_an_override_lowers_authorization(env):
    with tenant_session(T, env["url"]) as s:
        inv = s.get(models.Invoice, "inv-1")
        inv.fields = {
            "workflow": {
                "decision": "approve",
                "hold_overridden": {"message": "m", "finding_ids": ["f"]},
            }
        }
    score.compute(ectx(env), {})
    assert node(env)["attrs"]["dimensions"]["authorization"]["value"] == 0.7


def test_approval_nodes_drive_authorization(env):
    g = env["graph"]
    aid = g.upsert_node(
        CTX,
        kind="approval",
        natural_key="approval_steps:s1",
        attrs={"position": 1, "status": "pending", "decided_by": None},
    )
    g.upsert_edge(
        CTX,
        src=aid,
        dst=env["invoice"],
        kind="approves",
        source_system="v1",
        provenance="system_record",
        confidence=1.0,
        observed_at=AT,
    )
    score.compute(ectx(env), {})
    assert node(env)["attrs"]["dimensions"]["authorization"]["value"] == 0.5


def test_a_batch_run_scores_every_invoice(env):
    g = env["graph"]
    g.upsert_node(
        CTX,
        kind="invoice",
        natural_key="history:h1",
        ref_table="trust_history_records",
        ref_id="h1",
        attrs={"amount": "10.00"},
    )
    score.compute(ectx(env, invoice=False), {})
    keys = {n["natural_key"] for n in g.nodes(T, kind="trust_score")}
    assert keys == {"trust_score:invoices:inv-1", "trust_score:history:h1"}
    h = g.open_node(T, "trust_score", "trust_score:history:h1")["attrs"]
    assert h["dimensions"]["authorization"]["value"] is None


def test_a_contract_run_scores_nothing(env):
    score.compute(ectx(env, invoice=False, document={"id": "doc-c"}), {})
    assert env["graph"].nodes(T, kind="trust_score") == []


def test_evaluate_writes_the_score_after_the_engines(env):
    ev.RUNNERS["payment"] = lambda e: [
        dataclasses.replace(spec(env), transaction_node=e.invoice_node)
    ]
    ev.evaluate(
        CTX,
        "doc-1",
        graph=env["graph"],
        links=LinkStore(env["url"]),
        url=env["url"],
        settings_store=Store({"trust_engine_modes": {"payment": "advisory"}}),
        decider=FakeDecider({}, fail=True),
        blobs=object(),
    )
    invoice = env["graph"].nodes_by_ref(T, "invoices", ["inv-1"])["inv-1"]
    n = score.score_node(env["graph"], T, env["graph"].node(T, invoice))
    assert n["attrs"]["dimensions"]["identity"]["value"] == 0.0
    assert n["attrs"]["dimensions"]["identity"]["hold"] is False
