"""Explanation, evidence-pack and rating routes (the Why engine, FR-24)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from fresh_schema import fresh_schema

from invoice_processing.adapters import JsonlAuditSink, LocalBlobStore
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.repositories import PgDocumentRepository
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.explain import model as xm
from invoice_processing.trust.explain.templates import TEMPLATES
from invoice_processing.trust.findings.model import Citation, FindingSpec
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.store import GraphStore

# Every path this module adds; test_tenant_isolation_api unions it into its
# coverage check, and the probe class below exercises each one across tenants.
PROBED = {
    "/api/trust/findings/{finding_id}/explanation",
    "/api/trust/findings/{finding_id}/evidence-pack",
    "/api/trust/findings/{finding_id}/rating",
    "/api/trust/explanations/ratings",
}

ACME, GLOBEX = "tenant-acme", "tenant-globex"
AT = datetime(2026, 10, 1, tzinfo=UTC)


class FakeVerifier:
    """Token `<tenant>:<role>`; `acme:viewer` and `globex:clerk` are both real."""

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
    url = scratch_db("explain_routes.db")
    fresh_schema(url)
    for tid, slug in ((ACME, "acme"), (GLOBEX, "globex")):
        with tenant_session(tid, url) as s:
            s.add(models.Tenant(id=tid, name=slug, slug=slug, status="active"))
    with tenant_session(ACME, url) as s:
        s.add(
            models.Document(
                tenant_id=ACME,
                id="doc-c",
                sha256="c" * 64,
                storage_key="contracts/c.pdf",
                kind="contract",
                filename="msa.pdf",
            )
        )
    graph, findings = GraphStore(url), FindingStore(url)
    monkeypatch.setattr(service, "_graph_store", graph)
    monkeypatch.setattr(service, "_finding_store", findings)
    monkeypatch.setattr(service, "_documents", PgDocumentRepository(url))
    monkeypatch.setattr(
        service, "get_blobs", lambda tenant_id: LocalBlobStore(tmp_path / "b")
    )
    monkeypatch.setattr(
        service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
    )
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
        attrs={"name": "Acme Steel"},
    )
    invoice = graph.upsert_node(
        ctx,
        kind="invoice",
        natural_key="invoices:i1",
        ref_table="invoices",
        ref_id="i1",
        attrs={
            "case_id": "case-1",
            "invoice_number": "INV-1041",
            "amount": "1000.00",
            "currency": "INR",
        },
    )
    contract = graph.upsert_node(
        ctx,
        kind="contract",
        natural_key="documents:doc-c",
        ref_table="documents",
        ref_id="doc-c",
        attrs={"filename": "msa.pdf", "vendor_id": "v1"},
    )
    line = graph.upsert_node(
        ctx,
        kind="invoice_line",
        natural_key="invoice_lines:l1",
        attrs={
            "line_number": 1,
            "description": "steel rod",
            "quantity": "10",
            "unit_price": "60.00",
        },
    )
    clause = graph.upsert_node(
        ctx,
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
                Citation(line, "steel rod"),
                Citation(clause, "INR 50 per unit <script>x</script>"),
            ),
        ),
    )

    def client(token):
        return TestClient(app, headers={"Authorization": f"Bearer {token}"})

    return {
        "client": client,
        "finding": fid,
        "invoice": invoice,
        "clause": clause,
        "url": url,
    }


def test_explanation_is_cited_and_shaped_for_the_frontend(env):
    r = env["client"]("acme:viewer").get(
        f"/api/trust/findings/{env['finding']}/explanation"
    )
    assert r.status_code == 200, r.text
    x = r.json()
    assert x["finding_id"] == env["finding"] and x["type"] == "LEAK-RATE"
    assert x["version"] == xm.EXPLAIN_VERSION
    assert x["why"] and all(set(s) == {"text", "node_id"} for s in x["why"])
    cited = {env["invoice"], env["clause"]} | {
        e["node_id"] for e in x["evidence"]
    }
    assert all(s["node_id"] in cited for s in x["why"])
    assert x["counterfactual"]["payable_correct"] == "900.00"
    assert x["counterfactual"]["delta"] == "100.00"
    by_id = {e["node_id"]: e for e in x["evidence"]}
    assert by_id[env["clause"]]["source"] == {
        "document_url": "/api/trust/contracts/doc-c/file",
        "page": 2,
    }
    assert by_id[env["invoice"]]["source"]["document_url"] == (
        "/api/invoices/case-1/document"
    )
    assert x["caused_by"][0]["kind"] == "vendor"
    assert (
        env["client"]("acme:viewer")
        .get("/api/trust/findings/nope/explanation")
        .status_code
        == 404
    )


def test_uncited_template_is_a_500_and_logged(env, monkeypatch, caplog):
    monkeypatch.setitem(
        TEMPLATES, "LEAK-RATE", lambda ctx: {"why": [xm.Step("x", "n-made-up")]}
    )
    with caplog.at_level("ERROR"):
        r = env["client"]("acme:viewer").get(
            f"/api/trust/findings/{env['finding']}/explanation"
        )
    assert r.status_code == 500
    assert any(rec.message == "explain_uncited" for rec in caplog.records)
    assert env["finding"] not in caplog.text


def test_evidence_pack_json_and_html(env):
    c = env["client"]("acme:viewer")
    url = f"/api/trust/findings/{env['finding']}/evidence-pack"
    j = c.get(url).json()
    assert j["pack_version"] == 1 and j["sha256"]
    assert j["finding"]["id"] == env["finding"]
    assert j["explanation"]["type"] == "LEAK-RATE"
    assert {n["id"] for n in j["nodes"]} >= {env["invoice"], env["clause"]}
    assert j["lineage"][0]["stage"] == "contract"
    assert j["decision"]["status"] == "detected"
    assert c.get(url + "?format=json").json()["sha256"] == j["sha256"]
    h = c.get(url + "?format=html")
    assert h.status_code == 200
    assert h.headers["content-type"].startswith("text/html")
    assert h.headers["content-disposition"].startswith("inline")
    assert "<script>x</script>" not in h.text and "&lt;script&gt;" in h.text
    assert j["sha256"] in h.text
    assert c.get(url + "?format=pdf").status_code == 400
    assert c.get("/api/trust/findings/nope/evidence-pack").status_code == 404


def test_rating_needs_a_clerk_and_upserts(env):
    url = f"/api/trust/findings/{env['finding']}/rating"
    body = {"correct": True, "useful": False, "note": "ok"}
    assert env["client"]("acme:viewer").post(url, json=body).status_code == 403
    clerk = env["client"]("acme:clerk")
    r = clerk.post(url, json=body)
    assert r.status_code == 200, r.text
    assert (
        r.json()["rater"] == "clerk@acme.test" and r.json()["useful"] is False
    )
    r = clerk.post(url, json={"correct": False, "useful": True})
    assert r.json()["correct"] is False and r.json()["note"] is None
    assert clerk.post(url, json={"correct": "yes"}).status_code == 422
    assert clerk.post(url, json={**body, "note": "n" * 501}).status_code == 422
    assert (
        clerk.post("/api/trust/findings/nope/rating", json=body).status_code
        == 404
    )
    pack = clerk.get(
        f"/api/trust/findings/{env['finding']}/evidence-pack"
    ).json()
    assert [r["rater"] for r in pack["ratings"]] == ["clerk@acme.test"]
    agg = env["client"]("acme:viewer").get("/api/trust/explanations/ratings")
    assert agg.json() == {
        "by_type": {"LEAK-RATE": {"rated": 1, "correct": 0, "useful": 1}}
    }


class TestExplainIsolation:
    def test_explain_routes_stay_with_their_tenant(self, env):
        acme, globex = (
            env["client"]("acme:clerk"),
            env["client"]("globex:clerk"),
        )
        fid = env["finding"]
        acme.post(
            f"/api/trust/findings/{fid}/rating",
            json={"correct": True, "useful": True},
        )
        assert (
            globex.get(f"/api/trust/findings/{fid}/explanation").status_code
            == 404
        )
        assert (
            globex.get(f"/api/trust/findings/{fid}/evidence-pack").status_code
            == 404
        )
        assert (
            globex.get(
                f"/api/trust/findings/{fid}/evidence-pack?format=html"
            ).status_code
            == 404
        )
        probe = globex.post(
            f"/api/trust/findings/{fid}/rating",
            json={"correct": False, "useful": False},
        )
        assert probe.status_code == 404
        assert globex.get("/api/trust/explanations/ratings").json() == {
            "by_type": {}
        }
        pack = acme.get(f"/api/trust/findings/{fid}/evidence-pack").json()
        assert [r["correct"] for r in pack["ratings"]] == [True]
        assert acme.get("/api/trust/explanations/ratings").json()[
            "by_type"
        ] == {"LEAK-RATE": {"rated": 1, "correct": 1, "useful": 1}}
