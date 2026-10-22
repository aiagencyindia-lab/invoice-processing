"""The agent-control routes (FR-28.1, FR-28.5, FR-28.6; Wave 4 branch A).

Every path here is in `PROBED`; test_tenant_isolation_api unions it into its
coverage check (and stops expecting the scaffold's 503 for it), and
`test_no_route_crosses_tenants` below probes each one with the other
tenant's bearer and the other tenant's API key.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from fresh_schema import fresh_schema
from sqlalchemy import select

from invoice_processing.adapters import JsonlAuditSink
from invoice_processing.api import auth, service, trust_agent_routes
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.tenant_settings import JsonTenantSettings
from invoice_processing.trust import apikeys
from invoice_processing.trust.agents import rules
from invoice_processing.trust.agents.policy import Reason
from invoice_processing.trust.agents.store import AgentStore
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.store import GraphStore

PROBED = {
    "/api/trust/agents",
    "/api/trust/agents/ledger",
    "/api/trust/agents/actions/import",
    "/api/trust/agents/{agent_id}",
    "/api/trust/agents/{agent_id}/revoke",
    "/api/trust/agents/{agent_id}/mandates",
    "/api/trust/agents/{agent_id}/mandates/{mandate_id}/approve",
    "/api/trust/agents/{agent_id}/mandates/{mandate_id}/revoke",
    "/api/trust/agents/{agent_id}/actions/evaluate",
    "/api/trust/agents/{agent_id}/ledger",
}

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
ROLES = ("viewer", "clerk", "approver", "admin")
SCOPE = {
    "actions": ["invoice.approve", "invoice.export"],
    "caps": {
        "per_action": "50000",
        "per_period": {"period": "month", "amount": "100000"},
    },
    "currency": "INR",
}


class FakeVerifier:
    """Token `<tenant>:<role>[:<n>]`; n tells two people of one role apart."""

    def verify(self, token):
        tenant, role, n = [*token.split(":"), "1"][:3]
        if tenant not in ("acme", "globex") or role not in ROLES:
            raise auth.AuthError("Invalid token")
        return {
            "sub": f"u-{token}",
            "email": f"{role}{n}@{tenant}.test",
            "email_verified": True,
            "tenant_id": f"tenant-{tenant}",
            "role": role,
        }


def bearer(token):
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def keyed(key):
    return TestClient(app, headers={"X-API-Key": key})


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("agent_routes.db")
    fresh_schema(url)
    graph, findings = GraphStore(url), FindingStore(url)
    monkeypatch.setattr(service, "_graph_store", graph)
    monkeypatch.setattr(service, "_finding_store", findings)
    monkeypatch.setattr(service, "_agent_store", AgentStore(url, graph=graph))
    monkeypatch.setattr(
        service, "_tenant_settings", JsonTenantSettings(tmp_path / "ts.json")
    )
    monkeypatch.setattr(
        service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    monkeypatch.setattr(trust_agent_routes, "_now", lambda: NOW)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("TRUST_MANDATE_KEY", "test-mandate-key")
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    out = {"url": url, "audit": tmp_path / "audit.jsonl"}
    for slug in ("acme", "globex"):
        tid = f"tenant-{slug}"
        with tenant_session(tid, url) as s:
            s.add(models.Tenant(id=tid, name=slug, slug=slug, status="active"))
            s.add(
                models.WebhookEndpoint(
                    tenant_id=tid,
                    url=f"https://{slug}.example/hook",
                    secret=f"whsec_{slug}secret",
                    events=["*"],
                )
            )
        ctx = RunContext(
            tenant_id=tid, case_id="", run_id="", actor=f"admin1@{slug}.test"
        )
        keys = {}
        for name, role in (("approver-key", "approver"), ("bot", "clerk")):
            row, key = apikeys.create(ctx, name, role, url=url)
            keys[name] = {"id": row["id"], "key": key}
        out[slug] = {"keys": keys}
    return out


def register(slug, key_id=None, name="payables-bot", **extra):
    body = {"name": name, "owner": f"ops@{slug}.test", **extra}
    if key_id:
        body["principal"] = {"api_key_id": key_id}
    r = bearer(f"{slug}:admin").post("/api/trust/agents", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def propose(slug, agent_id, scope=SCOPE, start=None, end=None, n=1):
    r = bearer(f"{slug}:admin:{n}").post(
        f"/api/trust/agents/{agent_id}/mandates",
        json={
            "scope": scope,
            "valid_from": (start or NOW - timedelta(days=21)).isoformat(),
            "valid_to": (end or NOW + timedelta(days=40)).isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def approve(slug, agent_id, mandate_id, n=2):
    return bearer(f"{slug}:admin:{n}").post(
        f"/api/trust/agents/{agent_id}/mandates/{mandate_id}/approve"
    )


def mandated(slug, key_id=None, **kw):
    agent = register(slug, key_id, **kw)
    m = propose(slug, agent["id"])
    r = approve(slug, agent["id"], m["id"])
    assert r.status_code == 200, r.text
    return agent, r.json()


def evaluate(client, agent_id, **body):
    body.setdefault("action", "invoice.approve")
    body.setdefault("subject_ref", "inv-1")
    body.setdefault("source", "log")
    if body["source"] == "log":
        body.setdefault("occurred_at", (NOW - timedelta(hours=1)).isoformat())
    return client.post(
        f"/api/trust/agents/{agent_id}/actions/evaluate", json=body
    )


def audit_events(env):
    lines = env["audit"].read_text().splitlines()
    return [json.loads(line)["event_type"] for line in lines]


def blocked_events(url, tid):
    with tenant_session(tid, url) as s:
        return s.scalars(
            select(models.WebhookOutbox).where(
                models.WebhookOutbox.tenant_id == tid,
                models.WebhookOutbox.event == "agent.action.blocked",
            )
        ).all()


def mandate_rule(action, agent, mandate, store, settings, now, decider):
    """A stand-in for branch B's `mandate_active` (rules.RULES is empty
    until it lands): no current mandate blocks."""
    if mandate is None:
        return Reason(
            "mandate_active",
            "AGENT-OUT-OF-MANDATE",
            "BLOCK",
            "The agent has no active mandate.",
        )
    return None


# -- registry ------------------------------------------------------------------


def test_register_binds_a_live_key_and_audits(env):
    key = env["acme"]["keys"]["bot"]
    agent = register("acme", key["id"], description="Approves freight.")
    assert agent["kind"] == "api_key"
    assert agent["principal_id"] == f"api-key:{key['id']}"
    assert agent["principal_email"] == "bot@api-key.tenant-acme"
    assert agent["owner"] == "ops@acme.test" and agent["status"] == "active"
    assert agent["graph_node"]
    assert "agent_registered" in audit_events(env)
    got = bearer("acme:approver").get(f"/api/trust/agents/{agent['id']}")
    assert got.status_code == 200 and got.json()["name"] == "payables-bot"
    assert got.json()["mandate"] is None


def test_register_a_user_principal_and_an_external_agent(env):
    user = register(
        "acme",
        name="svc",
        principal={"user_id": "firebase-uid-9", "email": "Svc@Acme.test"},
    )
    assert user["kind"] == "user" and user["principal_email"] == "svc@acme.test"
    external = register("acme", name="sap-joule")
    assert external["kind"] == "external" and external["principal_id"] is None


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"name": "x", "owner": "not-an-email"}, 400),
        ({"name": " ", "owner": "o@acme.test"}, 400),
        ({"name": "x", "owner": "o@acme.test", "principal": {}}, 400),
        (
            {
                "name": "x",
                "owner": "o@acme.test",
                "principal": {"user_id": "u1", "email": "nope"},
            },
            400,
        ),
        (
            {
                "name": "x",
                "owner": "o@acme.test",
                "principal": {"api_key_id": "no-such-key"},
            },
            404,
        ),
    ],
)
def test_register_refuses_bad_bodies(env, body, status):
    r = bearer("acme:admin").post("/api/trust/agents", json=body)
    assert r.status_code == status, r.text


def test_a_principal_binds_one_agent(env):
    key = env["acme"]["keys"]["bot"]
    register("acme", key["id"])
    r = bearer("acme:admin").post(
        "/api/trust/agents",
        json={
            "name": "again",
            "owner": "o@acme.test",
            "principal": {"api_key_id": key["id"]},
        },
    )
    assert r.status_code == 409
    # The same email under another user id is the same actor to the rules.
    register(
        "acme", name="u", principal={"user_id": "u1", "email": "s@acme.test"}
    )
    r = bearer("acme:admin").post(
        "/api/trust/agents",
        json={
            "name": "u2",
            "owner": "o@acme.test",
            "principal": {"user_id": "u2", "email": "S@acme.test"},
        },
    )
    assert r.status_code == 409


def test_a_key_sharing_its_name_with_another_live_key_is_refused(env):
    ctx = RunContext(
        tenant_id="tenant-acme", case_id="", run_id="", actor="admin1@acme.test"
    )
    twin, _ = apikeys.create(ctx, "BOT", "clerk", url=env["url"])
    r = bearer("acme:admin").post(
        "/api/trust/agents",
        json={
            "name": "x",
            "owner": "o@acme.test",
            "principal": {"api_key_id": twin["id"]},
        },
    )
    assert r.status_code == 409 and "rename" in r.json()["detail"].lower()


def test_a_revoked_key_cannot_be_registered(env):
    ctx = RunContext(
        tenant_id="tenant-acme", case_id="", run_id="", actor="admin1@acme.test"
    )
    key = env["acme"]["keys"]["bot"]
    apikeys.revoke(ctx, key["id"], url=env["url"])
    r = bearer("acme:admin").post(
        "/api/trust/agents",
        json={
            "name": "x",
            "owner": "o@acme.test",
            "principal": {"api_key_id": key["id"]},
        },
    )
    assert r.status_code == 404


def test_list_filters_by_status_and_revoke_revokes_the_mandate(env):
    agent, _m = mandated("acme")
    other = register("acme", name="aaa-bot")
    r = bearer("acme:admin").post(
        f"/api/trust/agents/{agent['id']}/revoke", json={"reason": "retired"}
    )
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert "agent_revoked" in audit_events(env)
    listed = bearer("acme:viewer").get("/api/trust/agents")
    assert listed.status_code == 403  # reads are approver
    listed = bearer("acme:approver").get("/api/trust/agents?status=active")
    assert [a["id"] for a in listed.json()["agents"]] == [other["id"]]
    assert (
        bearer("acme:approver").get("/api/trust/agents?status=gone").status_code
        == 400
    )
    [row] = (
        bearer("acme:approver")
        .get(f"/api/trust/agents/{agent['id']}/mandates")
        .json()["mandates"]
    )
    assert row["status"] == "revoked" and row["reason"] == "agent revoked"
    # A revoked agent takes no new mandate.
    r = bearer("acme:admin").post(
        f"/api/trust/agents/{agent['id']}/mandates",
        json={
            "scope": SCOPE,
            "valid_from": NOW.isoformat(),
            "valid_to": (NOW + timedelta(days=1)).isoformat(),
        },
    )
    assert r.status_code == 409


# -- mandates ------------------------------------------------------------------


def test_two_admins_grant_a_mandate_and_it_verifies(env):
    agent, m = mandated("acme")
    assert m["status"] == "active" and m["approved_by"] == "admin2@acme.test"
    assert m["proposed_by"] == "admin1@acme.test" and m["signature"]
    [row] = (
        bearer("acme:approver")
        .get(f"/api/trust/agents/{agent['id']}/mandates")
        .json()["mandates"]
    )
    assert row["verified"] is True and row["expired"] is False
    got = bearer("acme:approver").get(f"/api/trust/agents/{agent['id']}")
    assert got.json()["mandate"]["id"] == m["id"]
    events = audit_events(env)
    assert "agent_mandate_proposed" in events
    assert "agent_mandate_approved" in events


def test_the_proposer_cannot_approve(env):
    agent = register("acme")
    m = propose("acme", agent["id"])
    r = approve("acme", agent["id"], m["id"], n=1)
    assert r.status_code == 403


def test_approval_is_503_while_the_key_is_unset(env, monkeypatch):
    agent = register("acme")
    m = propose("acme", agent["id"])
    monkeypatch.delenv("TRUST_MANDATE_KEY")
    r = approve("acme", agent["id"], m["id"])
    assert r.status_code == 503
    assert r.json()["detail"] == "Mandate signing is not configured."
    [row] = (
        bearer("acme:approver")
        .get(f"/api/trust/agents/{agent['id']}/mandates")
        .json()["mandates"]
    )
    assert row["status"] == "proposed"


def test_one_pending_version_at_a_time_and_supersede(env):
    agent, v1 = mandated("acme")
    v2 = propose("acme", agent["id"])
    r = bearer("acme:admin").post(
        f"/api/trust/agents/{agent['id']}/mandates",
        json={
            "scope": SCOPE,
            "valid_from": NOW.isoformat(),
            "valid_to": (NOW + timedelta(days=1)).isoformat(),
        },
    )
    assert r.status_code == 409
    assert approve("acme", agent["id"], v1["id"]).status_code == 409
    assert approve("acme", agent["id"], v2["id"]).status_code == 200
    rows = (
        bearer("acme:approver")
        .get(f"/api/trust/agents/{agent['id']}/mandates")
        .json()["mandates"]
    )
    assert [(m["version"], m["status"]) for m in rows] == [
        (1, "revoked"),
        (2, "active"),
    ]
    assert all(m["verified"] for m in rows)


def test_a_bad_scope_is_400_with_the_problem(env):
    agent = register("acme")
    r = bearer("acme:admin").post(
        f"/api/trust/agents/{agent['id']}/mandates",
        json={
            "scope": {**SCOPE, "actions": ["invoice.pay"]},
            "valid_from": NOW.isoformat(),
            "valid_to": (NOW + timedelta(days=1)).isoformat(),
        },
    )
    assert r.status_code == 400 and "actions" in r.json()["detail"]


def test_naive_times_are_utc(env):
    agent = register("acme")
    r = bearer("acme:admin").post(
        f"/api/trust/agents/{agent['id']}/mandates",
        json={
            "scope": SCOPE,
            "valid_from": "2026-10-01T00:00:00",
            "valid_to": "2026-11-01T00:00:00",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["valid_from"].startswith("2026-10-01T00:00:00")
    r = bearer("acme:approver").get(
        f"/api/trust/agents/{agent['id']}/ledger"
        "?since=2026-10-01T00:00:00&until=2026-10-23T00:00:00"
    )
    assert r.status_code == 200, r.text


def test_an_expired_mandate_reads_expired_and_is_not_current(env):
    agent = register("acme")
    m = propose(
        "acme",
        agent["id"],
        start=NOW - timedelta(days=30),
        end=NOW - timedelta(days=1),
    )
    assert approve("acme", agent["id"], m["id"]).status_code == 200
    [row] = (
        bearer("acme:approver")
        .get(f"/api/trust/agents/{agent['id']}/mandates")
        .json()["mandates"]
    )
    assert row["expired"] is True
    got = bearer("acme:approver").get(f"/api/trust/agents/{agent['id']}")
    assert got.json()["mandate"] is None


def test_revoke_a_mandate(env):
    agent, m = mandated("acme")
    r = bearer("acme:admin").post(
        f"/api/trust/agents/{agent['id']}/mandates/{m['id']}/revoke",
        json={"reason": "scope too wide"},
    )
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert "agent_mandate_revoked" in audit_events(env)
    r = bearer("acme:admin").post(
        f"/api/trust/agents/{agent['id']}/mandates/{m['id']}/revoke", json={}
    )
    assert r.status_code == 422  # a reason is required


def test_a_mandate_under_another_agent_is_404(env):
    _agent, m = mandated("acme")
    other = register("acme", name="other")
    base = f"/api/trust/agents/{other['id']}/mandates/{m['id']}"
    admin = bearer("acme:admin:2")
    assert admin.post(f"{base}/approve").status_code == 404
    r = admin.post(f"{base}/revoke", json={"reason": "x"})
    assert r.status_code == 404


# -- evaluate ------------------------------------------------------------------


def test_a_proposed_mandate_evaluates_to_block(env, monkeypatch):
    monkeypatch.setattr(rules, "RULES", (mandate_rule,))
    agent = register("acme")
    propose("acme", agent["id"])
    r = evaluate(bearer("acme:approver"), agent["id"], source="dryrun")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "BLOCK"
    assert body["reasons"][0]["type"] == "AGENT-OUT-OF-MANDATE"
    assert body["mode"] == "off" and body["source"] == "dryrun"


def test_dryrun_never_refuses_counts_or_raises_a_finding(env, monkeypatch):
    monkeypatch.setattr(rules, "RULES", (mandate_rule,))
    key = env["acme"]["keys"]["bot"]
    agent = register("acme", key["id"])
    r = evaluate(
        keyed(key["key"]), agent["id"], source="dryrun", amount="99999"
    )
    assert r.status_code == 200 and r.json()["decision"] == "BLOCK"
    store = service._agent_store
    [row] = store.actions("tenant-acme", agent_id=agent["id"])
    assert row["source"] == "dryrun" and row["finding_id"] is None
    assert not blocked_events(env["url"], "tenant-acme")


def test_a_log_block_raises_a_finding_and_no_blocked_event(env, monkeypatch):
    monkeypatch.setattr(rules, "RULES", (mandate_rule,))
    agent = register("acme")
    at = NOW - timedelta(days=2)
    r = evaluate(
        bearer("acme:approver"),
        agent["id"],
        occurred_at=at.isoformat(),
        vendor="Acme Steel Pvt Ltd",
        amount="1200.50",
        currency="inr",
    )
    assert r.status_code == 200 and r.json()["decision"] == "BLOCK"
    [row] = service._agent_store.actions("tenant-acme", agent_id=agent["id"])
    assert row["source"] == "log" and row["finding_id"]
    assert row["occurred_at"] == at and row["vendor_key"] == "ACME STEEL PVT"
    assert row["currency"] == "INR" and str(row["amount"]).startswith("1200.5")
    assert not blocked_events(env["url"], "tenant-acme")


def test_a_log_row_needs_its_time(env):
    agent = register("acme")
    r = bearer("acme:approver").post(
        f"/api/trust/agents/{agent['id']}/actions/evaluate",
        json={"action": "invoice.approve", "subject_ref": "i", "source": "log"},
    )
    assert r.status_code == 400 and "occurred_at" in r.json()["detail"]


@pytest.mark.parametrize(
    "over",
    [
        {"amount": "1,000"},
        {"amount": "-5"},
        {"amount": "NaN"},
        {"amount": 10.5},
        {"action": "invoice.pay"},
        {"source": "inline"},
    ],
)
def test_bad_evaluate_bodies_are_refused(env, over):
    agent = register("acme")
    r = evaluate(bearer("acme:approver"), agent["id"], **over)
    assert r.status_code in (400, 422), r.text


def test_an_agent_key_may_evaluate_only_itself(env):
    key = env["acme"]["keys"]["bot"]
    own = register("acme", key["id"])
    other = register("acme", name="other")
    client = keyed(key["key"])  # a clerk key
    assert evaluate(client, own["id"], source="dryrun").status_code == 200
    assert evaluate(client, other["id"], source="dryrun").status_code == 403
    assert evaluate(bearer("acme:clerk"), own["id"]).status_code == 403


# -- ledger --------------------------------------------------------------------


def test_the_ledger_sums_by_vendor_category_and_period(env):
    agent, m = mandated("acme")
    client = bearer("acme:approver")
    for body in (
        {"vendor": "Acme Steel", "category": "goods", "amount": "30000"},
        {"vendor": "Beta Ltd", "category": "services", "amount": "20000.50"},
        {"vendor": "Acme Steel", "amount": "30000", "action": "invoice.export"},
        {"vendor": "Acme Steel", "amount": "10", "currency": "USD"},
        {
            "vendor": "Acme Steel",
            "amount": "99999",
            "source": "dryrun",
            "occurred_at": (NOW - timedelta(hours=1)).isoformat(),
        },
        # Last month: outside the default window and the period.
        {
            "vendor": "Acme Steel",
            "amount": "5",
            "occurred_at": "2026-09-30T23:00:00+00:00",
        },
    ):
        body.setdefault("currency", "INR")
        r = evaluate(client, agent["id"], **body)
        assert r.status_code == 200, r.text
    r = client.get(f"/api/trust/agents/{agent['id']}/ledger")
    assert r.status_code == 200, r.text
    led = r.json()
    assert led["since"].startswith("2026-10-01T00:00:00")
    assert led["mandate"]["id"] == m["id"] and led["mandate"]["verified"]
    inr = led["spend"]["INR"]
    assert inr["approved"] == "50000.5" and inr["exported"] == "30000"
    assert inr["by_vendor"] == {"ACME STEEL": "30000", "BETA": "20000.5"}
    assert inr["by_category"] == {"goods": "30000", "services": "20000.5"}
    assert led["spend"]["USD"]["approved"] == "10"
    assert led["period"] == {
        "period": "month",
        "currency": "INR",
        "cap": "100000",
        "used": "50000.5",
        "remaining": "49999.5",
        "since": "2026-10-01T00:00:00+00:00",
        "until": "2026-11-01T00:00:00+00:00",
    }
    assert len(led["actions"]) == 5  # the dryrun too, labelled
    assert led["exceptions"] == []
    wide = client.get(
        f"/api/trust/agents/{agent['id']}/ledger?since=2026-09-01T00:00:00Z"
    ).json()
    assert wide["spend"]["INR"]["approved"] == "50005.5"
    bad = client.get(
        f"/api/trust/agents/{agent['id']}/ledger?since=2026-10-22T00:00:00Z"
        "&until=2026-10-01T00:00:00Z"
    )
    assert bad.status_code == 400


def test_the_tenant_ledger_groups_by_owner(env, monkeypatch):
    a, _ = mandated("acme")
    b = register("acme", name="b", owner="finance@acme.test")
    client = bearer("acme:approver")
    evaluate(client, a["id"], vendor="V", amount="100", currency="INR")
    monkeypatch.setattr(rules, "RULES", (mandate_rule,))
    evaluate(client, b["id"], vendor="V", amount="7", currency="INR")
    r = client.get("/api/trust/agents/ledger")
    assert r.status_code == 200, r.text
    owners = {o["owner"]: o for o in r.json()["owners"]}
    assert owners["ops@acme.test"]["approved"] == {"INR": "100"}
    assert owners["ops@acme.test"]["agents"] == 1
    assert owners["finance@acme.test"]["approved"] == {}
    assert owners["finance@acme.test"]["blocked"] == 1


# -- import --------------------------------------------------------------------

CSV_HEAD = (
    "agent,action,subject_ref,vendor,category,amount,currency,occurred_at\n"
)


def upload(client, text, name="log.csv"):
    return client.post(
        "/api/trust/agents/actions/import",
        files={"file": (name, text.encode(), "text/csv")},
    )


def test_import_evaluates_each_row_and_reports_bad_ones(env):
    key = env["acme"]["keys"]["bot"]
    agent, _ = mandated("acme", key["id"])
    register("acme", name="twin")
    register("acme", name="twin")
    at = "2026-10-21T10:00:00Z"
    text = CSV_HEAD + "\n".join(
        [
            f"payables-bot,invoice.approve,inv-1,Acme Steel,goods,100,INR,{at}",
            f"bot@api-key.tenant-acme,invoice.export,inv-1,Acme Steel,,100,INR,{at}",
            f"nobody,invoice.approve,inv-2,Acme,,1,INR,{at}",
            f"twin,invoice.approve,inv-3,Acme,,1,INR,{at}",
            f'payables-bot,invoice.approve,inv-4,Acme,,"1,000",INR,{at}',
            f"payables-bot,invoice.pay,inv-5,Acme,,1,INR,{at}",
            "payables-bot,invoice.approve,inv-6,Acme,,1,INR,",
            "payables-bot,invoice.approve,inv-7,Acme,,1,INR,yesterday",
        ]
    )
    r = upload(bearer("acme:admin"), text)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["rows"] == 8 and out["by_decision"] == {"ALLOW": 2}
    assert [e["row"] for e in out["errors"]] == [4, 5, 6, 7, 8, 9]
    assert all(e["message"] for e in out["errors"])
    rows = service._agent_store.actions("tenant-acme", agent_id=agent["id"])
    assert {r["source"] for r in rows} == {"log"} and len(rows) == 2


def test_import_refuses_other_formats_and_huge_files(env):
    admin = bearer("acme:admin")
    assert upload(admin, "{}", name="log.json").status_code == 400
    big = CSV_HEAD + "x,invoice.approve,i,v,,1,INR,2026-10-01\n" * 5001
    assert upload(admin, big).status_code == 400


# -- isolation -----------------------------------------------------------------


ADMIN_ROUTES = [
    ("post", "/api/trust/agents"),
    ("post", "/api/trust/agents/actions/import"),
    ("post", "/api/trust/agents/{a}/revoke"),
    ("post", "/api/trust/agents/{a}/mandates"),
    ("post", "/api/trust/agents/{a}/mandates/{m}/approve"),
    ("post", "/api/trust/agents/{a}/mandates/{m}/revoke"),
]


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
def test_an_api_key_cannot_administer_agents(env, method, path):
    agent, m = mandated("acme")
    path = path.format(a=agent["id"], m=m["id"])
    r = getattr(keyed(env["acme"]["keys"]["approver-key"]["key"]), method)(
        path, json={}
    )
    assert r.status_code == 403
    assert (
        getattr(bearer("acme:approver"), method)(path, json={}).status_code
        == 403
    )


def test_no_route_crosses_tenants(env):
    """Acme's agent, mandate and key, asked for by globex's admin bearer and
    by globex's approver key: nothing found, nothing changed."""
    key = env["acme"]["keys"]["bot"]
    agent, m = mandated("acme", key["id"])
    evaluate(bearer("acme:approver"), agent["id"], amount="10", currency="INR")
    a, mid = agent["id"], m["id"]
    gkey = env["globex"]["keys"]["approver-key"]["key"]
    for client, admin_status in (
        (bearer("globex:admin"), 404),
        (keyed(gkey), 403),  # a key is refused before any lookup
    ):
        assert client.get("/api/trust/agents").json()["agents"] == []
        assert client.get("/api/trust/agents/ledger").json()["owners"] == []
        for path in (
            f"/api/trust/agents/{a}",
            f"/api/trust/agents/{a}/mandates",
            f"/api/trust/agents/{a}/ledger",
        ):
            assert client.get(path).status_code == 404, path
        assert evaluate(client, a, source="dryrun").status_code == 404
        for path, body in (
            (f"/api/trust/agents/{a}/revoke", {"reason": "x"}),
            (
                f"/api/trust/agents/{a}/mandates",
                {
                    "scope": SCOPE,
                    "valid_from": NOW.isoformat(),
                    "valid_to": (NOW + timedelta(days=1)).isoformat(),
                },
            ),
            (f"/api/trust/agents/{a}/mandates/{mid}/approve", None),
            (f"/api/trust/agents/{a}/mandates/{mid}/revoke", {"reason": "x"}),
        ):
            r = client.post(path, json=body)
            assert r.status_code == admin_status, (path, r.status_code)
        r = client.post(
            "/api/trust/agents",
            json={
                "name": "steal",
                "owner": "o@globex.test",
                "principal": {"api_key_id": key["id"]},
            },
        )
        assert r.status_code == admin_status
        r = upload(
            client,
            CSV_HEAD
            + "payables-bot,invoice.approve,i,V,,1,INR,2026-10-21T00:00:00Z\n",
        )
        if admin_status == 404:
            assert r.status_code == 200 and r.json()["by_decision"] == {}
            assert len(r.json()["errors"]) == 1
        else:
            assert r.status_code == 403
    # Acme is as it was.
    got = bearer("acme:approver").get(f"/api/trust/agents/{a}").json()
    assert got["status"] == "active" and got["mandate"]["id"] == mid
    led = bearer("acme:approver").get(f"/api/trust/agents/{a}/ledger").json()
    assert len(led["actions"]) == 1
    assert service._agent_store.list("tenant-globex") == []
