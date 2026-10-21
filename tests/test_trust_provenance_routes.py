"""Vendor KYC documents and on-demand authenticity scores (FR-22.3)."""

import json
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from fresh_schema import fresh_schema

from benchmark.history.faults.provenance import make_pdf
from invoice_processing.adapters import JsonlAuditSink, LocalBlobStore
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.repositories import PgDocumentRepository
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.store import GraphStore
from tests.test_trust_explain_routes import FakeVerifier

# Every path this module adds; test_tenant_isolation_api unions it into its
# coverage check, and the probes below exercise each one across tenants.
PROBED = {
    "/api/vendors/{vendor_id}/documents",
    "/api/trust/documents/{document_id}/authenticity",
}

ACME, GLOBEX = "tenant-acme", "tenant-globex"
PDF = make_pdf()


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("provenance_routes.db")
    fresh_schema(url)
    graph = GraphStore(url)
    for tid, slug, vendor in ((ACME, "acme", "v1"), (GLOBEX, "globex", "g1")):
        with tenant_session(tid, url) as s:
            s.add(models.Tenant(id=tid, name=slug, slug=slug, status="active"))
            s.flush()
            s.add(
                models.Vendor(
                    tenant_id=tid,
                    id=vendor,
                    name=f"{slug} vendor",
                    normalized_name=f"{slug} vendor",
                    tax_id=f"27AAPFU0939F1Z{'V' if slug == 'acme' else 'W'}",
                )
            )
        graph.upsert_node(
            RunContext(tenant_id=tid, case_id="", run_id="r", actor="t"),
            kind="vendor",
            natural_key=f"vendors:{vendor}",
            ref_table="vendors",
            ref_id=vendor,
            attrs={"name": f"{slug} vendor"},
        )
    queued = []

    class Queue:
        def publish(self, payload, **attributes):
            queued.append(payload)
            return "m"

    monkeypatch.setattr(service, "_graph_store", graph)
    monkeypatch.setattr(service, "_documents", PgDocumentRepository(url))
    monkeypatch.setattr(
        service, "get_blobs", lambda tid: LocalBlobStore(tmp_path / "b" / tid)
    )
    monkeypatch.setattr(service, "get_queue", Queue)
    monkeypatch.setattr(
        service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)

    def client(token):
        return TestClient(app, headers={"Authorization": f"Bearer {token}"})

    return {
        "client": client,
        "graph": graph,
        "url": url,
        "queued": queued,
        "audit": tmp_path / "audit.jsonl",
    }


def upload(c, vendor="v1", data=PDF, subkind="gst_certificate", name="gst.pdf"):
    return c.post(
        f"/api/vendors/{vendor}/documents",
        files={"file": (name, data, "application/pdf")},
        data={"subkind": subkind},
    )


