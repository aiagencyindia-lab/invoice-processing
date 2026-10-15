"""The engine runtime: discovery by convention, modes, failure contract, batch."""

from decimal import Decimal

import pytest

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust import engine as engine_mod
from invoice_processing.trust import evaluate as ev
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.engine import EngineContext
from invoice_processing.trust.findings import model as finding_model
from invoice_processing.trust.findings.model import Citation, FindingSpec
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="worker")


class Store:
    def __init__(self, settings=None):
        self.settings = settings or {}

    def get(self, tenant_id):
        return self.settings


@pytest.fixture
def env(scratch_db, monkeypatch):
    url = scratch_db("engine.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
        s.flush()
        s.add(
            models.Document(
                tenant_id=T, id="doc-1", sha256="a" * 64, storage_key="k1"
            )
        )
        s.add(
            models.Document(
                tenant_id=T,
                id="doc-c",
                sha256="c" * 64,
                storage_key="kc",
                kind="contract",
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
                total_inc_tax=Decimal("100"),
                status="done",
            )
        )
    monkeypatch.setattr(ev, "RUNNERS", {})
    for code in list(finding_model.TYPES):
        if code.startswith("T-"):
            finding_model.TYPES.pop(code)
    return url, GraphStore(url), LinkStore(url)


def run(env, document_id="doc-1", modes=None, **kw):
    url, graph, links = env
    store = Store({"trust_engine_modes": modes or {}})
    return ev.evaluate(
        CTX,
        document_id,
        graph=graph,
        links=links,
        url=url,
        settings_store=store,
        decider=FakeDecider({}, fail=True),
        blobs=object(),
        **kw,
    )


def finding_for(ectx, code="T-NOTE"):
    node = ectx.invoice_node or ectx.graph.upsert_node(
        ectx.ctx, kind="contract", natural_key="c1"
    )
    return FindingSpec(
        type=code,
        transaction_node=node,
        dedupe_key=f"{code}:{node}",
        impact=None,
        currency=None,
        confidence=0.9,
        engine_version="t-1",
        rule_version="r",
        model_version="none",
        summary="test",
        citations=(Citation(node),),
    )


def test_missing_engine_package_is_skipped(env, monkeypatch):
    # Every engine package exists once Wave 1 lands, so absence is simulated.
    monkeypatch.setattr(ev, "_import_engine", lambda name: None)
    out = run(env, modes={"provenance": "advisory"})
    assert out["engines"]["provenance"] == "missing"


def test_off_engine_does_not_run(env):
    calls = []
    ev.RUNNERS["leakage"] = lambda ectx: calls.append(ectx) or []
    out = run(env, modes={})
    assert calls == [] and out["engines"]["leakage"] == "off"


def test_findings_are_stamped_with_mode_and_upserted(env):
    url, _, _ = env
    finding_model.register_type("T-NOTE", "note", engine="leakage")
    ev.RUNNERS["leakage"] = lambda ectx: [finding_for(ectx)]
    out = run(env, modes={"leakage": "shadow"})
    assert out["engines"]["leakage"] == {"mode": "shadow", "findings": 1}
    [f] = FindingStore(url).list(T, type="T-NOTE")
    assert f["details"]["mode"] == "shadow"


def test_insight_engine_failure_is_logged_not_raised(env):
    def boom(ectx):
        raise RuntimeError("engine bug")

    ev.RUNNERS["leakage"] = boom
    out = run(env, modes={"leakage": "enforcing"})
    assert out["engines"]["leakage"] == "failed"
    url, _, _ = env
    assert FindingStore(url).list(T, type="TRUST-ENGINE-FAILED") == []


def test_hold_engine_failure_in_enforcing_writes_engine_failed_and_a_clean_run_retires_it(
    env,
):
    url, _, _ = env
    finding_model.register_type("T-HOLD", "hold", engine="payment", hold=True)
    state = {"fail": True}

    def flaky(ectx):
        if state["fail"]:
            raise RuntimeError("provider bug")
        return []

    ev.RUNNERS["payment"] = flaky
    run(env, modes={"payment": "enforcing"})
    [failed] = FindingStore(url).list(T, type="TRUST-ENGINE-FAILED")
    assert failed["status"] == "detected"
    assert failed["details"]["engine"] == "payment"
    assert "TRUST-ENGINE-FAILED" in finding_model.HOLD_TYPES
    state["fail"] = False
    run(env, modes={"payment": "enforcing"})
    [retired] = FindingStore(url).list(T, type="TRUST-ENGINE-FAILED")
    assert retired["status"] == "rejected"


def test_contract_document_runs_engines_without_an_invoice(env):
    seen = {}

    def capture(ectx):
        seen["doc"], seen["node"] = ectx.document, ectx.invoice_node
        return []

    ev.RUNNERS["leakage"] = capture
    run(env, document_id="doc-c", modes={"leakage": "advisory"})
    assert seen["doc"]["kind"] == "contract" and seen["doc"]["id"] == "doc-c"
    assert seen["doc"]["storage_key"] == "kc" and seen["node"] is None


def test_batch_run_has_no_document(env):
    seen = {}
    ev.RUNNERS["leakage"] = lambda ectx: seen.update(doc=ectx.document) or []
    run(env, document_id=None, modes={"leakage": "advisory"})
    assert seen == {"doc": None}


def test_unknown_document_is_skipped(env):
    assert run(env, document_id="nope") == {"skipped": "no document"}


def test_threshold_reads_tenant_override_then_default(env):
    _, graph, links = env
    ectx = EngineContext(
        ctx=CTX,
        mode="advisory",
        document=None,
        invoice_node=None,
        graph=graph,
        links=links,
        findings=None,
        providers=None,
        settings={"trust_thresholds": {"payment.min_history": 9}},
        decider=None,
        blobs=None,
        url=None,
    )
    assert ectx.threshold("payment.min_history") == 9
    assert ectx.threshold("payment.amount_zscore") == 3.0
    with pytest.raises(ValueError, match="No such threshold"):
        ectx.threshold("nope")


def test_transactions_is_memoised(env, monkeypatch):
    calls = []
    monkeypatch.setattr(
        engine_mod,
        "transactions",
        lambda t, *, graph, as_of=None: calls.append(1) or [],
    )
    _, graph, links = env
    ectx = EngineContext(
        ctx=CTX,
        mode="advisory",
        document=None,
        invoice_node=None,
        graph=graph,
        links=links,
        findings=None,
        providers=None,
        settings={},
        decider=None,
        blobs=None,
        url=None,
    )
    ectx.transactions()
    ectx.transactions()
    assert calls == [1]


def test_nothing_is_built_when_every_engine_is_off(env, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("built a default while every engine is off")

    monkeypatch.setattr(ev, "_default_providers", explode)
    monkeypatch.setattr(ev, "_default_decider", explode)
    monkeypatch.setattr(ev, "_default_blobs", explode)
    url, graph, links = env
    out = ev.evaluate(
        CTX, "doc-1", graph=graph, links=links, url=url, settings_store=Store()
    )
    assert set(out["engines"].values()) == {"off"}


def test_findings_are_stamped_with_engine_and_hold(env):
    url, _, _ = env
    finding_model.register_type("T-HOLD2", "hold", engine="payment", hold=True)
    ev.RUNNERS["payment"] = lambda ectx: [finding_for(ectx, "T-HOLD2")]
    run(env, modes={"payment": "advisory"})
    [f] = FindingStore(url).list(T, type="T-HOLD2")
    assert f["details"]["engine"] == "payment" and f["details"]["hold"] is True


def test_an_engine_whose_import_breaks_is_isolated(env, monkeypatch):
    def broken(name):
        if name == "payment":
            raise ModuleNotFoundError(
                "No module named 'pdfplumber'", name="pdfplumber"
            )

    monkeypatch.setattr(ev, "_import_engine", broken)
    seen = []
    ev.RUNNERS["leakage"] = lambda ectx: seen.append(1) or []
    out = run(env, modes={"payment": "enforcing", "leakage": "advisory"})
    assert out["engines"]["payment"] == "failed" and seen == [1]
    url, _, _ = env
    # payment owns holds even though it never registered: fail closed.
    [failed] = FindingStore(url).list(T, type="TRUST-ENGINE-FAILED")
    assert failed["details"]["engine"] == "payment"


def test_a_hold_no_longer_found_by_a_clean_run_is_released(env):
    url, _, _ = env
    finding_model.register_type("T-HOLD3", "hold", engine="payment", hold=True)
    state = {"emit": True}
    ev.RUNNERS["payment"] = lambda ectx: (
        [finding_for(ectx, "T-HOLD3")] if state["emit"] else []
    )
    run(env, modes={"payment": "enforcing"})
    assert FindingStore(url).list(T, type="T-HOLD3")[0]["status"] == "detected"
    state["emit"] = False
    run(env, modes={"payment": "enforcing"})
    [f] = FindingStore(url).list(T, type="T-HOLD3")
    assert f["status"] == "rejected"


def test_a_human_decided_hold_is_not_released_by_the_engine(env):
    url, _, _ = env
    finding_model.register_type("T-HOLD4", "hold", engine="payment", hold=True)
    state = {"emit": True}
    ev.RUNNERS["payment"] = lambda ectx: (
        [finding_for(ectx, "T-HOLD4")] if state["emit"] else []
    )
    run(env, modes={"payment": "enforcing"})
    store = FindingStore(url)
    [f] = store.list(T, type="T-HOLD4")
    human = RunContext(tenant_id=T, case_id="", run_id="h", actor="c@x.test")
    store.transition(human, f["id"], "triaged", actor="c@x.test")
    store.transition(human, f["id"], "accepted", actor="c@x.test")
    state["emit"] = False
    run(env, modes={"payment": "enforcing"})
    assert store.list(T, type="T-HOLD4")[0]["status"] == "accepted"


def test_a_clean_run_also_releases_a_hold_engines_cleared_insight(env):
    """An ITC-at-risk amount must not outlive the hold it was priced from."""
    url, _, _ = env
    finding_model.register_type("T-RISK", "risk", engine="gst_itc")
    state = {"emit": True}
    ev.RUNNERS["gst_itc"] = lambda ectx: (
        [finding_for(ectx, "T-RISK")] if state["emit"] else []
    )
    run(env, modes={"gst_itc": "advisory"})
    state["emit"] = False
    run(env, modes={"gst_itc": "advisory"})
    assert FindingStore(url).list(T, type="T-RISK")[0]["status"] == "rejected"


def test_an_insight_engines_findings_are_not_released(env):
    url, _, _ = env
    finding_model.register_type("T-LEAK", "leak", engine="leakage")
    state = {"emit": True}
    ev.RUNNERS["leakage"] = lambda ectx: (
        [finding_for(ectx, "T-LEAK")] if state["emit"] else []
    )
    run(env, modes={"leakage": "advisory"})
    state["emit"] = False
    run(env, modes={"leakage": "advisory"})
    assert FindingStore(url).list(T, type="T-LEAK")[0]["status"] == "detected"


def _stamp(graph, key):
    [n] = [
        n
        for n in graph.nodes(T, kind="trust_evaluation")
        if n["natural_key"] == key
    ] or [None]
    return (n or {}).get("attrs") or {}


def test_a_document_run_records_which_engines_checked_the_invoice(env):
    _, graph, _ = env
    ev.RUNNERS["payment"] = lambda ectx: []
    ev.RUNNERS["leakage"] = lambda ectx: []
    run(env, modes={"payment": "enforcing", "leakage": "advisory"})
    assert set(_stamp(graph, "trust_evaluation:invoices:inv-1")) == {
        "payment",
        "leakage",
    }
    ev.RUNNERS["gst_itc"] = lambda ectx: []
    run(env, modes={"gst_itc": "advisory"})
    assert set(_stamp(graph, "trust_evaluation:invoices:inv-1")) == {
        "payment",
        "leakage",
        "gst_itc",
    }


def test_a_missing_engine_is_not_recorded_as_having_checked(env, monkeypatch):
    _, graph, _ = env
    monkeypatch.setattr(ev, "_import_engine", lambda name: None)
    run(env, modes={"payment": "enforcing"})
    assert "payment" not in _stamp(graph, "trust_evaluation:invoices:inv-1")


def test_a_batch_run_records_a_tenant_wide_check(env):
    _, graph, _ = env
    ev.RUNNERS["payment"] = lambda ectx: []
    run(env, document_id=None, modes={"payment": "advisory"})
    assert "payment" in _stamp(graph, "trust_evaluation:tenant")


def test_a_contract_run_is_not_a_tenant_wide_check(env):
    _, graph, _ = env
    ev.RUNNERS["payment"] = lambda ectx: []
    run(env, document_id="doc-c", modes={"payment": "advisory"})
    assert _stamp(graph, "trust_evaluation:tenant") == {}
