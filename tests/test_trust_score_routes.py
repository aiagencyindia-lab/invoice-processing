"""Trust Score, vendor profile, posture, readiness and mode routes (FR-25)."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from fresh_schema import fresh_schema
from sqlalchemy import select

from invoice_processing.adapters import JsonlAuditSink
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.tenant_settings import PgTenantSettings
from invoice_processing.trust import holds as holds_mod
from invoice_processing.trust import score
from invoice_processing.trust.engine import EngineContext
from invoice_processing.trust.findings.model import Citation, FindingSpec
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore

# Every path this module adds; test_tenant_isolation_api unions it into its
# coverage check, and TestScoreIsolation exercises each one across tenants.
PROBED = {
    "/api/trust/transactions/{node_id}/trust",
    "/api/trust/vendors/{node_id}/trust",
    "/api/trust/posture",
    "/api/trust/readiness",
    "/api/trust/engines/{engine}/mode",
}

ACME, GLOBEX = "tenant-acme", "tenant-globex"
AT = datetime(2026, 10, 1, tzinfo=UTC)


class FakeVerifier:
    def verify(self, token):
        tenant, _, role = token.partition(":")
        if tenant not in ("acme", "globex") or role not in (
            "viewer",
            "clerk",
            "approver",
            "admin",
        ):
            raise auth.AuthError("Invalid token")
        return {
            "sub": f"u-{token}",
            "email": f"{role}@{tenant}.test",
            "email_verified": True,
            "tenant_id": f"tenant-{tenant}",
            "role": role,
        }


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("score_routes.db")
    fresh_schema(url)
    for tid, slug in ((ACME, "acme"), (GLOBEX, "globex")):
        with tenant_session(tid, url) as s:
            s.add(models.Tenant(id=tid, name=slug, slug=slug, status="active"))
    graph, findings = GraphStore(url), FindingStore(url)
    settings = PgTenantSettings(url)
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(service, "_graph_store", graph)
    monkeypatch.setattr(service, "_finding_store", findings)
    monkeypatch.setattr(service, "_hold_store", holds_mod)
    monkeypatch.setattr(service, "_tenant_settings", settings)
    monkeypatch.setattr(service, "_audit", JsonlAuditSink(audit_path))
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)

    ctx = RunContext(tenant_id=ACME, case_id="", run_id="r", actor="engine")
    vendor = graph.upsert_node(
        ctx,
        kind="vendor",
        natural_key="vendors:v1",
        ref_table="vendors",
        ref_id="v1",
        attrs={"name": "Acme Steel", "abn": "51824753556"},
    )
    invoice = graph.upsert_node(
        ctx,
        kind="invoice",
        natural_key="history:h1",
        ref_table="trust_history_records",
        ref_id="h1",
        attrs={"amount": "1000.00", "invoice_date": "2026-09-01"},
    )
    graph.upsert_edge(
        ctx,
        src=invoice,
        dst=vendor,
        kind="issued_by",
        source_system="t",
        provenance="system_record",
        confidence=1.0,
        observed_at=AT,
    )
    fid, _ = findings.upsert(
        ctx,
        FindingSpec(
            type="PAY-UNVERIFIED",
            transaction_node=invoice,
            dedupe_key="PAY-UNVERIFIED:history:h1",
            impact=Decimal("1000.00"),
            currency="INR",
            confidence=1.0,
            engine_version="payment-1",
            rule_version="FR-21",
            model_version="none",
            summary="Not verified.",
            details={"engine": "payment", "mode": "advisory", "hold": True},
            citations=(Citation(invoice),),
        ),
    )
    with tenant_session(ACME, url) as s:
        s.add(
            models.WebhookEndpoint(
                tenant_id=ACME,
                url="https://hooks.example/x",
                secret="s",
                events=["hold.placed", "hold.lifted"],
                active=True,
            )
        )
    score.compute(
        EngineContext(
            ctx=ctx,
            mode="advisory",
            document=None,
            invoice_node=invoice,
            graph=graph,
            links=LinkStore(url),
            findings=findings,
            providers=None,
            settings={"trust_engine_modes": {"payment": "advisory"}},
            decider=None,
            blobs=None,
            url=url,
        ),
        {},
    )

    def client(token):
        return TestClient(app, headers={"Authorization": f"Bearer {token}"})

    return {
        "client": client,
        "url": url,
        "invoice": invoice,
        "vendor": vendor,
        "finding": fid,
        "audit": audit_path,
        "graph": graph,
    }


def test_transaction_trust_is_the_score_node_with_hold_and_findings(env):
    r = env["client"]("acme:viewer").get(
        f"/api/trust/transactions/{env['invoice']}/trust"
    )
    assert r.status_code == 200, r.text
    x = r.json()
    assert x["score_version"] == "score-1"
    assert x["dimensions"]["identity"]["value"] == 0.0
    assert x["hold"]["held"] is False
    (row,) = x["findings"]["identity"]
    assert row["id"] == env["finding"] and row["hold"] is True
    assert x["findings"]["other"] == []


def test_transaction_trust_reads_the_holds_with_todays_modes(env):
    PgTenantSettings(env["url"]).put(
        ACME, {"trust_engine_modes": {"payment": "enforcing"}}
    )
    x = (
        env["client"]("acme:viewer")
        .get(f"/api/trust/transactions/{env['invoice']}/trust")
        .json()
    )
    assert x["dimensions"]["identity"]["hold"] is True
    assert x["holds"] == [env["finding"]]
    assert x["mode"]["payment"] == "enforcing"


def test_transaction_trust_as_of_and_errors(env):
    c = env["client"]("acme:viewer")
    base = f"/api/trust/transactions/{env['invoice']}/trust"
    assert c.get(base + "?as_of=2000-01-01T00:00:00").status_code == 404
    assert c.get(base + "?as_of=yesterday").status_code == 400
    assert c.get("/api/trust/transactions/nope/trust").status_code == 404
    assert (
        c.get(f"/api/trust/transactions/{env['vendor']}/trust").status_code
        == 404
    )


def test_vendor_trust(env):
    r = env["client"]("acme:viewer").get(
        f"/api/trust/vendors/{env['vendor']}/trust"
    )
    assert r.status_code == 200, r.text
    assert r.json()["vendor"]["name"] == "Acme Steel"
    assert r.json()["dimensions"]["identity"] == 0.0
    assert (
        env["client"]("acme:viewer")
        .get(f"/api/trust/vendors/{env['invoice']}/trust")
        .status_code
        == 404
    )


def test_posture(env):
    c = env["client"]("acme:viewer")
    r = c.get("/api/trust/posture")
    assert r.status_code == 200, r.text
    assert r.json()["found"] == {"count": 1, "impact": "1000.00"}
    # Two vendors can share a name; the tax id tells them apart.
    assert [(v["name"], v["tax_id"]) for v in r.json()["by_vendor"]] == [
        ("Acme Steel", "51824753556")
    ]
    assert c.get("/api/trust/posture?since=2099-01-01").json()["found"] == {
        "count": 0,
        "impact": "0.00",
    }
    assert c.get("/api/trust/posture?since=soon").status_code == 400


def test_readiness_is_admin_only(env):
    assert (
        env["client"]("acme:approver").get("/api/trust/readiness").status_code
        == 403
    )
    r = env["client"]("acme:admin").get("/api/trust/readiness")
    assert r.status_code == 200, r.text
    rows = {row["engine"]: row for row in r.json()["engines"]}
    assert rows["payment"]["would_have_held"] == 1
    assert rows["payment"]["recommendation"]["to"] == "shadow"


def test_mode_change_is_audited_and_reconciles_holds(env):
    admin = env["client"]("acme:admin")
    url = "/api/trust/engines/payment/mode"
    assert admin.post(url, json={"mode": "enforcing"}).status_code == 400
    assert (
        admin.post(url, json={"mode": "loud", "reason": "x"}).status_code == 400
    )
    assert (
        admin.post(
            "/api/trust/engines/nope/mode", json={"mode": "off", "reason": "x"}
        ).status_code
        == 404
    )
    assert admin.post(url).status_code == 400
    assert (
        env["client"]("acme:approver")
        .post(url, json={"mode": "enforcing", "reason": "x"})
        .status_code
        == 403
    )
    r = admin.post(url, json={"mode": "enforcing", "reason": "Ready."})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "enforcing" and body["open_holds"] == 1
    assert (
        body["change"]["from"] == "off" and body["change"]["to"] == "enforcing"
    )
    events = [
        json.loads(line) for line in env["audit"].read_text().splitlines()
    ]
    (changed,) = [
        e for e in events if e["event_type"] == "trust_engine_mode_changed"
    ]
    assert changed["payload"]["reason"] == "Ready."
    with tenant_session(ACME, env["url"]) as s:
        outbox = [o.event for o in s.scalars(select(models.WebhookOutbox))]
    assert outbox == ["hold.placed"]
    r = admin.post(url, json={"mode": "advisory", "reason": "Back off."})
    assert r.json()["open_holds"] == 0
    with tenant_session(ACME, env["url"]) as s:
        outbox = sorted(
            o.event for o in s.scalars(select(models.WebhookOutbox))
        )
    assert outbox == ["hold.lifted", "hold.placed"]


class TestScoreIsolation:
    def test_score_routes_stay_with_their_tenant(self, env):
        globex = env["client"]("globex:admin")
        assert (
            globex.get(
                f"/api/trust/transactions/{env['invoice']}/trust"
            ).status_code
            == 404
        )
        assert (
            globex.get(f"/api/trust/vendors/{env['vendor']}/trust").status_code
            == 404
        )
        p = globex.get("/api/trust/posture").json()
        assert p["found"]["count"] == 0 and p["by_vendor"] == []
        rows = globex.get("/api/trust/readiness").json()["engines"]
        assert all(
            r["findings"] == 0 and r["would_have_held"] == 0 for r in rows
        )
        r = globex.post(
            "/api/trust/engines/payment/mode",
            json={"mode": "enforcing", "reason": "mine"},
        )
        assert r.status_code == 200 and r.json()["open_holds"] == 0
        with tenant_session(ACME, env["url"]) as s:
            assert s.scalars(select(models.TrustHold)).all() == []
        acme_modes = PgTenantSettings(env["url"]).get(ACME)
        assert (acme_modes.get("trust_engine_modes") or {}).get(
            "payment", "off"
        ) == "off"
