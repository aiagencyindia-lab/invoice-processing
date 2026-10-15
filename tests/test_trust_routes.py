"""The history upload routes: mapping proposal, import, listing."""

import json

import pytest
from fastapi.testclient import TestClient

from invoice_processing.adapters import JsonlAuditSink
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.trust.history import mapping
from invoice_processing.trust.history.importer import HistoryImporter

TENANT = "tenant-acme"
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
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
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
