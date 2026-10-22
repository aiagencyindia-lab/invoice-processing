"""Behavioural detectors over the agent ledger (FR-28.4, Wave 4 branch C)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fresh_schema import fresh_schema

from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust import thresholds
from invoice_processing.trust.agents import behaviour
from invoice_processing.trust.agents.policy import Action
from invoice_processing.trust.agents.store import AgentStore
from invoice_processing.trust.graph.store import GraphStore

T = "tenant-acme"
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
ADMIN = RunContext(tenant_id=T, case_id="", run_id="", actor="admin@acme.test")
SETTINGS = thresholds.merged(None)
CAP = {"caps": {"per_action": "100000"}, "currency": "INR"}


@pytest.fixture
def store(scratch_db):
    url = scratch_db("agents_behaviour.db")
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    return AgentStore(url, graph=GraphStore(url))


def agent(store, name="bot", owner="ops@acme.test"):
    return store.register(
        ADMIN,
        name=name,
        owner=owner,
        principal_id=f"api-key:{name}",
        principal_email=f"{name}@api-key.{T}",
        kind="api_key",
    )


def ledger(store, a, action="invoice.approve", **cols):
    row = {
        "vendor_key": "acme steel",
        "decision": "ALLOW",
        "source": "inline",
        "occurred_at": NOW,
        "mode": "advisory",
        "reasons": [],
        **cols,
    }
    with tenant_session(T, store.url) as s:
        return store.record_action(
            s, ADMIN, agent_id=a["id"], action=action, **row
        )


def approve(amount="40000", at=None, currency="INR", vendor="acme steel"):
    return Action(
        "invoice.approve",
        "invoices",
        "inv-1",
        vendor_key=vendor,
        amount=Decimal(amount) if amount is not None else None,
        currency=currency,
        occurred_at=at,
    )


def run(store, rule, action, a, mandate=None, settings=SETTINGS, decider=None):
    return rule(action, a, mandate, store, settings, NOW, decider)


# -- circular ---------------------------------------------------------------


def test_circular_flags_approval_to_a_vendor_the_agent_created(store):
    a = agent(store)
    ledger(store, a, action="vendor.create")
    r = run(store, behaviour.circular, approve(), a)
    assert (r.rule, r.type, r.severity) == (
        "circular",
        "AGENT-CIRCULAR",
        "REVIEW",
    )
    assert "bot" in r.message


def test_circular_flags_a_vendor_created_by_a_same_owner_agent(store):
    maker = agent(store, "maker", owner="OPS@acme.test")
    payer = agent(store, "payer", owner="ops@acme.test")
    ledger(store, maker, action="vendor.create")
    r = run(store, behaviour.circular, approve(), payer)
    assert r is not None and "maker" in r.message


def test_circular_ignores_other_owners_and_non_approvals(store):
    maker = agent(store, "maker", owner="someone@acme.test")
    payer = agent(store, "payer")
    ledger(store, maker, action="vendor.create")
    assert run(store, behaviour.circular, approve(), payer) is None
    mine = agent(store, "mine")
    ledger(store, mine, action="vendor.create", vendor_key="other")
    reject = Action("invoice.reject", "invoices", "i", vendor_key="other")
    assert run(store, behaviour.circular, reject, mine) is None
    assert run(store, behaviour.circular, approve(vendor=None), mine) is None


def test_circular_ignores_dryrun_and_blocked_creations(store):
    a = agent(store)
    ledger(store, a, action="vendor.create", source="dryrun")
    ledger(store, a, action="vendor.create", decision="BLOCK")
    assert run(store, behaviour.circular, approve(), a) is None


# -- split ------------------------------------------------------------------


def test_split_flags_approvals_each_under_the_cap_summing_over_it(store):
    a = agent(store)
    ledger(
        store,
        a,
        amount=Decimal("40000"),
        currency="INR",
        occurred_at=NOW - timedelta(days=2),
    )
    ledger(
        store,
        a,
        amount=Decimal("40000"),
        currency="INR",
        occurred_at=NOW - timedelta(days=1),
    )
    r = run(store, behaviour.split, approve("40000"), a, {"scope": CAP})
    assert (r.rule, r.type, r.severity) == ("split", "AGENT-SPLIT", "REVIEW")
    assert "120000" in r.message and "100000" in r.message


def test_split_sum_equal_to_the_cap_is_not_over(store):
    a = agent(store)
    ledger(store, a, amount=Decimal("60000"), currency="INR")
    assert (
        run(store, behaviour.split, approve("40000"), a, {"scope": CAP}) is None
    )


def test_split_needs_every_part_under_the_cap(store):
    a = agent(store)
    ledger(store, a, amount=Decimal("100000"), currency="INR")
    assert (
        run(store, behaviour.split, approve("40000"), a, {"scope": CAP}) is None
    )
    assert (
        run(store, behaviour.split, approve("100000"), a, {"scope": CAP})
        is None
    )


def test_split_counts_only_this_window_vendor_currency_and_live_rows(store):
    a = agent(store)
    for cols in (
        {"occurred_at": NOW - timedelta(days=7, seconds=1)},
        {"decision": "BLOCK"},
        {"source": "dryrun"},
        {"currency": "USD"},
        {"vendor_key": "other"},
        {"amount": None},
    ):
        ledger(
            store, a, **{"amount": Decimal("70000"), "currency": "INR", **cols}
        )
    assert (
        run(store, behaviour.split, approve("40000"), a, {"scope": CAP}) is None
    )
    # The window's far edge is inside it.
    ledger(
        store,
        a,
        amount=Decimal("70000"),
        currency="INR",
        occurred_at=NOW - timedelta(days=7),
    )
    assert run(store, behaviour.split, approve("40000"), a, {"scope": CAP})


def test_split_threshold_falls_back_to_high_value_then_skips(store):
    a = agent(store)
    ledger(store, a, amount=Decimal("70000"), currency="INR")
    assert run(store, behaviour.split, approve("40000"), a) is None
    hv = {**SETTINGS, "high_value_threshold": 100000}
    assert run(store, behaviour.split, approve("40000"), a, None, hv)
    capped = {"scope": {"caps": {"per_action": {"amount": "100000"}}}}
    assert run(store, behaviour.split, approve("40000"), a, capped)


def test_split_measures_at_the_actions_own_time(store):
    a = agent(store)
    then = NOW - timedelta(days=30)
    ledger(store, a, amount=Decimal("70000"), currency="INR", occurred_at=then)
    assert (
        run(store, behaviour.split, approve("40000"), a, {"scope": CAP}) is None
    )
    logged = approve("40000", at=then + timedelta(hours=1))
    assert run(store, behaviour.split, logged, a, {"scope": CAP})


def test_split_skips_non_approvals_and_amountless_actions(store):
    a = agent(store)
    ledger(store, a, amount=Decimal("70000"), currency="INR")
    assert run(store, behaviour.split, approve(None), a, {"scope": CAP}) is None
    export = Action(
        "invoice.export",
        "invoices",
        "i",
        vendor_key="acme steel",
        amount=Decimal("40000"),
        currency="INR",
    )
    assert run(store, behaviour.split, export, a, {"scope": CAP}) is None


# -- velocity ---------------------------------------------------------------


def burst(store, a, n, at=NOW):
    for i in range(n):
        ledger(store, a, action="invoice.reject", occurred_at=at)


def test_velocity_flags_at_the_max(store):
    a = agent(store)
    burst(store, a, 19)
    assert run(store, behaviour.velocity, approve(), a) is None
    burst(store, a, 1)
    r = run(store, behaviour.velocity, approve(), a)
    assert (r.rule, r.type, r.severity) == (
        "velocity",
        "AGENT-VELOCITY",
        "REVIEW",
    )
    assert "20" in r.message and "10 minutes" in r.message


def test_velocity_window_edges(store):
    a = agent(store)
    burst(store, a, 19, at=NOW - timedelta(minutes=10, seconds=1))
    burst(store, a, 19, at=NOW - timedelta(minutes=10))
    burst(store, a, 1, at=NOW + timedelta(seconds=1))  # after the action
    assert run(store, behaviour.velocity, approve(), a) is None
    burst(store, a, 1, at=NOW)
    assert run(store, behaviour.velocity, approve(), a)


def test_velocity_ignores_dryruns_and_measures_a_log_row_at_its_time(store):
    a = agent(store)
    for _ in range(25):
        ledger(store, a, source="dryrun")
    assert run(store, behaviour.velocity, approve(), a) is None
    then = NOW - timedelta(days=3)
    burst(store, a, 20, at=then)
    assert run(store, behaviour.velocity, approve(), a) is None
    assert run(store, behaviour.velocity, approve(at=then), a)


def test_rules_are_the_four_detectors_in_order():
    assert [r.__name__ for r in behaviour.RULES] == [
        "circular",
        "split",
        "velocity",
        "injection",
    ]
