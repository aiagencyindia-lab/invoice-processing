"""The /v2 façade and the integration admin routes (PRD §9, FR-30).

Every path here is in `PROBED`; test_tenant_isolation_api unions it into its
coverage check, and `TestIsolation` below probes each one across tenants with
a bearer and with the other tenant's API key.
"""

import hashlib
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from fresh_schema import fresh_schema
from sqlalchemy import select

from benchmark.history.faults.provenance import make_pdf
from invoice_processing.adapters import JsonlAuditSink, LocalBlobStore
from invoice_processing.api import auth, service, trust_agent_routes
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.repositories import PgDocumentRepository
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.tenant_settings import JsonTenantSettings
from invoice_processing.trust import apikeys, modes, score
from invoice_processing.trust.agents import mandates
from invoice_processing.trust.agents.store import AgentStore
from invoice_processing.trust.engine import EngineContext
from invoice_processing.trust.findings.model import Citation, FindingSpec
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.webhooks import target
from invoice_processing.worker.queue import LocalQueue

PROBED = {
    "/api/integrations/api-keys",
    "/api/integrations/api-keys/{key_id}",
    "/api/integrations/webhooks",
    "/api/integrations/webhooks/{endpoint_id}",
    "/api/integrations/webhooks/{endpoint_id}/test",
    "/api/integrations/webhooks/{endpoint_id}/deliveries",
    "/v2/transactions/{node_id}/trust",
    "/v2/transactions/{node_id}/explanation",
    "/v2/transactions/{node_id}/lineage",
    "/v2/findings",
    "/v2/findings/{finding_id}/decision",
    "/v2/vendors/{node_id}/trust",
    "/v2/documents/{document_id}/authenticity",
    "/v2/evidence/{node_id}",
    "/v2/trust-audits",
    "/v2/trust-audits/{audit_id}/runs",
    "/v2/trust-audits/{audit_id}/report",
    "/v2/agents",
    "/v2/agents/{agent_id}/mandates",
    "/v2/agents/{agent_id}/actions/evaluate",
    "/v2/agents/{agent_id}/ledger",
}
# Registry changes are a human admin's, as on /api/trust/agents.
ADMIN_ONLY = {"/v2/agents", "/v2/agents/{agent_id}/mandates"}

ACME, GLOBEX = "tenant-acme", "tenant-globex"
AT = datetime(2026, 10, 1, tzinfo=UTC)
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
SCOPE = {
    "actions": ["invoice.approve", "invoice.export"],
    "caps": {"per_action": "50000"},
    "currency": "INR",
}
ROLES = ("viewer", "clerk", "approver", "admin")


class FakeVerifier:
    """Token `<tenant>:<role>`."""

    def verify(self, token):
        tenant, _, role = token.partition(":")
        if tenant not in ("acme", "globex") or role not in ROLES:
            raise auth.AuthError("Invalid token")
        return {
            "sub": f"u-{token}",
            "email": f"{role}@{tenant}.test",
            "email_verified": True,
            "tenant_id": f"tenant-{tenant}",
            "role": role,
        }


def _seed(graph, findings, tid, vendor_name):
    ctx = RunContext(tenant_id=tid, case_id="", run_id="r", actor="engine")
    node = lambda **kw: graph.upsert_node(ctx, **kw)  # noqa: E731
    vendor = node(
        kind="vendor",
        natural_key="vendors:v1",
        ref_table="vendors",
        ref_id="v1",
        attrs={"name": vendor_name},
    )
    invoice = node(
        kind="invoice",
        natural_key="invoices:i1",
        ref_table="invoices",
        ref_id="i1",
        attrs={
            "invoice_number": "INV-1041",
            "amount": "1000.00",
            "currency": "INR",
        },
    )
    contract = node(
        kind="contract",
        natural_key=f"documents:doc-{tid}",
        ref_table="documents",
        ref_id=f"doc-{tid}",
        attrs={"filename": "msa.pdf", "vendor_id": "v1"},
    )
    line = node(
        kind="invoice_line",
        natural_key="invoice_lines:l1",
        attrs={"line_number": 1, "description": "rod", "quantity": "10"},
    )
    clause = node(
        kind="contract_clause",
        natural_key="clause:c:2:abc",
        attrs={
            "type": "rate",
            "params": {"price": "50"},
            "page": 2,
            "excerpt": "INR 50 per unit",
            "confidence": 0.8,
            "reviewed": False,
        },
    )
    for src, dst, kind in (
        (invoice, vendor, "issued_by"),
        (contract, vendor, "governs"),
        (invoice, contract, "governed_by"),
        (line, invoice, "part_of"),
        (clause, contract, "part_of"),
    ):
        graph.upsert_edge(
            ctx,
            src=src,
            dst=dst,
            kind=kind,
            source_system="t",
            provenance="system_record",
            confidence=1.0,
            observed_at=AT,
        )
    fid, _ = findings.upsert(
        ctx,
        FindingSpec(
            type="LEAK-RATE",
            transaction_node=invoice,
            dedupe_key="LEAK-RATE:l1",
            impact=Decimal("100.00"),
            currency="INR",
            confidence=0.9,
            engine_version="leakage-1",
            rule_version="FR-20.rate-1",
            model_version="decide",
            summary="Unit price above what the contract allows.",
            details={
                "line": line,
                "quantity": "10",
                "unit_price": "60.00",
                "contract_rate": "50",
                "allowed_unit_price": "50.00",
                "clauses": [clause],
                "needs_review": True,
            },
            citations=(
                Citation(invoice),
                Citation(contract),
                Citation(line, "rod"),
                Citation(clause, "INR 50 per unit"),
            ),
        ),
    )
    return {"vendor": vendor, "invoice": invoice, "finding": fid}


