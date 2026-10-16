"""The Causal Why engine: model, counterfactuals, templates, pack, ratings."""

from __future__ import annotations

import pytest

from invoice_processing.trust.explain import model as xm

TXN = "n-txn"


def _explanation(**over):
    base = {
        "finding_id": "f1",
        "type": "LEAK-RATE",
        "version": xm.EXPLAIN_VERSION,
        "what_happened": "Billed above the contract rate.",
        "why": (xm.Step("The contract says 50.", "n-clause"),),
        "caused_by": (xm.Actor("vendor", "Acme", None),),
        "evidence": (
            xm.Evidence("n-clause", "contract_clause", "Clause", "50", None),
        ),
        "confidence": 0.9,
        "confidence_note": "High.",
        "next_action": "Short-pay.",
        "would_change": ("A reviewed clause.",),
        "counterfactual": None,
        "versions": {"explain_version": xm.EXPLAIN_VERSION},
        "transaction_node": TXN,
    }
    base.update(over)
    return xm.Explanation(**base)


def test_check_accepts_steps_on_cited_nodes_and_the_transaction():
    x = _explanation(
        why=(xm.Step("a", "n-clause"), xm.Step("b", TXN)),
        caused_by=(
            xm.Actor("vendor", "Acme", TXN),
            xm.Actor("engine", "e", None),
        ),
    )
    x.check({"n-clause"})  # no raise


def test_check_rejects_uncited_step():
    x = _explanation(why=(xm.Step("a", "n-other"),))
    with pytest.raises(xm.Uncited):
        x.check({"n-clause"})
    x = _explanation(caused_by=(xm.Actor("person", "p", "n-other"),))
    with pytest.raises(xm.Uncited):
        x.check({"n-clause"})
    x = _explanation(evidence=(xm.Evidence("n-other", "k", "l", None, None),))
    with pytest.raises(xm.Uncited):
        x.check({"n-clause"})


def test_check_rejects_empty_evidence_and_blank_step():
    with pytest.raises(xm.Uncited):
        _explanation(evidence=()).check({"n-clause"})
    with pytest.raises(xm.Uncited):
        _explanation(why=(xm.Step("  ", "n-clause"),)).check({"n-clause"})
    with pytest.raises(xm.Uncited):
        _explanation(why=()).check({"n-clause"})


def test_as_dict_matches_the_frontend_contract():
    d = _explanation().as_dict()
    assert set(d) == {
        "finding_id", "type", "version", "what_happened", "why", "caused_by",
        "evidence", "confidence", "confidence_note", "next_action",
        "would_change", "counterfactual", "versions", "transaction_node",
    }  # fmt: skip
    assert d["why"] == [
        {"text": "The contract says 50.", "node_id": "n-clause"}
    ]
    assert d["would_change"] == ["A reviewed clause."]


# -- fixtures: one tiny graph per registered type ----------------------------

import json  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402

import invoice_processing.trust.gst_itc  # noqa: E402
import invoice_processing.trust.leakage  # noqa: E402
import invoice_processing.trust.payment  # noqa: E402
import invoice_processing.trust.provenance  # noqa: E402, F401
from invoice_processing.trust.explain import counterfactual as cf  # noqa: E402
from invoice_processing.trust.explain import engine  # noqa: E402
from invoice_processing.trust.explain.templates import TEMPLATES  # noqa: E402
from invoice_processing.trust.findings import model as finding_model  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "trust_explain"
T = "tenant-acme"


class FakeGraph:
    """Just enough of GraphStore for the engine: node() and edges()."""

    def __init__(self, nodes: dict, edges: list):
        self._nodes = {
            nid: {
                "id": nid,
                "kind": n["kind"],
                "natural_key": n.get("natural_key", nid),
                "ref_table": n.get("ref_table"),
                "ref_id": n.get("ref_id"),
                "attrs": dict(n.get("attrs") or {}),
                "natural_keys": {},
                "valid_from": "2026-10-01T00:00:00+00:00",
                "valid_to": None,
            }
            for nid, n in nodes.items()
        }
        self._edges = [
            {
                "id": f"e{i}",
                "src": s,
                "dst": d,
                "kind": k,
                "source_system": "test",
                "observed_at": "2026-10-01T00:00:00+00:00",
                "provenance": "system_record",
                "confidence": 1.0,
                "attrs": {},
                "valid_from": "2026-10-01T00:00:00+00:00",
                "valid_to": None,
            }
            for i, (s, d, k) in enumerate(edges)
        ]

    def node(self, tenant_id, node_id):
        return self._nodes.get(node_id)

    def edges(self, tenant_id, *, src=None, dst=None, kind=None, as_of=None):
        return [
            e
            for e in self._edges
            if (src is None or e["src"] == src)
            and (dst is None or e["dst"] == dst)
            and (kind is None or e["kind"] == kind)
        ]


