"""Mandate scope validation, signing and verification (FR-28.5, B4, B16)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fresh_schema import fresh_schema
from sqlalchemy import update

from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.agents import mandates
from invoice_processing.trust.agents.store import AgentStore
from invoice_processing.trust.graph.store import GraphStore

T = "tenant-acme"
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
ADMIN_A = RunContext(tenant_id=T, case_id="", run_id="", actor="a@acme.test")
ADMIN_B = RunContext(tenant_id=T, case_id="", run_id="", actor="b@acme.test")


def body(**over):
    scope = {
        "actions": ["invoice.approve"],
        "counterparties": ["Acme Steel Pvt. Ltd."],
        "caps": {
            "per_action": "50000",
            "per_period": {"period": "month", "amount": "100000.00"},
            "per_vendor": "80000",
            "per_category": {"goods": "60000"},
        },
        "currency": "inr",
    }
    scope.update(over.pop("scope", {}))
    return {
        "scope": scope,
        "valid_from": over.pop("valid_from", NOW),
        "valid_to": over.pop("valid_to", NOW + timedelta(days=30)),
    }


def test_a_valid_scope_is_normalised():
    scope, start, end = mandates.validate(body(), now=NOW)
    assert scope["actions"] == ["invoice.approve"]
    assert scope["counterparties"] == ["ACME STEEL PVT"]
    assert scope["currency"] == "INR"
    assert scope["caps"]["per_period"] == {
        "period": "month",
        "amount": "100000.00",
    }
    assert scope["caps"]["per_category"] == {"goods": "60000"}
    assert (start, end) == (NOW, NOW + timedelta(days=30))


def test_naive_times_are_utc():
    _, start, _ = mandates.validate(
        body(valid_from=NOW.replace(tzinfo=None)), now=NOW
    )
    assert start == NOW and start.tzinfo is not None


def test_no_counterparties_means_any():
    scope, _, _ = mandates.validate(
        body(scope={"counterparties": None}), now=NOW
    )
    assert scope["counterparties"] is None


@pytest.mark.parametrize(
    ("over", "problem"),
    [
        ({"scope": {"actions": []}}, "actions"),
        ({"scope": {"actions": ["invoice.pay"]}}, "actions"),
        ({"scope": {"actions": "invoice.approve"}}, "actions"),
        ({"scope": {"currency": None}}, "currency"),
        ({"scope": {"currency": "rupees"}}, "currency"),
        ({"scope": {"caps": {"per_day": "1"}}}, "caps"),
        ({"scope": {"caps": {"per_action": "-5"}}}, "per_action"),
        ({"scope": {"caps": {"per_action": "1,000"}}}, "per_action"),
        ({"scope": {"caps": {"per_action": "NaN"}}}, "per_action"),
        ({"scope": {"caps": {"per_action": 10.5}}}, "per_action"),
        (
            {
                "scope": {
                    "caps": {"per_period": {"period": "week", "amount": "1"}}
                }
            },
            "period",
        ),
        ({"scope": {"caps": {"per_period": "100"}}}, "per_period"),
        ({"scope": {"caps": {"per_category": {"goods": "x"}}}}, "goods"),
        ({"scope": {"counterparties": ["  "]}}, "counterpart"),
        ({"scope": {"counterparties": "acme"}}, "counterpart"),
        ({"valid_to": NOW}, "valid_to"),
        ({"valid_to": NOW + timedelta(days=367)}, "366"),
    ],
)
def test_each_bad_scope_is_refused_with_its_problem(over, problem):
    with pytest.raises(ValueError, match=problem):
        mandates.validate(body(**over), now=NOW)


def test_a_signature_is_per_tenant(monkeypatch):
    monkeypatch.setenv("TRUST_MANDATE_KEY", "k1")
    a = mandates.sign(T, "d" * 64)
    assert a == mandates.sign(T, "d" * 64) and len(a) == 64
    assert a != mandates.sign("tenant-globex", "d" * 64)
    monkeypatch.setenv("TRUST_MANDATE_KEY", "k2")
    assert a != mandates.sign(T, "d" * 64)
    monkeypatch.delenv("TRUST_MANDATE_KEY")
    with pytest.raises(mandates.MandateKeyMissing):
        mandates.sign(T, "d" * 64)


@pytest.fixture
def env(scratch_db, monkeypatch):
    monkeypatch.setenv("TRUST_MANDATE_KEY", "test-key")
    url = scratch_db("mandates.db")
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    store = AgentStore(url, graph=GraphStore(url))
    agent = store.register(
        ADMIN_A, name="bot", owner="o@acme.test", kind="external"
    )
    return store, agent["id"], url


def propose(store, agent_id):
    scope, start, end = mandates.validate(body(), now=NOW)
    return store.propose_mandate(
        ADMIN_A, agent_id, scope=scope, valid_from=start, valid_to=end
    )


def edit(url, mandate_id, **cols):
    with tenant_session(T, url) as s:
        s.execute(
            update(models.AgentMandate)
            .where(models.AgentMandate.id == mandate_id)
            .values(**cols)
        )


def test_an_approved_mandate_verifies_and_chains(env):
    store, agent_id, _ = env
    v1 = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    assert v1["signature"] and mandates.verify(v1, None)
    v2 = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    rows = store.mandates(T, agent_id)
    assert [m["verified"] for m in mandates.annotate(rows, NOW)] == [
        True,
        True,
    ]
    assert mandates.verify(rows[1], rows[0])
    assert not mandates.verify(rows[1], None)  # chain broken
    assert v2["prev_digest"] == v1["digest"]


def test_a_proposed_mandate_verifies_on_its_digest_alone(env):
    store, agent_id, url = env
    m = propose(store, agent_id)
    assert mandates.verify(m, None)
    edit(url, m["id"], valid_to=NOW + timedelta(days=60))
    assert not mandates.verify(store.mandates(T, agent_id)[0], None)


@pytest.mark.parametrize(
    "cols",
    [
        {"scope": {"actions": ["invoice.export"], "currency": "INR"}},
        {"valid_to": NOW + timedelta(days=90)},
        {"approved_by": "mallory@acme.test"},
    ],
)
def test_editing_a_signed_column_in_the_db_breaks_verification(env, cols):
    store, agent_id, url = env
    m = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    edit(url, m["id"], **cols)
    [row] = store.mandates(T, agent_id)
    assert not mandates.verify(row, None)


def test_a_re_digested_edit_still_fails_on_the_signature(env):
    from invoice_processing.trust.agents.store import digest  # noqa: PLC0415

    store, agent_id, url = env
    m = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    forged = {**m, "valid_to": NOW + timedelta(days=90)}
    edit(url, m["id"], valid_to=forged["valid_to"], digest=digest(forged))
    [row] = store.mandates(T, agent_id)
    assert not mandates.verify(row, None)


def test_without_the_key_approval_changes_nothing(env, monkeypatch):
    store, agent_id, _ = env
    m = propose(store, agent_id)
    monkeypatch.delenv("TRUST_MANDATE_KEY")
    with pytest.raises(mandates.MandateKeyMissing):
        mandates.approve(ADMIN_B, store, m["id"])
    [row] = store.mandates(T, agent_id)
    assert row["status"] == "proposed" and row["approved_by"] is None


def test_an_unsigned_active_mandate_does_not_verify(env, monkeypatch):
    store, agent_id, _ = env
    m = propose(store, agent_id)
    store.approve_mandate(ADMIN_B, m["id"], signature=None)
    [row] = store.mandates(T, agent_id)
    assert row["status"] == "active" and not mandates.verify(row, None)
    monkeypatch.delenv("TRUST_MANDATE_KEY")
    assert not mandates.verify(row, None)


def test_expired_is_computed_on_read(env):
    store, agent_id, _ = env
    mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    rows = store.mandates(T, agent_id)
    assert mandates.annotate(rows, NOW)[0]["expired"] is False
    later = NOW + timedelta(days=31)
    assert mandates.annotate(rows, later)[0]["expired"] is True


def test_a_non_ascii_signature_in_the_db_reads_unverified(env):
    store, agent_id, url = env
    m = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    edit(url, m["id"], signature="é" * 64)
    [row] = store.mandates(T, agent_id)
    assert mandates.verify(row, None) is False
    edit(url, m["id"], digest="é" * 64)
    [row] = store.mandates(T, agent_id)
    assert mandates.verify(row, None) is False


def test_in_force_picks_the_version_that_held_at_a_time(env):
    store, agent_id, url = env
    v1 = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    v2 = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    # Both valid NOW..NOW+30d; v1 held until v2 replaced it on day 10.
    edit(url, v1["id"], approved_at=NOW)
    edit(url, v1["id"], revoked_at=NOW + timedelta(days=10))
    edit(url, v2["id"], approved_at=NOW + timedelta(days=10))
    rows = store.mandates(T, agent_id)
    at = mandates.in_force(rows, NOW + timedelta(days=5))
    assert at["id"] == v1["id"] and at["status"] == "active"
    assert mandates.in_force(rows, NOW + timedelta(days=20))["id"] == v2["id"]
    assert mandates.in_force(rows, NOW - timedelta(days=1)) is None
    assert mandates.in_force(rows, NOW + timedelta(days=31)) is None
