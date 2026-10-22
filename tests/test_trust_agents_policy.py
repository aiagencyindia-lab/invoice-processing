"""The policy seam and the one writer of agent decisions (Wave 4 scaffold)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fresh_schema import fresh_schema
from sqlalchemy import select

from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.agents import (
    behaviour,
    policy,
    record,
    rules,
)
from invoice_processing.trust.agents.policy import Action, Decision, Reason
from invoice_processing.trust.agents.store import AgentStore
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.store import GraphStore

T = "tenant-acme"
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
CTX = RunContext(tenant_id=T, case_id="", run_id="r1", actor="bot@api-key.t")


def _rule(reason):
    return lambda *a, **k: reason


REVIEW = Reason("velocity", "AGENT-VELOCITY", "REVIEW", "It acted fast.")
BLOCK = Reason("self", "AGENT-SELF-APPROVAL", "BLOCK", "It approved its own.")
ACTION = Action(
    kind="invoice.approve",
    subject_table="invoices",
    subject_ref="invoices:case_001",
    vendor_key="acme supplies",
    amount=Decimal("100.00"),
    currency="INR",
)


def evaluate(mode="advisory"):
    return policy.evaluate_action(
        CTX,
        ACTION,
        agent={"id": "a1"},
        mandate=None,
        store=None,
        settings={},
        now=NOW,
        mode=mode,
    )


def test_with_no_rules_everything_is_allowed(monkeypatch):
    # The branches fill the rule tuples; this pins the composition alone.
    monkeypatch.setattr(rules, "RULES", ())
    monkeypatch.setattr(behaviour, "RULES", ())
    out = evaluate()
    assert (
        out.decision == "ALLOW" and out.reasons == () and out.explanation == ""
    )


def test_block_beats_review_and_every_rule_runs(monkeypatch):
    monkeypatch.setattr(rules, "RULES", (_rule(None), _rule(BLOCK)))
    monkeypatch.setattr(behaviour, "RULES", (_rule(REVIEW),))
    out = evaluate()
    assert out.decision == "BLOCK"
    assert out.reasons == (BLOCK, REVIEW)
    assert out.explanation == "It approved its own. It acted fast."


def test_review_alone_allows_with_review(monkeypatch):
    monkeypatch.setattr(behaviour, "RULES", (_rule(REVIEW),))
    assert evaluate().decision == "ALLOW-WITH-REVIEW"


@pytest.mark.parametrize(
    ("mode", "decision"),
    [("enforcing", "BLOCK"), ("advisory", "ALLOW-WITH-REVIEW")],
)
def test_a_rule_that_raises_blocks_only_in_enforcing(
    monkeypatch, mode, decision
):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(rules, "RULES", (boom,))
    out = evaluate(mode)
    assert out.decision == decision
    assert out.reasons[0].rule == "rule-failed"


# -- record ----------------------------------------------------------------


@pytest.fixture
def env(scratch_db):
    url = scratch_db("agents_record.db")
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
        s.add(
            models.WebhookEndpoint(
                tenant_id=T,
                url="https://hooks.example.test/t",
                secret="s",
                events=["*"],
            )
        )
    graph = GraphStore(url)
    store = AgentStore(url, graph=graph)
    agent = store.register(
        CTX,
        name="bot",
        owner="ops@acme.test",
        kind="api_key",
        principal_id="api-key:1",
        principal_email="bot@api-key.t",
    )
    return {
        "url": url,
        "graph": graph,
        "store": store,
        "findings": FindingStore(url),
        "agent": agent,
    }


def write(
    env,
    decision,
    *,
    source="inline",
    refused=False,
    action=ACTION,
    mode="advisory",
):
    return record.record(
        CTX,
        decision,
        action,
        agent=env["agent"],
        mandate=None,
        store=env["store"],
        findings=env["findings"],
        graph=env["graph"],
        mode=mode,
        source=source,
        refused=refused,
        now=NOW,
    )


def _rows(env, model):
    with tenant_session(T, env["url"]) as s:
        return s.scalars(select(model)).all()


def test_an_allow_writes_only_the_ledger_row(env):
    action_id = write(env, Decision("ALLOW", (), ""))
    (row,) = _rows(env, models.AgentAction)
    assert (
        row.id == action_id
        and row.decision == "ALLOW"
        and row.finding_id is None
    )
    assert row.amount == Decimal("100.00") and row.source == "inline"
    assert (
        _rows(env, models.TrustFinding) == []
        and _rows(env, models.WebhookOutbox) == []
    )


def test_a_shadow_block_raises_a_finding_but_no_blocked_event(env):
    write(env, Decision("BLOCK", (BLOCK, REVIEW), "x"), mode="shadow")
    (finding,) = _rows(env, models.TrustFinding)
    assert finding.type == "AGENT-SELF-APPROVAL"
    assert finding.details["engine"] == "agents"
    assert [r["type"] for r in finding.details["reasons"]] == [
        "AGENT-SELF-APPROVAL",
        "AGENT-VELOCITY",
    ]
    assert _rows(env, models.AgentAction)[0].finding_id == finding.id
    assert [o.event for o in _rows(env, models.WebhookOutbox)] == [
        "finding.created"
    ]


def test_a_refused_block_emits_agent_action_blocked(env):
    action_id = write(
        env,
        Decision("BLOCK", (BLOCK,), "It approved its own."),
        refused=True,
        mode="enforcing",
    )
    events = {o.event: o.payload for o in _rows(env, models.WebhookOutbox)}
    data = events["agent.action.blocked"]["data"]
    assert data["action_id"] == action_id and data["decision"] == "BLOCK"
    assert data["agent_name"] == "bot" and data["owner"] == "ops@acme.test"
    assert data["explanation"] == "It approved its own."


def test_a_dry_run_never_raises_a_finding(env):
    write(env, Decision("BLOCK", (BLOCK,), "x"), source="dryrun")
    assert _rows(env, models.TrustFinding) == []
    assert _rows(env, models.AgentAction)[0].source == "dryrun"


def test_without_an_invoice_node_the_finding_sits_on_the_agent_node(env):
    write(env, Decision("ALLOW-WITH-REVIEW", (REVIEW,), "x"))
    (finding,) = _rows(env, models.TrustFinding)
    assert finding.transaction_node == env["agent"]["graph_node"]


def test_a_known_invoice_node_carries_the_finding_and_an_acted_on_edge(env):
    ctx = RunContext(tenant_id=T, case_id="", run_id="", actor="sync")
    node = env["graph"].upsert_node(
        ctx, kind="invoice", natural_key="invoices:case_001"
    )
    action = Action(**{**ACTION.__dict__, "transaction_node": node})
    write(env, Decision("ALLOW-WITH-REVIEW", (REVIEW,), "x"), action=action)
    (finding,) = _rows(env, models.TrustFinding)
    assert finding.transaction_node == node
    edges = env["graph"].edges(
        T, src=env["agent"]["graph_node"], kind="acted_on"
    )
    assert [e["dst"] for e in edges] == [node]


def test_a_blocked_action_draws_no_acted_on_edge(env):
    ctx = RunContext(tenant_id=T, case_id="", run_id="", actor="sync")
    node = env["graph"].upsert_node(
        ctx, kind="invoice", natural_key="invoices:case_002"
    )
    action = Action(**{**ACTION.__dict__, "transaction_node": node})
    write(
        env,
        Decision("BLOCK", (BLOCK,), "x"),
        action=action,
        refused=True,
        mode="enforcing",
    )
    assert (
        env["graph"].edges(T, src=env["agent"]["graph_node"], kind="acted_on")
        == []
    )


def test_the_finding_is_typed_by_the_first_reason_of_the_decided_severity(
    env, monkeypatch
):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(behaviour, "RULES", (_rule(REVIEW), boom))
    decision = policy.evaluate_action(
        CTX,
        ACTION,
        agent=env["agent"],
        mandate=None,
        store=env["store"],
        settings={},
        now=NOW,
        mode="enforcing",
    )
    assert decision.decision == "BLOCK"
    write(env, decision, mode="enforcing")
    (finding,) = _rows(env, models.TrustFinding)
    assert finding.type == "AGENT-OUT-OF-MANDATE"  # the BLOCK, not the review


def test_recording_without_a_graph_node_is_refused_plainly(env):
    graphless = {**env["agent"], "graph_node": None}
    with pytest.raises(ValueError, match="graph"):
        record.record(
            CTX,
            Decision("BLOCK", (BLOCK,), "x"),
            ACTION,
            agent=graphless,
            mandate=None,
            store=env["store"],
            findings=env["findings"],
            graph=None,
            mode="advisory",
            source="inline",
            refused=False,
            now=NOW,
        )


def test_record_runs_while_the_guard_holds_the_agent_lock(env):
    """The guard's order (B17): lock the agent, read spend, record. On
    Postgres a plain FOR UPDATE would hang here for ever."""
    with tenant_session(T, env["url"]) as s:
        env["store"].lock_agent(s, T, env["agent"]["id"])
        write(
            env,
            Decision("BLOCK", (BLOCK,), "x"),
            refused=True,
            mode="enforcing",
        )
    assert len(_rows(env, models.AgentAction)) == 1