def test_upload_stores_a_kyc_document_and_queues_it(env):
    r = upload(env["client"]("acme:clerk"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["vendor_id"] == "v1" and body["subkind"] == "gst_certificate"
    assert body["duplicate"] is False
    with tenant_session(ACME, env["url"]) as s:
        doc = s.get(models.Document, body["document_id"])
        assert doc.kind == "kyc" and doc.source == "upload"
        assert doc.filename == "gst.pdf"
    v = env["graph"].node(ACME, body["version_node"])
    assert v["attrs"]["kind"] == "kyc"
    assert v["attrs"]["uploaded_by_role"] == "clerk"
    [edge] = env["graph"].edges(ACME, src=v["id"], kind="evidences")
    assert env["graph"].node(ACME, edge["dst"])["ref_id"] == "v1"
    assert [q["document_id"] for q in env["queued"]] == [body["document_id"]]
    events = [
        json.loads(line) for line in env["audit"].read_text().splitlines()
    ]
    assert any(
        e.get("event_type") == "trust_vendor_document_uploaded" for e in events
    )


@pytest.mark.parametrize(
    "token,kw,status",
    [
        ("acme:viewer", {}, 403),
        ("acme:clerk", {"subkind": "passport"}, 400),
        ("acme:clerk", {"vendor": "nope"}, 404),
        ("acme:clerk", {"data": b"GIF89a"}, 400),
    ],
)
def test_upload_refusals(env, token, kw, status):
    r = upload(env["client"](token), **kw)
    assert r.status_code == status, r.text
    assert env["queued"] == []


def test_the_same_bytes_again_are_a_duplicate_not_a_new_document(env):
    c = env["client"]("acme:clerk")
    first = upload(c).json()
    again = upload(c)
    assert again.status_code == 200
    assert again.json()["document_id"] == first["document_id"]
    assert again.json()["duplicate"] is True
    # queued again: the first enqueue may have failed
    assert [q["document_id"] for q in env["queued"]] == [
        first["document_id"]
    ] * 2
    assert upload(c, subkind="pan").status_code == 409
    with tenant_session(ACME, env["url"]) as s:
        s.add(
            models.Vendor(
                tenant_id=ACME,
                id="v2",
                name="b",
                normalized_name="b",
                tax_id="29AAPFU0939F1ZV",
            )
        )
    assert upload(c, vendor="v2").status_code == 409


def test_a_kyc_row_left_without_its_node_is_planted_on_reupload(env):
    import hashlib  # noqa: PLC0415

    with tenant_session(ACME, env["url"]) as s:
        s.add(
            models.Document(
                tenant_id=ACME,
                id="k-orphan",
                sha256=hashlib.sha256(PDF).hexdigest(),
                storage_key="documents/x.pdf",
                kind="kyc",
            )
        )
    r = upload(env["client"]("acme:clerk"))
    assert r.status_code == 200, r.text
    assert r.json()["document_id"] == "k-orphan"
    v = env["graph"].node(ACME, r.json()["version_node"])
    assert v["attrs"]["vendor_id"] == "v1"
    assert v["attrs"]["subkind"] == "gst_certificate"


def test_bytes_received_as_an_invoice_are_refused(env):
    with tenant_session(ACME, env["url"]) as s:
        import hashlib  # noqa: PLC0415

        s.add(
            models.Document(
                tenant_id=ACME,
                id="inv-doc",
                sha256=hashlib.sha256(PDF).hexdigest(),
                storage_key="documents/x.pdf",
            )
        )
    assert upload(env["client"]("acme:clerk")).status_code == 409


def test_list_shows_each_document_with_its_latest_score(env):
    c = env["client"]("acme:clerk")
    doc = upload(c).json()["document_id"]
    upload(c, data=make_pdf(font="Courier"), subkind="pan", name="pan.pdf")
    rows = env["client"]("acme:viewer").get("/api/vendors/v1/documents")
    assert rows.status_code == 200
    docs = rows.json()["documents"]
    assert [(r["subkind"], r["filename"], r["score"]) for r in docs] == [
        ("gst_certificate", "gst.pdf", None),
        ("pan", "pan.pdf", None),
    ]
    # the frontend's VendorDocument shape, and nothing else
    assert set(docs[0]) == {
        "document_id",
        "subkind",
        "filename",
        "score",
        "uploaded_at",
    }
    assert datetime.fromisoformat(docs[0]["uploaded_at"])
    scored = env["client"]("acme:viewer").post(
        f"/api/trust/documents/{doc}/authenticity"
    )
    assert scored.status_code == 200, scored.text
    [gst] = [
        r
        for r in env["client"]("acme:viewer")
        .get("/api/vendors/v1/documents")
        .json()["documents"]
        if r["document_id"] == doc
    ]
    assert gst["score"] == scored.json()["attrs"]["score"] > 0.5
    assert gst["uploaded_at"] == docs[0]["uploaded_at"]  # not the scoring time


def test_authenticity_scores_now_and_writes_no_finding(env):
    doc = upload(env["client"]("acme:clerk")).json()["document_id"]
    r = env["client"]("acme:viewer").post(
        f"/api/trust/documents/{doc}/authenticity"
    )
    body = r.json()
    assert body["document_id"] == doc
    assert body["attrs"]["engine_version"] == "provenance-2"
    assert body["attrs"]["subkind"] == "gst_certificate"
    assert set(body["attrs"]["components"]) == {
        "channel",
        "template",
        "text",
        "meta",
        "producer",
        "edits",
        "fonts",
    }
    assert env["graph"].node(ACME, body["node_id"])["attrs"] == body["attrs"]
    assert FindingStore(env["url"]).list(ACME) == []


def test_authenticity_of_an_invoice_syncs_it_first(env, tmp_path):
    sha = "a" * 64
    LocalBlobStore(tmp_path / "b" / ACME).put(f"documents/{sha}.pdf", PDF)
    with tenant_session(ACME, env["url"]) as s:
        s.add(
            models.Document(
                tenant_id=ACME,
                id="d-inv",
                sha256=sha,
                storage_key=f"documents/{sha}.pdf",
                source="api",
            )
        )
        s.flush()
        s.add(
            models.Invoice(
                tenant_id=ACME,
                id="i1",
                document_id="d-inv",
                case_id="case-1",
                invoice_number="INV-1",
                vendor_name="acme vendor",
                total_inc_tax=Decimal("100.00"),
                currency="INR",
                status="done",
            )
        )
    r = env["client"]("acme:viewer").post(
        "/api/trust/documents/d-inv/authenticity"
    )
    assert r.status_code == 200, r.text
    assert r.json()["attrs"]["components"]["channel"] == 0.8  # api
    [edge] = env["graph"].edges(ACME, src=r.json()["node_id"], kind="evidences")
    assert env["graph"].node(ACME, edge["dst"])["kind"] == "invoice"


def test_authenticity_refusals(env, tmp_path):
    c = env["client"]("acme:viewer")
    assert c.post("/api/trust/documents/nope/authenticity").status_code == 404
    with tenant_session(ACME, env["url"]) as s:
        s.add(
            models.Document(
                tenant_id=ACME,
                id="d-c",
                sha256="c" * 64,
                storage_key="contracts/c.pdf",
                kind="contract",
            )
        )
        s.add(
            models.Document(
                tenant_id=ACME,
                id="d-png",
                sha256="d" * 64,
                storage_key="documents/png.pdf",
                kind="kyc",
            )
        )
    LocalBlobStore(tmp_path / "b" / ACME).put("documents/png.pdf", b"\x89PNG")
    assert c.post("/api/trust/documents/d-c/authenticity").status_code == 409
    assert c.post("/api/trust/documents/d-png/authenticity").status_code == 422


# -- two-tenant probes ---------------------------------------------------------


def test_a_tenant_cannot_upload_to_another_tenants_vendor(env):
    assert upload(env["client"]("globex:clerk"), vendor="v1").status_code == 404
    assert upload(env["client"]("acme:clerk"), vendor="g1").status_code == 404
    assert env["queued"] == []


def test_a_tenant_cannot_list_another_tenants_vendor_documents(env):
    upload(env["client"]("acme:clerk"))
    upload(
        env["client"]("globex:clerk"),
        vendor="g1",
        data=make_pdf(font="Courier"),
    )
    r = env["client"]("globex:viewer").get("/api/vendors/v1/documents")
    assert r.status_code == 404
    mine = (
        env["client"]("acme:viewer")
        .get("/api/vendors/v1/documents")
        .json()["documents"]
    )
    assert len(mine) == 1 and mine[0]["filename"] == "gst.pdf"


def test_a_tenant_cannot_score_another_tenants_document(env):
    doc = upload(env["client"]("acme:clerk")).json()["document_id"]
    r = env["client"]("globex:viewer").post(
        f"/api/trust/documents/{doc}/authenticity"
    )
    assert r.status_code == 404
    v = env["graph"].nodes(ACME, kind="document_version")
    assert all("score" not in n["attrs"] for n in v)