def _scorable(graph, findings, tid, tmp_path, url):
    """What the score and provenance routes read: the invoice's
    `trust_score` node, written by the score engine itself, and a stored
    PDF the authenticity route can score. Returns the document id."""
    ctx = RunContext(tenant_id=tid, case_id="", run_id="r", actor="engine")
    score.compute(
        EngineContext(
            ctx=ctx,
            mode="advisory",
            document=None,
            invoice_node=None,
            graph=graph,
            links=LinkStore(url),
            findings=findings,
            providers=None,
            settings={},
            decider=None,
            blobs=None,
            url=url,
        ),
        {},
    )
    pdf = make_pdf()
    key = f"documents/{tid}.pdf"
    LocalBlobStore(tmp_path / "b").put(key, pdf)
    with tenant_session(tid, url) as s:
        s.add(
            models.Document(
                tenant_id=tid,
                id=f"kyc-{tid}",
                sha256=hashlib.sha256(pdf).hexdigest(),
                storage_key=key,
                kind="kyc",
            )
        )
    return f"kyc-{tid}"


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    # Test hosts resolve outward; no real DNS.
    monkeypatch.setattr(target, "addresses", lambda host: ["93.184.216.34"])
    url = scratch_db("api_routes.db")
    fresh_schema(url)
    for tid, slug in ((ACME, "acme"), (GLOBEX, "globex")):
        with tenant_session(tid, url) as s:
            s.add(models.Tenant(id=tid, name=slug, slug=slug, status="active"))
    graph, findings = GraphStore(url), FindingStore(url)
    audit = JsonlAuditSink(tmp_path / "audit.jsonl")
    monkeypatch.setattr(service, "_graph_store", graph)
    monkeypatch.setattr(service, "_finding_store", findings)
    monkeypatch.setattr(service, "_documents", PgDocumentRepository(url))
    monkeypatch.setattr(
        service, "_tenant_settings", JsonTenantSettings(tmp_path / "ts.json")
    )
    monkeypatch.setattr(
        service, "get_blobs", lambda tenant_id: LocalBlobStore(tmp_path / "b")
    )
    monkeypatch.setattr(service, "_audit", audit)
    agents = AgentStore(url, graph=graph)
    monkeypatch.setattr(service, "_agent_store", agents)
    monkeypatch.setattr(trust_agent_routes, "_now", lambda: NOW)
    monkeypatch.setenv("TRUST_MANDATE_KEY", "test-mandate-key")
    queue = LocalQueue(tmp_path / "queue")
    monkeypatch.setattr(service, "get_queue", lambda: queue)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)

    out = {"url": url, "audit": tmp_path / "audit.jsonl"}
    for tid, slug, name in (
        (ACME, "acme", "Acme Steel"),
        (GLOBEX, "globex", "Globex"),
    ):
        data = _seed(graph, findings, tid, name)
        data["document"] = _scorable(graph, findings, tid, tmp_path, url)
        ctx = RunContext(
            tenant_id=tid, case_id="", run_id="r", actor=f"admin@{slug}"
        )
        data["keys"] = {}
        for role in ("viewer", "clerk", "approver"):
            row, key = apikeys.create(ctx, f"{role}-key", role, url=url)
            data["keys"][role] = key
            data["key_id"] = row["id"]
        data["agent"] = _mandated(agents, tid, slug, data["key_id"])
        # Off means agent control does nothing; its routes need it on.
        modes.set_engine_mode(
            ctx, "agents", "shadow", store=service._tenant_settings
        )
        with tenant_session(tid, url) as s:
            ep = models.WebhookEndpoint(
                tenant_id=tid,
                url=f"https://{slug}.example/hook",
                secret=f"whsec_{slug}secret",
                events=["*"],
            )
            s.add(ep)
            s.flush()
            data["endpoint"] = ep.id
        r = bearer(f"{slug}:approver").post(
            "/api/trust/audits", json={"name": "FY26"}
        )
        assert r.status_code == 201, r.text
        data["audit"] = r.json()["id"]
        out[slug] = data
    return out


