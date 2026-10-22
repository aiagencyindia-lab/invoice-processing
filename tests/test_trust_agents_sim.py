"""The simulated agent fleet that gates AC-28 (benchmark/history/agents.py).

The gate tests are written for the merged system: they pass once the policy
branch's structural rules and the behaviour branch's detectors are on main.
"""

from collections import Counter

import pytest
from fresh_schema import fresh_schema

from benchmark.history import agents as fleet
from benchmark.history import eval as history_eval
from benchmark.history import generate
from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.agents import rules
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.store import GraphStore

TENANT = "bench-agents"


def _bench(url, vendors=30, invoices=30):
    fresh_schema(url)
    with tenant_session(TENANT, url) as s:
        s.add(models.Tenant(id=TENANT, name="B", slug=TENANT, status="active"))
    graph = GraphStore(url)
    ctx = RunContext(tenant_id=TENANT, case_id="", run_id="r", actor="t")
    for i in range(vendors):
        graph.upsert_node(
            ctx,
            kind="vendor",
            natural_key=f"vendors:v{i}",
            ref_table="vendors",
            ref_id=f"v{i}",
            attrs={"name": f"Vendor {i:02d} Pvt Ltd"},
        )
    for i in range(invoices):
        graph.upsert_node(
            ctx,
            kind="invoice",
            natural_key=f"invoices:i{i}",
            ref_table="invoices",
            ref_id=f"i{i}",
            attrs={"invoice_number": f"INV-{i:03d}"},
        )
    return graph, FindingStore(url)


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("TRUST_MANDATE_KEY", "bench-test-key")


def _script():
    vendors = [f"Vendor {i:02d} Pvt Ltd" for i in range(30)]
    return fleet.script(vendors, [])


def test_the_script_labels_every_kind_at_least_five_times():
    steps = _script()
    labels = Counter(s.label for s in steps)
    assert set(labels) == {"compliant", "structural", "behavioural"}
    assert labels["compliant"] >= 40
    kinds = Counter(s.kind for s in steps if s.kind)
    assert set(kinds) == set(fleet.EXPECTED)
    assert min(kinds.values()) >= 5
    for s in steps:
        assert (s.kind is None) == (s.label == "compliant")
        assert s.kind is None or fleet.KIND_LABEL[s.kind] == s.label
    times = [s.action.occurred_at for s in steps]
    assert times == sorted(times)


def test_velocity_labels_only_actions_21_to_25():
    burst = [
        s
        for s in _script()
        if s.agent == "rogue-burst" and s.action.kind == "invoice.export"
    ]
    assert len(burst) == 25
    assert [s.kind for s in burst] == [None] * 20 + ["velocity"] * 5
    span = burst[-1].action.occurred_at - burst[0].action.occurred_at
    assert span.total_seconds() < 60


def test_split_labels_only_the_approval_over_the_line():
    by_vendor: dict = {}
    for s in _script():
        if s.agent == "rogue-burst" and s.kind in (None, "split"):
            if s.action.kind == "invoice.approve" and s.action.amount == 40000:
                by_vendor.setdefault(s.action.vendor_key, []).append(s.kind)
    assert len(by_vendor) == 5
    assert all(k == [None, None, "split"] for k in by_vendor.values())


def test_measure_meets_all_four_gates(scratch_db, key):
    url = scratch_db("fleet.db")
    graph, findings = _bench(url)
    out = fleet.measure(TENANT, graph, findings, {}, url)
    assert out["gates"] == {
        "agents_structural_blocked": True,
        "agents_behavioural_flagged": True,
        "agents_false_blocks": True,
        "agents_latency_60s": True,
    }, out
    for kind, row in out["by_kind"].items():
        assert row["caught"] == row["n"] >= 5, kind
    assert out["compliant"]["false_blocks"] == 0
    assert out["compliant"]["false_reviews"] == 0, out["compliant"]
    assert out["structural"]["blocked"] == out["structural"]["n"]


def test_a_dead_rule_fails_the_structural_gate(scratch_db, key, monkeypatch):
    """The harness can fail: a structural rule that never fires shows."""
    monkeypatch.setattr(
        rules,
        "RULES",
        tuple(
            (lambda *a: None) if r.__name__ == "self_approval" else r
            for r in rules.RULES
        ),
    )
    url = scratch_db("dead.db")
    graph, findings = _bench(url)
    out = fleet.measure(TENANT, graph, findings, {}, url)
    assert out["by_kind"]["self_approval"]["caught"] == 0
    assert out["gates"]["agents_structural_blocked"] is False


def test_the_fleet_runs_with_a_bare_graph(scratch_db, key):
    """No vendors or invoices yet: synthetic names, no transaction nodes."""
    url = scratch_db("bare.db")
    graph, findings = _bench(url, vendors=0, invoices=0)
    out = fleet.measure(TENANT, graph, findings, {}, url)
    assert out["structural"]["n"] > 0 and "gates" in out


def test_eval_reports_the_four_agent_gates(tmp_path, scratch_db, key):
    data = tmp_path / "d"
    generate.generate(7, data)
    out = history_eval.run(scratch_db("bench.db"), data)
    for gate in (
        "agents_structural_blocked",
        "agents_behavioural_flagged",
        "agents_false_blocks",
        "agents_latency_60s",
    ):
        assert out["gates"][gate] is True, (gate, out["agents"])
    assert out["passed"]