def load(code: str) -> tuple[dict, FakeGraph, dict]:
    data = json.loads((FIXTURES / f"{code}.json").read_text())
    f = {
        "owner_id": None,
        "dedupe_key": f"{code}:x",
        "decided_at": None,
        "events": [],
        **data["finding"],
    }
    return f, FakeGraph(data["nodes"], data["edges"]), data["expect"]


ALL_TYPES = sorted(finding_model.TYPES)


@pytest.mark.parametrize("code", ALL_TYPES)
def test_every_type_has_a_fixture(code):
    assert (FIXTURES / f"{code}.json").exists()


@pytest.mark.parametrize("code", ALL_TYPES)
def test_counterfactual(code):
    f, graph, expect = load(code)
    out = cf.counterfactual(f, graph.node(T, f["transaction_node"]))
    if "note" in expect:
        assert out.note == expect["note"]
        assert out.delta is None and out.payable_correct is None
    else:
        assert out.payable_now == expect["payable_now"]
        assert out.payable_correct == expect["payable_correct"]
        assert out.delta == expect["delta"]
        assert Decimal(out.delta) == Decimal(f["impact"])
        assert out.formula
    assert out.currency == f["currency"]


def test_counterfactual_without_an_amount_still_prices_the_delta():
    f, _graph, _ = load("LEAK-RATE")
    out = cf.counterfactual(f, {"id": "x", "kind": "invoice", "attrs": {}})
    assert out.payable_now is None and out.payable_correct is None
    assert out.delta == "100.00"


@pytest.mark.parametrize("code", ALL_TYPES)
def test_every_type_explains(code):
    f, graph, _ = load(code)
    x = engine.build(T, f, graph=graph)
    cited = {c["node_id"] for c in f["citations"]}
    x.check(cited)  # the engine already did; a second pass costs nothing
    assert x.type == code and x.finding_id == f["id"]
    assert x.version == xm.EXPLAIN_VERSION
    assert len(x.why) >= 1 and x.what_happened and x.next_action
    assert {e.node_id for e in x.evidence} == cited
    assert x.versions["explain_version"] == xm.EXPLAIN_VERSION
    assert x.versions["engine_version"] == f["engine_version"]
    assert x.counterfactual is not None
    assert all(
        a.kind in ("vendor", "person", "system", "engine", "provider")
        for a in x.caused_by
    )
    d = x.as_dict()
    json.dumps(d)  # serialisable as the route returns it
    if code not in TEMPLATES:
        assert x.what_happened == f["summary"]


def test_unknown_type_falls_back_to_generic():
    f, graph, _ = load("LEAK-RATE")
    f = {**f, "type": "NEW-THING"}
    x = engine.build(T, f, graph=graph)
    x.check({c["node_id"] for c in f["citations"]})
    assert x.what_happened == f["summary"]
    assert x.next_action == "Review the cited evidence and accept or reject."
    assert any(a.kind == "engine" for a in x.caused_by)


def test_template_that_cites_an_uncited_node_is_refused(monkeypatch):
    f, graph, _ = load("LEAK-RATE")
    monkeypatch.setitem(
        TEMPLATES,
        "LEAK-RATE",
        lambda ctx: {"why": [xm.Step("made up", "n-ven")]},
    )
    with pytest.raises(xm.Uncited):
        engine.build(T, f, graph=graph)


def test_missing_cited_node_is_still_evidence():
    f, graph, _ = load("LEAK-RATE")
    graph._nodes.pop("n-rate")
    x = engine.build(T, f, graph=graph)
    ev = {e.node_id: e for e in x.evidence}
    assert ev["n-rate"].kind == "missing" and ev["n-rate"].excerpt


def test_evidence_carries_source_and_labels():
    f, graph, _ = load("LEAK-RATE")
    x = engine.build(T, f, graph=graph)
    ev = {e.node_id: e for e in x.evidence}
    assert ev["n-inv"].source == {
        "document_url": "/api/invoices/case-1/document",
        "page": None,
    }
    assert ev["n-rate"].source == {
        "document_url": "/api/trust/contracts/doc-c/file",
        "page": 2,
    }
    assert "INV-1041" in ev["n-inv"].label
    assert (
        x.caused_by[0].kind == "vendor" and "Acme Steel" in x.caused_by[0].label
    )
    assert x.caused_by[0].node_id is None  # the vendor was not cited


# -- per-family templates ------------------------------------------------------


def test_every_engine_type_has_a_template():
    engine_types = {
        t
        for t, e in finding_model.TYPE_ENGINE.items()
        if e in ("leakage", "payment", "provenance", "gst_itc")
    }
    assert engine_types <= set(TEMPLATES)