def _mandated(agents, tid, slug, key_id):
    """An agent bound to the tenant's approver key, under a mandate one
    admin proposed and another approved."""
    ctx = lambda who: RunContext(  # noqa: E731
        tenant_id=tid, case_id="", run_id="r", actor=f"{who}@{slug}.test"
    )
    agent = agents.register(
        ctx("admin"),
        name="payables-bot",
        owner=f"ops@{slug}.test",
        kind="api_key",
        principal_id=f"api-key:{key_id}",
        principal_email=f"approver-key@api-key.{tid}",
    )
    m = agents.propose_mandate(
        ctx("admin"),
        agent["id"],
        scope=SCOPE,
        valid_from=NOW - timedelta(days=21),
        valid_to=NOW + timedelta(days=40),
    )
    mandates.approve(ctx("controller"), agents, m["id"])
    return agent["id"]


def _mandate_body():
    return {
        "scope": SCOPE,
        "valid_from": (NOW - timedelta(days=1)).isoformat(),
        "valid_to": (NOW + timedelta(days=30)).isoformat(),
    }


def bearer(token):
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def keyed(key):
    return TestClient(app, headers={"X-API-Key": key})


# -- parity: /v2 answers what /api answers ------------------------------------


def _pairs(d):
    inv, ven, aud = d["invoice"], d["vendor"], d["audit"]
    return [
        ("get", "/v2/findings", "/api/trust/findings"),
        (
            "get",
            "/v2/findings?status=detected&limit=1",
            "/api/trust/findings?status=detected&limit=1",
        ),
        (
            "get",
            "/v2/findings?status=bogus",
            "/api/trust/findings?status=bogus",
        ),
        ("get", f"/v2/transactions/{inv}/lineage", f"/api/trust/lineage/{inv}"),
        ("get", "/v2/transactions/nope/lineage", "/api/trust/lineage/nope"),
        ("get", f"/v2/evidence/{inv}", f"/api/trust/nodes/{inv}"),
        ("get", "/v2/evidence/nope", "/api/trust/nodes/nope"),
        (
            "get",
            f"/v2/transactions/{inv}/trust",
            f"/api/trust/transactions/{inv}/trust",
        ),
        ("get", f"/v2/vendors/{ven}/trust", f"/api/trust/vendors/{ven}/trust"),
        (
            "post",
            "/v2/documents/doc-x/authenticity",
            "/api/trust/documents/doc-x/authenticity",
        ),
        (
            "get",
            f"/v2/trust-audits/{aud}/report",
            f"/api/trust/audits/{aud}/report",
        ),
    ]


@pytest.mark.parametrize("via", ["bearer", "key"])
def test_each_v2_read_answers_what_its_api_route_answers(env, via):
    d = env["acme"]
    client = (
        bearer("acme:viewer") if via == "bearer" else keyed(d["keys"]["viewer"])
    )
    for method, v2, api in _pairs(d):
        a, b = getattr(client, method)(v2), getattr(client, method)(api)
        if v2.endswith("/trust"):
            # The real handlers, seeded: parity of two 404s would prove little.
            assert a.status_code == 200, (v2, a.text)
        assert (a.status_code, _stable(a.json())) == (
            b.status_code,
            _stable(b.json()),
        ), v2


def _stable(body):
    """The answer without the moment it was generated."""
    return {k: v for k, v in body.items() if k != "generated_at"}


