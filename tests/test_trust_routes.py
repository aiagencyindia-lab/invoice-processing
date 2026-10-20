"""The history upload routes: mapping proposal, import, listing."""

import json

import pytest
from fastapi.testclient import TestClient
from fresh_schema import fresh_schema

from invoice_processing.adapters import JsonlAuditSink, LocalBlobStore
from invoice_processing.api import auth, service
from invoice_processing.api.ingest import MAX_DOCUMENT_BYTES
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.repositories import PgDocumentRepository
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.graph.sync import sync_tenant
from invoice_processing.trust.history import mapping
from invoice_processing.trust.history.importer import HistoryImporter

TENANT = "tenant-acme"
CTX_ROUTES = RunContext(tenant_id=TENANT, case_id="", run_id="t", actor="t")
CSV = (
    b"Bill No,Supplier,Amount,Date\n"
    b"INV-1,Acme,100.50,2026-01-05\n"
    b"INV-2,Acme,oops,2026-01-06\n"
)
MAP = {
    "Bill No": "doc_number",
    "Supplier": "vendor_key",
    "Amount": "amount",
    "Date": "record_date",
}


class FakeVerifier:
    def verify(self, token):
        if token not in ("viewer", "clerk", "approver", "admin"):
            raise auth.AuthError("Invalid token")
        return {
            "sub": f"u-{token}",
            "email": f"{token}@x.test",
            "email_verified": True,
            "tenant_id": TENANT,
            "role": token,
        }


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("trust_routes.db")
    fresh_schema(url)
    with tenant_session(TENANT, url) as s:
        s.add(
            models.Tenant(id=TENANT, name="Acme", slug="acme", status="active")
        )
    monkeypatch.setattr(service, "_history_importer", HistoryImporter(url))
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(
        service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    return lambda token: TestClient(
        app, headers={"Authorization": f"Bearer {token}"}
    )


def _post(client, path, data, **form):
    return client.post(
        path, files={"file": ("ap.csv", data, "text/csv")}, data=form
    )


def test_mapping_route_proposes_and_returns_saved(env, monkeypatch):
    monkeypatch.setattr(
        mapping, "decide", lambda state, questions, decider=None: []
    )
    r = _post(
        env("clerk"),
        "/api/trust/history/mapping",
        CSV,
        kind="ap_invoice",
        source_name="tally",
    )
    assert r.status_code == 200
    assert r.json()["proposed"] == MAP and r.json()["saved"] is None


def test_import_route_returns_201_with_row_errors(env):
    r = _post(
        env("clerk"),
        "/api/trust/history/import",
        CSV,
        kind="ap_invoice",
        source_name="tally",
        mapping=json.dumps(MAP),
    )
    assert r.status_code == 201
    assert r.json()["rows_ok"] == 1 and r.json()["errors"][0]["row"] == 3
    again = _post(
        env("clerk"),
        "/api/trust/history/import",
        CSV,
        kind="ap_invoice",
        source_name="tally",
        mapping=json.dumps(MAP),
    )
    assert again.status_code == 200
    assert again.json()["duplicate_of"] == r.json()["import_id"]
    listed = env("viewer").get("/api/trust/history/imports")
    assert listed.status_code == 200 and listed.json()[0]["rows_bad"] == 1


def test_viewer_cannot_import(env):
    r = _post(
        env("viewer"),
        "/api/trust/history/import",
        CSV,
        kind="ap_invoice",
        source_name="t",
        mapping=json.dumps(MAP),
    )
    assert r.status_code == 403


def test_bad_mapping_is_400_in_words(env):
    r = _post(
        env("clerk"),
        "/api/trust/history/import",
        CSV,
        kind="ap_invoice",
        source_name="t",
        mapping=json.dumps({"Bill No": "doc_number"}),
    )
    assert r.status_code == 400
    assert "required field amount is not mapped" in r.json()["detail"]


def test_import_without_database_is_503(env, monkeypatch):
    monkeypatch.setattr(service, "_history_importer", None)
    monkeypatch.setattr(service, "_database_configured", lambda: False)
    r = _post(
        env("clerk"),
        "/api/trust/history/import",
        CSV,
        kind="ap_invoice",
        source_name="t",
        mapping=json.dumps(MAP),
    )
    assert r.status_code == 503 and "needs a database" in r.json()["detail"]


def test_oversized_and_non_multipart_uploads_are_refused(env):
    big = b"a,b\n" + b"x,y\n" * (6 * 1024 * 1024 // 4)
    r = _post(
        env("clerk"),
        "/api/trust/history/import",
        big,
        kind="ap_invoice",
        source_name="t",
        mapping="{}",
    )
    assert r.status_code == 413
    r = env("clerk").post(
        "/api/trust/history/import", json={"kind": "ap_invoice"}
    )
    assert r.status_code == 400 and "multipart" in r.json()["detail"]


def test_one_audit_row_per_import_and_lean_listing(env, tmp_path):
    _post(
        env("clerk"),
        "/api/trust/history/import",
        CSV,
        kind="ap_invoice",
        source_name="tally",
        mapping=json.dumps(MAP),
    )
    with tenant_session(TENANT) as s:  # DATABASE_URL from the fixture
        rows = s.query(models.AuditEvent).all()
    assert sum("trust_history" in r.action for r in rows) == 1
    [listed] = env("viewer").get("/api/trust/history/imports").json()
    assert listed["rows_bad"] == 1 and "errors" not in listed


PDF = b"%PDF-1.4\n% a contract\n"


@pytest.fixture
def contracts(env, tmp_path, monkeypatch, scratch_db):
    url = scratch_db("trust_routes.db")
    graph = GraphStore(url)
    monkeypatch.setattr(service, "_graph_store", graph)
    monkeypatch.setattr(service, "_documents", PgDocumentRepository(url))
    blobs = LocalBlobStore(tmp_path / "blobs")
    monkeypatch.setattr(service, "get_blobs", lambda tenant_id: blobs)
    queued = []

    class Queue:
        def publish(self, payload, **attributes):
            queued.append(payload)
            return "m"

    monkeypatch.setattr(service, "get_queue", Queue)
    with tenant_session(TENANT, url) as s:
        s.add(
            models.Vendor(
                tenant_id=TENANT,
                id="ven-1",
                name="Acme Steel",
                normalized_name="acme steel",
                tax_id="27AAPFU0939F1ZV",
            )
        )
    vendor_node = graph.upsert_node(
        CTX_ROUTES,
        kind="vendor",
        natural_key="vendors:ven-1",
        ref_table="vendors",
        ref_id="ven-1",
    )
    return {
        "client": env,
        "graph": graph,
        "blobs": blobs,
        "queued": queued,
        "vendor_node": vendor_node,
        "url": url,
    }


def _upload(client, data=PDF, vendor_id="ven-1", name="msa.pdf"):
    return client.post(
        "/api/trust/contracts",
        files={"file": (name, data, "application/pdf")},
        data={"vendor_id": vendor_id},
    )


def test_contract_upload_stores_document_node_and_governs_edge(contracts):
    r = _upload(contracts["client"]("clerk"))
    assert r.status_code == 201, r.text
    body = r.json()
    graph = contracts["graph"]
    node = graph.node(TENANT, body["contract_node"])
    assert node["kind"] == "contract" and node["ref_table"] == "documents"
    assert (
        node["attrs"]["vendor_id"] == "ven-1"
        and node["attrs"]["filename"] == "msa.pdf"
    )
    [edge] = graph.edges(TENANT, src=node["id"], kind="governs")
    assert edge["dst"] == contracts["vendor_node"]
    with tenant_session(TENANT, contracts["url"]) as s:
        doc = s.get(models.Document, body["document_id"])
        assert doc.kind == "contract" and doc.source == "upload"
    assert contracts["blobs"].get(doc.storage_key) == PDF
    assert contracts["queued"][-1]["type"] == "trust.evaluate"
    assert contracts["queued"][-1]["document_id"] == body["document_id"]
    again = _upload(contracts["client"]("clerk"))
    assert again.status_code == 200 and again.json()["duplicate"] is True


def test_contract_upload_refuses_non_pdf_and_unknown_vendor(contracts):
    client = contracts["client"]("clerk")
    assert _upload(client, data=b"not a pdf").status_code == 400
    r = _upload(client, vendor_id="nope")
    assert r.status_code == 404 and "vendor" in r.json()["detail"].lower()
    assert _upload(contracts["client"]("viewer")).status_code == 403


def test_contracts_list_is_tenant_scoped(contracts):
    _upload(contracts["client"]("clerk"))
    listed = contracts["client"]("viewer").get("/api/trust/contracts").json()
    assert [c["vendor_id"] for c in listed] == ["ven-1"]
    assert listed[0]["filename"] == "msa.pdf"


def test_the_same_contract_under_another_vendor_is_refused(contracts):
    client = contracts["client"]("clerk")
    with tenant_session(TENANT, contracts["url"]) as s:
        s.add(
            models.Vendor(
                tenant_id=TENANT,
                id="ven-2",
                name="Globex",
                normalized_name="globex",
                tax_id="29AABCU9603R1ZX",
            )
        )
    assert _upload(client).status_code == 201
    r = _upload(client, vendor_id="ven-2")
    assert r.status_code == 409 and "another vendor" in r.json()["detail"]


def test_oversized_contract_is_refused_before_reading(contracts):
    r = contracts["client"]("clerk").post(
        "/api/trust/contracts",
        files={
            "file": (
                "big.pdf",
                b"%PDF" + b"0" * (MAX_DOCUMENT_BYTES + 1),
                "application/pdf",
            )
        },
        data={"vendor_id": "ven-1"},
    )
    assert r.status_code == 413


def test_a_contract_for_an_unsynced_vendor_gets_its_edge_at_the_next_sync(
    contracts,
):
    with tenant_session(TENANT, contracts["url"]) as s:
        s.add(
            models.Vendor(
                tenant_id=TENANT,
                id="ven-3",
                name="Initech",
                normalized_name="initech",
                tax_id="24ITCCR9797D1ZQ",
            )
        )
    r = _upload(
        contracts["client"]("clerk"), vendor_id="ven-3", data=PDF + b"3"
    )
    node = r.json()["contract_node"]
    graph = contracts["graph"]
    assert graph.edges(TENANT, src=node, kind="governs") == []
    sync_tenant(
        CTX_ROUTES,
        graph,
        LinkStore(contracts["url"]),
        url=contracts["url"],
        decider=FakeDecider({}),
    )
    [edge] = graph.edges(TENANT, src=node, kind="governs")
    assert (
        edge["dst"] == graph.nodes_by_ref(TENANT, "vendors", ["ven-3"])["ven-3"]
    )
    assert edge["provenance"] == "human_assertion"