def _steps(code):
    f, graph, _ = load(code)
    x = engine.build(T, f, graph=graph)
    return x, {s.node_id for s in x.why}, " ".join(s.text for s in x.why)


def test_leak_rate_walks_clause_line_and_overbilling():
    x, nodes, text = _steps("LEAK-RATE")
    assert {"n-rate", "n-line"} <= nodes
    assert "60.00" in text and "50" in text and "100.00" in text
    assert "short-pay" in x.next_action.lower()
    assert len(x.would_change) >= 2
    assert "not yet reviewed" in x.confidence_note


def test_dup_near_points_at_the_earlier_invoice_and_holds_the_later():
    x, nodes, text = _steps("DUP-NEAR")
    assert "n-inv0" in nodes
    assert "INV-1040" in text
    assert "hold" in x.next_action.lower()


def test_pay_unverified_starts_from_the_provider_evidence():
    x, _nodes, text = _steps("PAY-UNVERIFIED")
    assert x.why[0].node_id == "n-prov"
    assert "not_verified" in text and "****1234" in text
    assert any("call-back" in w.lower() for w in x.would_change)
    assert any(a.kind == "provider" for a in x.caused_by)


def test_prov_suspect_names_the_score_components():
    _x, _nodes, text = _steps("PROV-SUSPECT")
    assert "channel" in text and "meta" in text and "0.30" in text


def test_prov_tamper_cites_the_approved_twin():
    _x, nodes, text = _steps("PROV-TAMPER")
    assert "n-inv0" in nodes and "approved" in text


def test_gst_inactive_reads_lookup_then_status_then_itc():
    _x, nodes, text = _steps("GST-INACTIVE")
    assert "n-gst" in nodes and "cancelled" in text
    assert "ITC" in text or "input tax credit" in text.lower()


def test_leak_discount_cites_the_payment():
    _x, nodes, text = _steps("LEAK-DISCOUNT")
    assert "n-pay" in nodes and "10%" in text


def test_radar_renewal_is_about_the_contract_itself():
    x, nodes, text = _steps("RADAR-RENEWAL")
    assert "2026-11-30" in text and "n-term" in nodes
    assert x.counterfactual.note == cf.NO_MONEY


def test_caused_by_is_worded_as_likely_and_names_the_approver():
    f, graph, _ = load("LEAK-RATE")
    graph._nodes["n-appr"] = {
        "id": "n-appr",
        "kind": "approval",
        "attrs": {"decided_by": "boss@x.test", "status": "approved"},
    }
    graph._edges.append(
        {"src": "n-appr", "dst": "n-inv", "kind": "approves", "attrs": {}}
    )
    x = engine.build(T, f, graph=graph)
    assert any("likely" in a.label for a in x.caused_by)
    person = [a for a in x.caused_by if a.kind == "person"]
    assert (
        person
        and person[0].node_id is None
        and "boss@x.test" in person[0].label
    )


# -- evidence pack + HTML -------------------------------------------------------

import hashlib  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from invoice_processing.trust.explain import pack as xp  # noqa: E402
from invoice_processing.trust.explain import render  # noqa: E402

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
RATING = {
    "rater": "clerk@x.test",
    "correct": True,
    "useful": False,
    "note": "<script>alert(1)</script>",
    "at": "2026-10-16T11:00:00+00:00",
}