def test_the_explanation_lists_each_open_finding_with_its_explanation(env):
    d = env["acme"]
    client = keyed(d["keys"]["viewer"])
    r = client.get(f"/v2/transactions/{d['invoice']}/explanation")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        body["transaction"]
        == client.get(f"/api/trust/nodes/{d['invoice']}").json()["node"]
    )
    (item,) = body["findings"]
    listed = client.get("/api/trust/findings").json()["findings"]
    assert item["finding"] == next(f for f in listed if f["id"] == d["finding"])
    assert (
        item["explanation"]
        == client.get(f"/api/trust/findings/{d['finding']}/explanation").json()
    )
    assert client.get("/v2/transactions/nope/explanation").status_code == 404


def test_a_decided_finding_leaves_the_explanation(env):
    d = env["acme"]
    bearer("acme:approver").post(
        f"/v2/findings/{d['finding']}/decision",
        json={"decision": "reject", "reason": "fine"},
    )
    r = keyed(d["keys"]["viewer"]).get(
        f"/v2/transactions/{d['invoice']}/explanation"
    )
    assert r.json()["findings"] == []


def test_trust_audit_writes_match_their_api_routes(env):
    d = env["acme"]
    client = keyed(d["keys"]["approver"])
    r = client.post("/v2/trust-audits", json={"name": "Q3"})
    assert r.status_code == 201, r.text
    created = r.json()
    got = client.get(f"/api/trust/audits/{created['id']}").json()
    assert created == {k: got[k] for k in created}
    r = client.post(
        f"/v2/trust-audits/{created['id']}/runs", json={"engines": []}
    )
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued"
    assert (
        client.post("/v2/trust-audits", json={"name": " "}).status_code == 400
    )
    # A viewer key cannot open an engagement.
    v = keyed(d["keys"]["viewer"]).post("/v2/trust-audits", json={"name": "x"})
    assert v.status_code == 403


# -- agents --------------------------------------------------------------------


def _without(body, *keys):
    return {k: v for k, v in body.items() if k not in keys}


def test_agent_registry_writes_match_their_api_routes(env):
    admin = bearer("acme:admin")
    made = []
    for path, name in (
        ("/v2/agents", "v2-bot"),
        ("/api/trust/agents", "api-bot"),
    ):
        r = admin.post(path, json={"name": name, "owner": "ops@acme.test"})
        assert r.status_code == 201, r.text
        made.append(r.json())
    assert _without(made[0], "id", "name", "graph_node") == _without(
        made[1], "id", "name", "graph_node"
    )
    proposed = []
    for prefix, agent in zip(
        ("/v2/agents", "/api/trust/agents"), made, strict=True
    ):
        r = admin.post(f"{prefix}/{agent['id']}/mandates", json=_mandate_body())
        assert r.status_code == 201, r.text
        proposed.append(r.json())
    same = (
        "version",
        "status",
        "scope",
        "valid_from",
        "valid_to",
        "proposed_by",
    )
    assert [{k: m[k] for k in same} for m in proposed] == [
        {k: proposed[1][k] for k in same}
    ] * 2
    for path in ("/v2/agents", "/api/trust/agents"):
        bad = admin.post(path, json={"name": "x", "owner": "not-an-email"})
        assert bad.status_code == 400
    bad = [
        admin.post(f"{p}/{made[0]['id']}/mandates", json={"scope": {}})
        for p in ("/v2/agents", "/api/trust/agents")
    ]
    assert bad[0].status_code == bad[1].status_code == 422


def test_agent_evaluate_and_ledger_match_their_api_routes(env):
    d = env["acme"]
    client = keyed(d["keys"]["approver"])
    body = {
        "action": "invoice.approve",
        "subject_ref": "INV-1041",
        "vendor": "Acme Steel",
        "amount": "1200.00",
        "currency": "INR",
        "source": "dryrun",
    }
    agent = d["agent"]
    answers = [
        client.post(f"{p}/{agent}/actions/evaluate", json=body)
        for p in ("/v2/agents", "/api/trust/agents")
    ]
    assert [a.status_code for a in answers] == [200, 200], answers[0].text
    assert _without(answers[0].json(), "action_id") == _without(
        answers[1].json(), "action_id"
    )
    a, b = (
        client.get(f"{p}/{agent}/ledger")
        for p in ("/v2/agents", "/api/trust/agents")
    )
    assert a.status_code == 200, a.text
    assert a.json() == b.json()
    assert (
        client.get("/v2/agents/nope/ledger").status_code
        == client.get("/api/trust/agents/nope/ledger").status_code
        == 404
    )


@pytest.mark.parametrize("role", ["viewer", "clerk", "approver"])
def test_agent_registry_writes_refuse_every_key(env, role):
    d = env["acme"]
    client = keyed(d["keys"][role])
    assert (
        client.post(
            "/v2/agents", json={"name": "x", "owner": "ops@acme.test"}
        ).status_code
        == 403
    )
    r = client.post(f"/v2/agents/{d['agent']}/mandates", json=_mandate_body())
    assert r.status_code == 403


