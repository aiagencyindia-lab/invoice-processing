"""The agent registry, mandate versions and the decision ledger (Wave 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fresh_schema import fresh_schema

from invoice_processing.db import models
from invoice_processing.db.repositories import SelfApproval
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.agents import store as store_mod
from invoice_processing.trust.agents.store import AgentStore, MandateConflict
from invoice_processing.trust.graph.store import GraphStore

T = "tenant-acme"
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
ADMIN_A = RunContext(
    tenant_id=T, case_id="", run_id="", actor="Admin-A@acme.test"
)
ADMIN_B = RunContext(
    tenant_id=T, case_id="", run_id="", actor="admin-b@acme.test"
)
SCOPE = {"actions": ["invoice.approve"], "currency": "INR"}


@pytest.fixture
def env(scratch_db):
    url = scratch_db("agents_store.db")
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    graph = GraphStore(url)
    return AgentStore(url, graph=graph), graph, url


def agent(store, name="bot", principal="api-key:1", owner="ops@acme.test"):
    return store.register(
        ADMIN_A,
        name=name,
        owner=owner,
        principal_id=principal,
        principal_email=f"{name}@api-key.{T}" if principal else None,
        kind="api_key" if principal else "external",
    )


def propose(store, agent_id, scope=SCOPE, days=30):
    return store.propose_mandate(
        ADMIN_A,
        agent_id,
        scope=scope,
        valid_from=NOW - timedelta(days=1),
        valid_to=NOW + timedelta(days=days),
    )


def active(store, agent_id):
    m = propose(store, agent_id)
    return store.approve_mandate(ADMIN_B, m["id"], signature="sig")


def test_register_and_look_up_by_principal_email_and_owner(env):
    store, graph, _ = env
    a = agent(store)
    assert store.by_principal(T, "api-key:1")["id"] == a["id"]
    assert (
        store.by_emails(T, ["BOT@api-key.tenant-acme"])[
            "bot@api-key.tenant-acme"
        ]["id"]
        == a["id"]
    )
    assert [x["id"] for x in store.by_owner(T, "OPS@acme.test")] == [a["id"]]
    node = graph.node(T, a["graph_node"])
    assert node["kind"] == "agent" and node["attrs"]["owner"] == "ops@acme.test"
    assert store.by_principal(T, "api-key:other") is None


def test_a_mandate_cannot_be_approved_by_its_proposer(env):
    store, _, _ = env
    m = propose(store, agent(store)["id"])
    same_person = RunContext(
        tenant_id=T, case_id="", run_id="", actor="admin-a@ACME.test"
    )
    with pytest.raises(SelfApproval):
        store.approve_mandate(same_person, m["id"], signature="sig")


def test_propose_while_one_is_pending_conflicts(env):
    store, _, _ = env
    a = agent(store)
    propose(store, a["id"])
    with pytest.raises(MandateConflict):
        propose(store, a["id"])


def test_only_a_pending_latest_version_can_be_approved(env):
    store, _, _ = env
    a = agent(store)
    v1 = active(store, a["id"])
    with pytest.raises(MandateConflict):  # already active
        store.approve_mandate(ADMIN_B, v1["id"], signature="sig")
    v2 = propose(store, a["id"])
    store.revoke_mandate(ADMIN_A, v2["id"], "withdrawn")
    with pytest.raises(MandateConflict):  # revoked, not pending
        store.approve_mandate(ADMIN_B, v2["id"], signature="sig")


def test_approve_supersedes_the_active_version_and_chains_digests(env):
    store, _, _ = env
    a = agent(store)
    v1 = active(store, a["id"])
    v2 = store.approve_mandate(
        ADMIN_B, propose(store, a["id"])["id"], signature="sig2"
    )
    versions = {m["version"]: m for m in store.mandates(T, a["id"])}
    assert versions[1]["status"] == "revoked"
    assert versions[1]["reason"] == "superseded"
    assert v2["status"] == "active" and v2["version"] == 2
    assert v2["prev_digest"] == v1["digest"]
    assert store_mod.digest(v2) == v2["digest"]
    assert (
        v2["approved_by"] == "admin-b@acme.test" and v2["signature"] == "sig2"
    )


def test_a_digest_changes_when_the_scope_does(env):
    store, _, _ = env
    m = active(store, agent(store)["id"])
    edited = {**m, "scope": {**m["scope"], "actions": ["invoice.export"]}}
    assert store_mod.digest(edited) != m["digest"]


def test_current_mandate_respects_the_validity_window(env):
    store, _, _ = env
    a = agent(store)
    m = active(store, a["id"])
    assert store.current_mandate(T, a["id"], NOW)["id"] == m["id"]
    assert store.current_mandate(T, a["id"], m["valid_to"]) is None
    assert (
        store.current_mandate(
            T, a["id"], m["valid_from"] - timedelta(seconds=1)
        )
        is None
    )
    assert (
        store.current_mandate(T, agent(store, "b2", "api-key:2")["id"], NOW)
        is None
    )


def record(store, url, agent_id, **cols):
    base = {
        "action": "invoice.approve",
        "decision": "ALLOW",
        "source": "inline",
        "occurred_at": NOW,
        "currency": "INR",
        "vendor_key": "acme supplies",
    }
    with tenant_session(T, url) as s:
        return store.record_action(
            s, ADMIN_A, agent_id=agent_id, **{**base, **cols}
        )


def test_spent_counts_only_non_blocked_non_dryrun_approvals_in_currency(env):
    store, _, url = env
    a = agent(store)["id"]
    record(store, url, a, amount=Decimal("100"))
    record(store, url, a, amount=Decimal("50"), decision="BLOCK")
    record(store, url, a, amount=Decimal("70"), source="dryrun")
    record(store, url, a, amount=Decimal("100"), action="invoice.export")
    record(store, url, a, amount=Decimal("10"), currency="USD")
    record(
        store,
        url,
        a,
        amount=Decimal("5"),
        decision="ALLOW-WITH-REVIEW",
        source="log",
    )
    record(
        store, url, a, amount=Decimal("1"), occurred_at=NOW - timedelta(days=40)
    )
    since, until = NOW - timedelta(days=30), NOW + timedelta(seconds=1)
    assert store.spent(
        T, a, since=since, until=until, currency="INR"
    ) == Decimal("105")
    assert store.spent(
        T, a, since=since, until=until, currency="INR", vendor_key="other"
    ) == Decimal("0")


def test_count_since_and_creators_of_ignore_dryrun_and_blocks(env):
    store, _, url = env
    a = agent(store)
    b = agent(store, "b2", "api-key:2")
    record(store, url, a["id"], action="vendor.create", vendor_key="newco")
    record(
        store,
        url,
        b["id"],
        action="vendor.create",
        vendor_key="newco",
        source="dryrun",
    )
    record(
        store,
        url,
        b["id"],
        action="vendor.create",
        vendor_key="newco",
        decision="BLOCK",
    )
    assert [x["id"] for x in store.creators_of(T, "newco")] == [a["id"]]
    window = {
        "since": NOW - timedelta(minutes=1),
        "until": NOW + timedelta(minutes=1),
    }
    assert store.count_since(T, a["id"], **window) == 1
    assert (
        store.count_since(T, b["id"], **window) == 1
    )  # the BLOCK counts; the dryrun does not


def test_actions_are_newest_first_and_filterable(env):
    store, _, url = env
    a = agent(store)["id"]
    first = record(store, url, a, occurred_at=NOW - timedelta(minutes=5))
    second = record(store, url, a, decision="BLOCK")
    assert [r["id"] for r in store.actions(T, agent_id=a)] == [second, first]
    assert [
        r["id"] for r in store.actions(T, agent_id=a, decisions=["BLOCK"])
    ] == [second]
    row = store.actions(T, agent_id=a)[0]
    assert row["occurred_at"].tzinfo is not None


def test_revoking_an_agent_revokes_its_active_mandate(env):
    store, graph, _ = env
    a = agent(store)
    active(store, a["id"])
    out = store.revoke(ADMIN_A, a["id"], "retired")
    assert out["status"] == "revoked" and out["revoke_reason"] == "retired"
    assert store.current_mandate(T, a["id"], NOW) is None
    assert store.mandates(T, a["id"])[0]["reason"] == "agent revoked"
    assert (
        graph.open_node(T, "agent", f"agents:{a['id']}")["attrs"]["status"]
        == "revoked"
    )


def test_lock_agent_takes_the_row(env):
    store, _, url = env
    a = agent(store)["id"]
    with tenant_session(T, url) as s:
        store.lock_agent(s, T, a)  # no error on SQLite; FOR UPDATE on Postgres