def test_pack_sha_is_stable_and_excludes_itself():
    f, graph, _ = load("LEAK-RATE")
    a = xp.evidence_pack(T, f, graph=graph, ratings=[RATING], now=NOW)
    b = xp.evidence_pack(T, f, graph=graph, ratings=[RATING], now=NOW)
    assert a == b and a["pack_version"] == 1
    body = {k: v for k, v in a.items() if k != "sha256"}
    assert (
        a["sha256"]
        == hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert a["generated_at"] == NOW.isoformat()
    assert a["versions"]["explain_version"] == xm.EXPLAIN_VERSION
    assert a["versions"]["lineage_version"]
    assert a["decision"] == {
        "status": "detected",
        "owner_id": None,
        "decided_at": None,
        "events": [],
    }
    assert a["ratings"] == [RATING]
    assert a["explanation"]["finding_id"] == f["id"]
    assert [link["stage"] for link in a["lineage"]][:1] == ["contract"]
    assert set(a) == {
        "pack_version", "generated_at", "finding", "explanation", "lineage",
        "nodes", "edges", "versions", "decision", "ratings", "sha256",
    }  # fmt: skip


def test_pack_edges_only_between_cited_nodes():
    f, graph, _ = load("LEAK-RATE")
    out = xp.evidence_pack(T, f, graph=graph, now=NOW)
    ids = {n["id"] for n in out["nodes"]}
    assert ids == {"n-inv", "n-con", "n-line", "n-rate"}  # not the vendor
    assert out["edges"] and all(
        e["src"] in ids and e["dst"] in ids for e in out["edges"]
    )
    assert {(e["src"], e["dst"]) for e in out["edges"]} == {
        ("n-line", "n-inv"),
        ("n-rate", "n-con"),
    }


def test_pack_rereads_a_finding_without_citations_through_the_store():
    f, graph, _ = load("LEAK-RATE")

    class Store:
        def get(self, tenant_id, finding_id):
            assert (tenant_id, finding_id) == (T, f["id"])
            return f

    row = {k: v for k, v in f.items() if k not in ("citations", "events")}
    out = xp.evidence_pack(T, row, graph=graph, findings=Store(), now=NOW)
    assert out["finding"]["citations"] == f["citations"]


def test_pack_html_escapes_script():
    f, graph, _ = load("LEAK-RATE")
    f["citations"][3]["excerpt"] = "<script>alert('x')</script> INR 50"
    f["summary"] = "<b>bold</b> summary"
    out = xp.evidence_pack(T, f, graph=graph, ratings=[RATING], now=NOW)
    html = render.pack_html(out)
    assert "<script>" not in html and "<b>" not in html
    assert "&lt;script&gt;" in html
    assert out["sha256"] in html and "Evidence pack" in html
    assert "Acme Steel" in html and "What happened" in html


# -- ratings --------------------------------------------------------------------

from invoice_processing.db import models  # noqa: E402
from invoice_processing.db.session import (  # noqa: E402
    create_all,
    get_engine,
    tenant_session,
)
from invoice_processing.ports import RunContext  # noqa: E402
from invoice_processing.trust.explain.ratings import RatingStore  # noqa: E402
from invoice_processing.trust.findings.model import Citation, FindingSpec  # noqa: E402
from invoice_processing.trust.findings.store import FindingStore  # noqa: E402
from invoice_processing.trust.graph.store import GraphStore  # noqa: E402

CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="engine")
OTHER = "tenant-globex"


@pytest.fixture
def db(scratch_db):
    url = scratch_db("explain.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    for tid, slug in ((T, "acme"), (OTHER, "globex")):
        with tenant_session(tid, url) as s:
            s.add(models.Tenant(id=tid, name=slug, slug=slug, status="active"))
    graph, findings = GraphStore(url), FindingStore(url)
    node = graph.upsert_node(CTX, kind="invoice", natural_key="i1")
    ids = {}
    for code in ("LEAK-RATE", "LEAK-RATE", "DUP-NEAR"):
        fid, _ = findings.upsert(
            CTX,
            FindingSpec(
                type=code,
                transaction_node=node,
                dedupe_key=f"{code}:{len(ids)}",
                impact=Decimal("10"),
                currency="INR",
                confidence=0.9,
                engine_version="e",
                rule_version="r",
                model_version="none",
                summary="s",
                citations=(Citation(node, "x"),),
            ),
        )
        ids[fid] = code
    return url, RatingStore(url), ids


def test_rating_upserts_per_rater_and_audits(db):
    url, store, ids = db
    fid = next(iter(ids))
    who = RunContext(tenant_id=T, run_id="req", actor="clerk@x.test")
    first = store.rate(who, fid, correct=True, useful=True, note="fine")
    assert first["rater"] == "clerk@x.test" and first["correct"] is True
    again = store.rate(who, fid, correct=False, useful=True, note=None)
    assert again["correct"] is False and again["note"] is None
    assert len(store.list(T, fid)) == 1
    store.rate(who.child(actor="boss@x.test"), fid, correct=True, useful=False)
    assert [r["rater"] for r in store.list(T, fid)] == [
        "boss@x.test",
        "clerk@x.test",
    ]
    with tenant_session(T, url) as s:
        rows = s.query(models.AuditEvent).filter_by(
            action="trust_explanation_rated"
        )
        assert rows.count() == 3
        assert {r.entity_id for r in rows} == {fid}
        assert rows.first().after["correct"] is True
    with pytest.raises(KeyError):
        store.rate(who, "nope", correct=True, useful=True)
    with pytest.raises(KeyError):
        store.rate(who.child(tenant_id=OTHER), fid, correct=True, useful=True)
    assert store.list(OTHER, fid) == []


def test_ratings_aggregate_by_type(db):
    _, store, ids = db
    who = RunContext(tenant_id=T, run_id="req", actor="clerk@x.test")
    for i, (fid, _code) in enumerate(ids.items()):
        store.rate(who, fid, correct=i != 1, useful=True)
        store.rate(who.child(actor="b@x.test"), fid, correct=True, useful=False)
    assert store.aggregate(T) == {
        "LEAK-RATE": {"rated": 4, "correct": 3, "useful": 2},
        "DUP-NEAR": {"rated": 2, "correct": 2, "useful": 1},
    }
    assert store.aggregate(OTHER) == {}
