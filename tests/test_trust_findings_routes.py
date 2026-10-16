"""Findings, node, lineage, contract-file and clause routes (Wave 2 scaffold)."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from invoice_processing.adapters import JsonlAuditSink, LocalBlobStore
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.repositories import PgDocumentRepository
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.findings import model as finding_model
from invoice_processing.trust.findings.model import Citation, FindingSpec
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.store import GraphStore

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="engine")
AT = datetime(2026, 10, 1, tzinfo=timezone.utc)
PDF = b"%PDF-1.4\n% contract\n"


class FakeVerifier:
    def verify(self, token):
        if token not in ("viewer", "clerk", "approver", "admin"):
            raise auth.AuthError("Invalid token")
        return {
            "sub": f"u-{token}",
            "email": f"{token}@x.test",
            "email_verified": True,
            "tenant_id": T,
            "role": token,
        }


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("findings_routes.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
        s.flush()
        s.add(
            models.Document(
                tenant_id=T,
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
    blobs = LocalBlobStore(tmp_path / "blobs")
    blobs.put("contracts/c.pdf", PDF, content_type="application/pdf")
    monkeypatch.setattr(service, "get_blobs", lambda tenant_id: blobs)
    monkeypatch.setattr(
        service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    finding_model.register_type("T-LEAK", "a leak", engine="leakage")
    finding_model.register_type("T-PAY", "a hold", engine="payment", hold=True)

    vendor = graph.upsert_node(
        CTX,
        kind="vendor",
        natural_key="vendors:v1",
        ref_table="vendors",
        ref_id="v1",
        attrs={"name": "Acme Steel"},
    )
    invoice = graph.upsert_node(
        CTX,
        kind="invoice",
        natural_key="invoices:i1",
        ref_table="invoices",
        ref_id="i1",
        attrs={"case_id": "case-1", "amount": "1000.0000"},
    )
    contract = graph.upsert_node(
        CTX,
        kind="contract",
        natural_key="documents:doc-c",
        ref_table="documents",
        ref_id="doc-c",
        attrs={"filename": "msa.pdf"},
    )
    clause = graph.upsert_node(
        CTX,
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

    def edge(src, dst, kind, source="v1", prov="system_record"):
        graph.upsert_edge(
            CTX,
            src=src,
            dst=dst,
            kind=kind,
            source_system=source,
            provenance=prov,
            confidence=1.0,
            observed_at=AT,
        )

    edge(invoice, vendor, "issued_by")
    edge(contract, vendor, "governs", "upload", "human_assertion")
    edge(invoice, contract, "governed_by", "leakage", "model_inference")
    edge(clause, contract, "part_of", "leakage", "model_inference")

    def finding(code, key, impact="100", node=invoice, cites=()):
        fid, _ = findings.upsert(
            CTX,
            FindingSpec(
                type=code,
                transaction_node=node,
                dedupe_key=key,
                impact=Decimal(impact),
                currency="INR",
                confidence=0.9,
                engine_version="e",
                rule_version="r",
                model_version="none",
                summary=f"{code} on i1",
                details={
                    "engine": finding_model.TYPE_ENGINE[code],
                    "hold": code in finding_model.HOLD_TYPES,
                },
                citations=(Citation(node, "the invoice"), *cites),
            ),
        )
        return fid

    leak = finding(
        "T-LEAK", "T-LEAK:i1", cites=(Citation(clause, "INR 50 per unit"),)
    )
    pay = finding("T-PAY", "T-PAY:i1", impact="1000")

    def client(role):
        return TestClient(app, headers={"Authorization": f"Bearer {role}"})

    return {
        "client": client,
        "graph": graph,
        "findings": findings,
        "vendor": vendor,
        "invoice": invoice,
        "contract": contract,
        "clause": clause,
        "leak": leak,
        "pay": pay,
        "url": url,
    }


# -- list / summary -----------------------------------------------------------


def test_list_carries_vendor_engine_and_hold_and_pages(env):
    out = env["client"]("viewer").get("/api/trust/findings?limit=1").json()
    assert out["total"] == 2 and out["limit"] == 1 and out["offset"] == 0
    [row] = out["findings"]
    assert row["vendor"] == {"node": env["vendor"], "name": "Acme Steel"}
    assert row["engine"] in ("leakage", "payment") and isinstance(
        row["hold"], bool
    )
    assert "citations" not in row or row["citations"] == []


def test_list_filters_by_engine_hold_vendor_and_open(env):
    c = env["client"]("viewer")
    ids = lambda r: {f["id"] for f in r.json()["findings"]}  # noqa: E731
    assert ids(c.get("/api/trust/findings?engine=payment")) == {env["pay"]}
    assert ids(c.get("/api/trust/findings?hold=true")) == {env["pay"]}
    assert ids(c.get(f"/api/trust/findings?vendor={env['vendor']}")) == {
        env["leak"],
        env["pay"],
    }
    assert ids(c.get("/api/trust/findings?vendor=nope")) == set()
    c.post(
        f"/api/trust/findings/{env['leak']}/transition",
        json={"to_status": "triaged"},
    )
    assert ids(c.get("/api/trust/findings?open=true")) == {
        env["leak"],
        env["pay"],
    }
    assert (
        ids(c.get("/api/trust/findings?status=triaged")) == set()
    )  # viewer cannot transition
    assert c.get("/api/trust/findings?limit=500").json()["limit"] == 200


def test_summary_names_vendors(env):
    out = env["client"]("viewer").get("/api/trust/findings/summary").json()
    assert out["by_status"] == {"detected": 2}
    assert out["hold_open"] == 1
    assert out["vendors"] == [
        {"node": env["vendor"], "name": "Acme Steel", "count": 2}
    ]


# -- detail -------------------------------------------------------------------


def test_detail_resolves_citations_sources_lineage_and_transitions(env):
    out = (
        env["client"]("viewer").get(f"/api/trust/findings/{env['leak']}").json()
    )
    assert out["vendor"]["name"] == "Acme Steel"
    by_node = {c["node_id"]: c for c in out["citations"]}
    inv = by_node[env["invoice"]]
    assert inv["node"]["kind"] == "invoice"
    assert inv["source"] == {
        "document_url": "/api/invoices/case-1/document",
        "page": None,
    }
    clause = by_node[env["clause"]]
    assert clause["source"] == {
        "document_url": "/api/trust/contracts/doc-c/file",
        "page": 2,
    }
    assert out["transaction"]["node"]["id"] == env["invoice"]
    assert [link["stage"] for link in out["lineage"]][:2] == ["contract", "po"]
    assert out["lineage"][0]["status"] == "found"
    assert out["allowed_transitions"] == ["triaged"]
    assert (
        env["client"]("viewer").get("/api/trust/findings/nope").status_code
        == 404
    )


# -- transition ---------------------------------------------------------------


def test_clerk_triages_but_only_an_approver_decides_money(env):
    clerk, approver = env["client"]("clerk"), env["client"]("approver")
    path = f"/api/trust/findings/{env['leak']}/transition"
    r = clerk.post(path, json={"to_status": "triaged"})
    assert r.status_code == 200 and r.json()["status"] == "triaged"
    assert clerk.post(path, json={"to_status": "accepted"}).status_code == 403
    r = approver.post(path, json={"to_status": "accepted"})
    assert r.status_code == 200 and r.json()["owner_id"] == "approver@x.test"


def test_transition_errors_are_400_409_with_the_current_status(env):
    approver = env["client"]("approver")
    path = f"/api/trust/findings/{env['leak']}/transition"
    assert (
        approver.post(path, json={"to_status": "recovered"}).status_code == 409
    )
    approver.post(path, json={"to_status": "triaged"})
    r = approver.post(path, json={"to_status": "rejected"})
    assert r.status_code == 400 and "reason" in r.json()["detail"]
    r = approver.post(
        path, json={"to_status": "accepted", "expect": "detected"}
    )
    assert r.status_code == 409 and r.json()["detail"]["current"] == "triaged"
    r = approver.post(path, json={"to_status": "accepted", "amount": "abc"})
    assert r.status_code == 400


def test_recovered_amount_defaults_to_the_impact_and_is_stored(env):
    approver = env["client"]("approver")
    path = f"/api/trust/findings/{env['leak']}/transition"
    for status in ("triaged", "accepted", "claimed", "recovered"):
        assert (
            approver.post(path, json={"to_status": status}).status_code == 200
        )
    [row] = env["findings"].events(T, to_status=("recovered",))
    assert row["amount"] == "100.0000" and row["actor"] == "approver@x.test"


# -- nodes / lineage ----------------------------------------------------------


def test_node_route_returns_record_source_and_edges(env):
    out = (
        env["client"]("viewer").get(f"/api/trust/nodes/{env['invoice']}").json()
    )
    assert out["node"]["natural_key"] == "invoices:i1"
    assert out["source"]["document_url"] == "/api/invoices/case-1/document"
    kinds = {e["kind"] for e in out["edges_out"]}
    assert kinds == {"issued_by", "governed_by"} and out["edges_in"] == []
    assert (
        env["client"]("viewer").get("/api/trust/nodes/nope").status_code == 404
    )


def test_lineage_route(env):
    out = (
        env["client"]("viewer")
        .get(f"/api/trust/lineage/{env['invoice']}")
        .json()
    )
    assert out["node_id"] == env["invoice"]
    assert out["links"][0] == {
        "stage": "contract",
        "status": "found",
        "node_id": env["contract"],
        "note": "Governed by contract.",
    }
    assert (
        env["client"]("viewer").get("/api/trust/lineage/nope").status_code
        == 404
    )


# -- contracts / clauses ------------------------------------------------------


def test_contract_file_serves_only_contract_documents(env):
    c = env["client"]("viewer")
    r = c.get("/api/trust/contracts/doc-c/file")
    assert r.status_code == 200 and r.content == PDF
    assert r.headers["content-type"].startswith("application/pdf")
    with tenant_session(T, env["url"]) as s:
        s.add(
            models.Document(
                tenant_id=T,
                id="doc-i",
                sha256="i" * 64,
                storage_key="k",
                kind="invoice",
            )
        )
    assert c.get("/api/trust/contracts/doc-i/file").status_code == 404
    assert c.get("/api/trust/contracts/nope/file").status_code == 404


def test_clauses_list_and_review_keeps_the_contract_edge(env):
    viewer, approver = env["client"]("viewer"), env["client"]("approver")
    [clause] = viewer.get("/api/trust/contracts/doc-c/clauses").json()
    assert clause["node_id"] == env["clause"] and clause["reviewed"] is False
    assert clause["params"] == {"price": "50"} and clause["page"] == 2
    r = approver.patch(
        f"/api/trust/clauses/{env['clause']}", json={"params": {"price": "48"}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["reviewed"] is True and r.json()["params"] == {
        "price": "48"
    }
    assert r.json()["type"] == "rate" and r.json()["node_id"] != env["clause"]
    [again] = viewer.get("/api/trust/contracts/doc-c/clauses").json()
    assert again["node_id"] == r.json()["node_id"]
    assert (
        approver.patch(
            f"/api/trust/clauses/{env['clause']}", json={"type": "bogus"}
        ).status_code
        == 400
    )
    assert (
        approver.patch(
            "/api/trust/clauses/nope", json={"type": "rate"}
        ).status_code
        == 404
    )
    assert (
        env["client"]("clerk")
        .patch(f"/api/trust/clauses/{again['node_id']}", json={})
        .status_code
        == 403
    )


def test_routes_are_503_without_a_database(env, monkeypatch):
    monkeypatch.setattr(service, "_finding_store", None)
    monkeypatch.setattr(service, "_database_configured", lambda: False)
    assert env["client"]("viewer").get("/api/trust/findings").status_code == 503