def test_a_viewer_cannot_evaluate_for_another_agent(env):
    d = env["acme"]
    r = keyed(d["keys"]["viewer"]).post(
        f"/v2/agents/{d['agent']}/actions/evaluate",
        json={
            "action": "invoice.approve",
            "subject_ref": "x",
            "source": "dryrun",
        },
    )
    assert r.status_code == 403


# -- decisions -----------------------------------------------------------------


def test_a_decision_triages_a_detected_finding_first(env):
    d = env["acme"]
    r = keyed(d["keys"]["clerk"]).post(
        f"/v2/findings/{d['finding']}/decision",
        json={"decision": "needs_vendor_input", "reason": "asked for POD"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "needs_vendor_input"
    got = (
        bearer("acme:viewer").get(f"/api/trust/findings/{d['finding']}").json()
    )
    assert got["status"] == "needs_vendor_input"


def test_accept_needs_an_approver(env):
    d = env["acme"]
    path = f"/v2/findings/{d['finding']}/decision"
    body = {"decision": "accept", "reason": "agreed"}
    assert keyed(d["keys"]["clerk"]).post(path, json=body).status_code == 403
    # Refused before anything moved: still detected.
    got = (
        bearer("acme:viewer").get(f"/api/trust/findings/{d['finding']}").json()
    )
    assert got["status"] == "detected"
    r = keyed(d["keys"]["approver"]).post(path, json=body)
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    again = keyed(d["keys"]["approver"]).post(path, json=body)
    assert again.status_code == 409


def test_a_viewer_cannot_decide_and_a_bad_decision_is_refused(env):
    d = env["acme"]
    path = f"/v2/findings/{d['finding']}/decision"
    v = keyed(d["keys"]["viewer"]).post(path, json={"decision": "reject"})
    assert v.status_code == 403
    bad = keyed(d["keys"]["approver"]).post(path, json={"decision": "approve"})
    assert bad.status_code == 422
    unknown = keyed(d["keys"]["approver"]).post(
        "/v2/findings/nope/decision",
        json={"decision": "reject", "reason": "dup"},
    )
    assert unknown.status_code == 404


# -- authentication --------------------------------------------------------------


def test_a_bearer_wins_over_a_key_header(env):
    c = TestClient(
        app,
        headers={
            "Authorization": "Bearer globex:viewer",
            "X-API-Key": env["acme"]["keys"]["viewer"],
        },
    )
    ids = {f["id"] for f in c.get("/v2/findings").json()["findings"]}
    assert ids == {env["globex"]["finding"]}


def test_a_key_works_as_a_bearer_too(env):
    k = env["acme"]["keys"]["viewer"]
    r = bearer(k).get("/v2/findings")
    assert {f["id"] for f in r.json()["findings"]} == {env["acme"]["finding"]}


@pytest.mark.parametrize(
    "key", ["ik_", "ik_deadbeef_" + "x" * 43, "not-a-key", "ik_zz_zz"]
)
def test_an_unknown_key_is_a_401(env, key):
    assert keyed(key).get("/v2/findings").status_code == 401
    assert bearer(key).get("/v2/findings").status_code == 401


def test_a_revoked_key_is_a_401(env):
    d = env["acme"]
    key = d["keys"]["approver"]
    assert keyed(key).get("/v2/findings").status_code == 200
    r = bearer("acme:admin").delete(f"/api/integrations/api-keys/{d['key_id']}")
    assert r.status_code == 200
    assert keyed(key).get("/v2/findings").status_code == 401


# -- integration admin -------------------------------------------------------------


def test_api_key_lifecycle(env):
    admin = bearer("acme:admin")
    r = admin.post(
        "/api/integrations/api-keys", json={"name": "erp", "role": "clerk"}
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["key"].startswith("ik_")
    listed = admin.get("/api/integrations/api-keys").json()["keys"]
    assert created["id"] in {k["id"] for k in listed}
    assert created["key"] not in str(listed)
    assert all("key_hash" not in k and "key" not in k for k in listed)
    assert keyed(created["key"]).get("/v2/findings").status_code == 200
    assert (
        admin.post(
            "/api/integrations/api-keys", json={"name": "x", "role": "admin"}
        ).status_code
        == 400
    )
    gone = admin.delete(f"/api/integrations/api-keys/{created['id']}")
    assert gone.status_code == 200 and gone.json()["revoked_at"]
    assert (
        admin.delete(f"/api/integrations/api-keys/{created['id']}").status_code
        == 404
    )


def test_webhook_lifecycle(env):
    admin, url = bearer("acme:admin"), env["url"]
    r = admin.post(
        "/api/integrations/webhooks",
        json={"url": "https://erp.example/in", "events": ["hold.placed"]},
    )
    assert r.status_code == 201, r.text
    ep = r.json()
    assert ep["secret"].startswith("whsec_") and len(ep["secret"]) > 20
    listed = admin.get("/api/integrations/webhooks").json()["endpoints"]
    mine = next(e for e in listed if e["id"] == ep["id"])
    assert mine["secret"] != ep["secret"] and mine["secret"].endswith(
        ep["secret"][-4:]
    )
    assert ep["secret"] not in str(listed)

    t = admin.post(f"/api/integrations/webhooks/{ep['id']}/test")
    assert t.status_code == 202
    deliveries = admin.get(
        f"/api/integrations/webhooks/{ep['id']}/deliveries"
    ).json()
    (row,) = deliveries["deliveries"]
    assert row["event"] == "ping" and row["delivered_at"] is None
    assert row["payload"]["tenant"] == ACME

    p = admin.patch(
        f"/api/integrations/webhooks/{ep['id']}",
        json={"active": False, "events": ["*"], "description": "paused"},
    )
    assert p.status_code == 200
    assert (p.json()["active"], p.json()["events"]) == (False, ["*"])

    assert (
        admin.delete(f"/api/integrations/webhooks/{ep['id']}").status_code
        == 200
    )
    with tenant_session(ACME, url) as s:
        left = s.scalars(
            select(models.WebhookOutbox).where(
                models.WebhookOutbox.endpoint_id == ep["id"]
            )
        ).all()
    assert left == []
    assert (
        admin.delete(f"/api/integrations/webhooks/{ep['id']}").status_code
        == 404
    )


@pytest.mark.parametrize(
    "body",
    [
        {"url": "http://erp.example/in", "events": ["ping"]},
        {"url": "https://", "events": ["ping"]},
        {"url": "ftp://erp.example", "events": ["ping"]},
        {"url": "https://erp.example/in", "events": []},
        {"url": "https://erp.example/in", "events": ["invoice.paid"]},
        {"url": "https://erp.example/in", "events": ["*", "ping"]},
    ],
)
def test_a_bad_webhook_is_refused(env, body):
    r = bearer("acme:admin").post("/api/integrations/webhooks", json=body)
    assert r.status_code == 400, r.text


def test_a_bad_patch_is_refused(env):
    ep = env["acme"]["endpoint"]
    r = bearer("acme:admin").patch(
        f"/api/integrations/webhooks/{ep}", json={"events": ["nope"]}
    )
    assert r.status_code == 400


def test_deliveries_mask_bank_numbers(env):
    ep = env["acme"]["endpoint"]
    with tenant_session(ACME, env["url"]) as s:
        s.add(
            models.WebhookOutbox(
                tenant_id=ACME,
                endpoint_id=ep,
                event="hold.placed",
                payload={
                    "event": "hold.placed",
                    "data": {"bank_account_number": "123456789012"},
                },
                next_attempt_at=AT,
            )
        )
    r = bearer("acme:admin").get(f"/api/integrations/webhooks/{ep}/deliveries")
    assert "123456789012" not in r.text


@pytest.mark.parametrize("role", ["viewer", "clerk", "approver"])
def test_integrations_need_an_admin_and_refuse_every_key(env, role):
    for client in (bearer(f"acme:{role}"), keyed(env["acme"]["keys"][role])):
        assert client.get("/api/integrations/api-keys").status_code == 403
        assert client.get("/api/integrations/webhooks").status_code == 403


def test_a_key_principal_is_refused_even_as_admin(env, monkeypatch):
    real = apikeys.resolve

    def as_admin(token, url=None):
        p = real(token, url)
        return p and auth.Principal(p.user_id, p.email, p.tenant_id, "admin")

    monkeypatch.setattr(apikeys, "resolve", as_admin)
    r = keyed(env["acme"]["keys"]["viewer"]).get("/api/integrations/api-keys")
    assert r.status_code == 403


def test_an_unknown_role_body_is_refused(env):
    r = bearer("acme:admin").post(
        "/api/integrations/api-keys", json={"name": "erp", "role": "god"}
    )
    assert r.status_code == 400


# -- isolation -----------------------------------------------------------------------


def _calls(d):
    """(method, path template, filled path, json) for every PROBED route,
    pointed at tenant `d`'s rows."""
    fill = {
        "key_id": d["key_id"],
        "endpoint_id": d["endpoint"],
        "node_id": d["invoice"],
        "finding_id": d["finding"],
        "document_id": d["document"],
        "audit_id": d["audit"],
        "agent_id": d["agent"],
    }

    def filled(template):
        # A vendor route names the vendor's node, not the invoice's.
        values = (
            {**fill, "node_id": d["vendor"]}
            if template.startswith("/v2/vendors/")
            else fill
        )
        return re.sub(r"\{(\w+)\}", lambda g: values[g.group(1)], template)

    rows = [
        ("get", "/api/integrations/api-keys", None),
        ("post", "/api/integrations/api-keys", {"name": "x", "role": "viewer"}),
        ("delete", "/api/integrations/api-keys/{key_id}", None),
        ("get", "/api/integrations/webhooks", None),
        (
            "post",
            "/api/integrations/webhooks",
            {"url": "https://x.example/h", "events": ["*"]},
        ),
        (
            "patch",
            "/api/integrations/webhooks/{endpoint_id}",
            {"description": "x"},
        ),
        ("delete", "/api/integrations/webhooks/{endpoint_id}", None),
        ("post", "/api/integrations/webhooks/{endpoint_id}/test", None),
        ("get", "/api/integrations/webhooks/{endpoint_id}/deliveries", None),
        ("get", "/v2/transactions/{node_id}/trust", None),
        ("get", "/v2/transactions/{node_id}/explanation", None),
        ("get", "/v2/transactions/{node_id}/lineage", None),
        ("get", "/v2/findings", None),
        (
            "post",
            "/v2/findings/{finding_id}/decision",
            {"decision": "reject", "reason": "probe"},
        ),
        ("get", "/v2/vendors/{node_id}/trust", None),
        ("post", "/v2/documents/{document_id}/authenticity", None),
        ("get", "/v2/evidence/{node_id}", None),
        ("post", "/v2/trust-audits", {"name": "probe"}),
        ("post", "/v2/trust-audits/{audit_id}/runs", {"engines": []}),
        ("get", "/v2/trust-audits/{audit_id}/report", None),
        ("post", "/v2/agents", {"name": "probe", "owner": "ops@x.test"}),
        ("post", "/v2/agents/{agent_id}/mandates", _mandate_body()),
        (
            "post",
            "/v2/agents/{agent_id}/actions/evaluate",
            {
                "action": "invoice.export",
                "subject_ref": "p",
                "source": "dryrun",
            },
        ),
        ("get", "/v2/agents/{agent_id}/ledger", None),
    ]
    return [(m, t, filled(t), body) for m, t, body in rows]


def _send(client, method, path, body):
    if body is None:
        return getattr(client, method)(path)
    return client.request(method.upper(), path, json=body)


def test_every_probed_route_is_exercised():
    assert {
        t
        for _, t, _, _ in _calls(
            dict.fromkeys(
                (
                    "key_id",
                    "endpoint",
                    "invoice",
                    "vendor",
                    "document",
                    "finding",
                    "audit",
                    "agent",
                ),
                "x",
            )
        )
    } == PROBED


class TestIsolation:
    """Globex, by bearer and by key, aimed at Acme's rows: never Acme's data.
    Acme itself, the same call, gets through, so each probe would notice a
    leak rather than pass on a refusal."""

    @pytest.mark.parametrize("via", ["bearer", "key"])
    @pytest.mark.parametrize("i", range(24))
    def test_a_foreign_tenant_sees_nothing_of_acme(self, env, via, i):
        acme = env["acme"]
        method, template, path, body = _calls(acme)[i]
        integration = (
            template.startswith("/api/integrations") or template in ADMIN_ONLY
        )
        if via == "bearer":
            foreign = bearer(
                "globex:admin" if integration else "globex:approver"
            )
            own = bearer("acme:admin" if integration else "acme:approver")
        else:
            foreign = keyed(env["globex"]["keys"]["approver"])
            own = keyed(acme["keys"]["approver"])

        r = _send(foreign, method, path, body)
        for secret in (
            acme["finding"],
            acme["invoice"],
            acme["vendor"],
            acme["endpoint"],
            acme["key_id"],
            acme["audit"],
            acme["agent"],
            "Acme Steel",
            "acme.example",
        ):
            assert secret not in r.text, (template, via, r.status_code)
        if via == "key" and integration:
            assert r.status_code == 403, (template, r.status_code)
        elif "{" in template:
            # Aimed at an Acme id: not found, never done.
            assert r.status_code == 404, (template, via, r.status_code)

        mine = _send(own, method, path, body)
        if via == "key" and integration:
            assert mine.status_code == 403
        else:
            assert mine.status_code in (200, 201, 202), (
                template,
                via,
                mine.status_code,
                mine.text,
            )


def test_the_doc_lists_every_v2_route_and_nothing_else():
    from pathlib import Path  # noqa: PLC0415

    from invoice_processing.api.v2_routes import router  # noqa: PLC0415

    doc = (Path(__file__).parent.parent / "docs" / "API_V2.md").read_text()
    documented = set(re.findall(r"\| (GET|POST) \| `(/v2/[^`]+)`", doc))
    routed = {
        (method, route.path)
        for route in router.routes
        for method in route.methods
    }
    assert documented == routed


def test_the_doc_recipe_verifies_a_real_signature():
    from pathlib import Path  # noqa: PLC0415

    from invoice_processing.trust.webhooks.deliver import sign  # noqa: PLC0415

    doc = (Path(__file__).parent.parent / "docs" / "API_V2.md").read_text()
    recipe = re.search(r"```python\n(.*?)```", doc, re.S).group(1)
    scope: dict = {}
    exec(recipe, scope)  # noqa: S102 - our own doc
    import time  # noqa: PLC0415

    ts, body = str(int(time.time())), '{"event":"ping"}'
    headers = {
        "X-Trust-Timestamp": ts,
        "X-Trust-Signature": sign("s", ts, body),
    }
    assert scope["verify"]("s", headers, body.encode())
    assert not scope["verify"]("t", headers, body.encode())


def test_a_reject_without_a_reason_moves_nothing(env):
    d = env["acme"]
    r = keyed(d["keys"]["approver"]).post(
        f"/v2/findings/{d['finding']}/decision", json={"decision": "reject"}
    )
    assert r.status_code == 400
    got = (
        bearer("acme:viewer").get(f"/api/trust/findings/{d['finding']}").json()
    )
    assert got["status"] == "detected"


@pytest.mark.parametrize(
    "target",
    [
        "https://localhost/h",
        "https://api.localhost/h",
        "https://127.0.0.1/h",
        "https://10.0.0.5/h",
        "https://169.254.169.254/latest",
        "https://[::1]/h",
        "https://192.168.1.1/h",
    ],
)
def test_an_internal_webhook_target_is_refused(env, target):
    r = bearer("acme:admin").post(
        "/api/integrations/webhooks", json={"url": target, "events": ["*"]}
    )
    assert r.status_code == 400, r.text


def test_a_ping_to_a_paused_endpoint_is_refused(env):
    ep = env["acme"]["endpoint"]
    admin = bearer("acme:admin")
    admin.patch(f"/api/integrations/webhooks/{ep}", json={"active": False})
    assert (
        admin.post(f"/api/integrations/webhooks/{ep}/test").status_code == 409
    )


def test_a_foreign_patch_changes_nothing(env):
    ep = env["acme"]["endpoint"]
    r = bearer("globex:admin").patch(
        f"/api/integrations/webhooks/{ep}", json={"description": "pwned"}
    )
    assert r.status_code == 404
    listed = bearer("acme:admin").get("/api/integrations/webhooks").json()
    assert all(e["description"] != "pwned" for e in listed["endpoints"])


def test_a_name_resolving_inward_is_refused(env, monkeypatch):
    monkeypatch.setattr(target, "addresses", lambda host: ["10.0.0.5"])
    r = bearer("acme:admin").post(
        "/api/integrations/webhooks",
        json={"url": "https://inward.example/h", "events": ["*"]},
    )
    assert r.status_code == 400, r.text


@pytest.mark.parametrize(
    "u", ["https://10.0.0.1\\@erp.example/h", "https://u:p@erp.example/h"]
)
def test_an_ambiguous_or_credentialed_url_is_refused(env, u):
    r = bearer("acme:admin").post(
        "/api/integrations/webhooks", json={"url": u, "events": ["*"]}
    )
    assert r.status_code == 400, r.text


def test_the_intake_route_is_a_stub_until_branch_c(env):
    r = keyed(env["acme"]["keys"]["approver"]).post(
        "/v2/agents/actions", json={"actions": []}
    )
    assert r.status_code == 503
