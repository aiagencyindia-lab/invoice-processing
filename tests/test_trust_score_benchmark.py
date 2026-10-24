"""benchmark/history/score.py: score coverage and hold consistency."""

from decimal import Decimal

from fresh_schema import fresh_schema

from benchmark.history import score as bench
from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.findings.model import Citation, FindingSpec
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.store import GraphStore

T = "tenant-bench"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="engine")


def test_measure(scratch_db):
    url = scratch_db("bench_score.db")
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Bench", slug="bench", status="active"))
    g, store = GraphStore(url), FindingStore(url)
    a = g.upsert_node(CTX, kind="invoice", natural_key="invoices:a")
    g.upsert_node(CTX, kind="invoice", natural_key="invoices:b")
    out = bench.measure(T, g, store, {}, url)
    assert (
        out["score_coverage"] == 0.0 and out["gates"]["score_coverage"] is False
    )
    assert out["dimension_hold_consistency"] == 1.0

    def scored(key, identity):
        g.upsert_node(
            CTX,
            kind="trust_score",
            natural_key=f"trust_score:invoices:{key}",
            attrs={"dimensions": {"identity": {"value": identity}}},
        )

    scored("a", 1.0)
    scored("b", 1.0)
    store.upsert(
        CTX,
        FindingSpec(
            type="PAY-UNVERIFIED",
            transaction_node=a,
            dedupe_key="PAY-UNVERIFIED:a",
            impact=Decimal("1"),
            currency="INR",
            confidence=1.0,
            engine_version="p",
            rule_version="r",
            model_version="none",
            summary="s",
            citations=(Citation(a),),
        ),
    )
    out = bench.measure(T, g, store, {}, url)
    assert out["score_coverage"] == 1.0
    assert out["dimension_hold_consistency"] == 0.0
    assert out["gates"]["dimension_hold_consistency"] is False
    scored("a", 0.0)
    assert all(bench.measure(T, g, store, {}, url)["gates"].values())
