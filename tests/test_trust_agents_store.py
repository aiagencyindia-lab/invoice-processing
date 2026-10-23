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


def test_the_agent_lock_is_a_plain_for_update_on_postgres():
    """The guard records its decision in the session holding the lock (Wave
    5 B6), so the ledger insert's FK check runs on the same connection and a
    plain FOR UPDATE serialises one agent's check-then-record."""
    from sqlalchemy.dialects import postgresql  # noqa: PLC0415

    sql = str(
        store_mod._lock_query(T, "a1").compile(dialect=postgresql.dialect())
    )
    assert "FOR UPDATE" in sql and "NO KEY" not in sql


def test_a_non_utc_validity_keeps_its_digest_after_a_reload(env):
    from datetime import timezone  # noqa: PLC0415

    store, _, _ = env
    a = agent(store)
    ist = timezone(timedelta(hours=5, minutes=30))
    m = store.propose_mandate(
        ADMIN_A,
        a["id"],
        scope=SCOPE,
        valid_from=NOW.astimezone(ist),
        valid_to=(NOW + timedelta(days=5)).astimezone(ist),
    )
    (reloaded,) = store.mandates(T, a["id"])
    assert store_mod.digest(reloaded) == m["digest"] == reloaded["digest"]


def test_revoke_reports_whether_it_changed_the_mandate(env):
    store, _, _ = env
    a = agent(store)
    m = active(store, a["id"])
    first = store.revoke_mandate(ADMIN_A, m["id"], "done")
    again = store.revoke_mandate(ADMIN_A, m["id"], "done")
    assert first["changed"] is True and again["changed"] is False


def test_a_failed_approval_spends_nothing(env):
    """An approval that failed after the check (e.g. the invoice answered
    409) is stamped failed and leaves the cap alone; an unstamped one still
    counts, so caps stay conservative."""
    store, _, url = env
    a = agent(store)["id"]
    ok = record(store, url, a, amount=Decimal("100"))
    failed = record(store, url, a, amount=Decimal("40"))
    record(store, url, a, amount=Decimal("7"))  # unstamped: counts
    store.set_outcome(T, ok, "executed")
    store.set_outcome(T, failed, "failed")
    window = {
        "since": NOW - timedelta(days=1),
        "until": NOW + timedelta(days=1),
    }
    assert store.spent(T, a, currency="INR", **window) == Decimal("107")
    rows = {r["id"]: r["outcome"] for r in store.actions(T, agent_id=a)}
    assert rows[ok] == "executed" and rows[failed] == "failed"


def test_set_outcome_refuses_an_unknown_outcome(env):
    store, _, url = env
    a = agent(store)["id"]
    row = record(store, url, a, amount=Decimal("1"))
    with pytest.raises(ValueError):
        store.set_outcome(T, row, "maybe")


def test_count_since_counts_every_row_whatever_its_request_id(env):
    """A caller may send its own X-Request-ID, and a log import carries one
    id for every row: velocity must not take the caller's word for what
    one burst is."""
    store, _, url = env
    a = agent(store)["id"]
    for _ in range(5):
        record(store, url, a, action="vendor.create", request_id="req-bulk")
    record(store, url, a)
    since, until = NOW - timedelta(minutes=5), NOW + timedelta(seconds=1)
    assert store.count_since(T, a, since=since, until=until) == 6


def _record_held(store, url, agent_id):
    from invoice_processing.trust.agents import record as record_mod  # noqa: PLC0415
    from invoice_processing.trust.agents.policy import (  # noqa: PLC0415
        Action,
        Decision,
        Reason,
    )
    from invoice_processing.trust.findings.store import FindingStore  # noqa: PLC0415

    why = Reason("in_scope", "AGENT-OUT-OF-MANDATE", "BLOCK", "Out of scope.")
    return record_mod.record(
        ADMIN_A,
        Decision("BLOCK", (why,), why.message),
        Action(
            kind="invoice.approve",
            subject_table="invoices",
            subject_ref="invoices:case_001",
            occurred_at=NOW,
        ),
        agent=store.get(T, agent_id),
        mandate=None,
        store=store,
        findings=FindingStore(url),
        graph=store.graph,
        mode="enforcing",
        source="log",
        refused=False,
        now=NOW,
        hold=True,
    )


def test_count_blocked_tells_stopped_from_review(env):
    store, _, url = env
    a = agent(store)["id"]
    since = NOW - timedelta(hours=24)
    assert store.count_blocked(T, since) == {"blocked": 0, "reviews": 0}
    enforcing = {"decision": "BLOCK", "mode": "enforcing"}
    record(store, url, a, **enforcing)  # refused inline
    # Held: a log BLOCK whose finding placed an open hold (branch B's path).
    held = _record_held(store, url, a)
    record(
        store, url, a, **enforcing, source="log",
        subject_table="invoices", transaction_node=None,
    )  # the same decision, but no hold was placed: a review  # fmt: skip
    record(
        store, url, a, **enforcing, source="log", subject_table="vendors"
    )  # nothing held: a review
    record(store, url, a, decision="BLOCK", mode="advisory")  # review
    record(store, url, a, decision="ALLOW-WITH-REVIEW", mode="enforcing")
    record(store, url, a, **enforcing, source="dryrun")  # never counted
    record(store, url, a, **enforcing, occurred_at=NOW - timedelta(days=2))
    record(store, url, a)  # ALLOW
    assert held
    assert store.count_blocked(T, since) == {"blocked": 2, "reviews": 4}


def test_purge_before_keeps_vendor_creates_and_counts(env):
    store, _, url = env
    a = agent(store)["id"]
    old = NOW - timedelta(days=500)
    record(store, url, a, occurred_at=old)
    record(store, url, a, occurred_at=old, action="vendor.create")
    record(store, url, a, occurred_at=NOW - timedelta(days=10))
    assert store.purge_before(T, NOW - timedelta(days=400)) == 1
    kinds = sorted(r["action"] for r in store.actions(T, agent_id=a))
    assert kinds == ["invoice.approve", "vendor.create"]
    assert store.purge_before(T, NOW - timedelta(days=400)) == 0
