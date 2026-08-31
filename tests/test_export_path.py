"""The export path: how an approved invoice reaches the accounting system.

Export is the first point where this system creates a liability in someone
else's ledger, so the tests that matter are the refusals:

* an invoice that was never approved must not export, because posting early
  puts an unreviewed liability on the books;
* an invoice already posted must not post twice, because a duplicate bill is a
  duplicate payment;
* a rejected invoice must not export at all.

Approval is read from durable workflow state, not from an audit event. An audit
event records that something happened; it is not a fact the system can act on.

Nothing here reaches Xero -- the transport is a fake.
"""

import csv
import io
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from invoice_processing import paths
from invoice_processing.adapters import FsCaseRepository, JsonlAuditSink
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.connectors.xero import (
    ExportResult,
    HttpTransport,
    Transport,
)

TENANT = "tenant-acme"
CLAIMS = {
    "boss": {
        "sub": "u-1",
        "email": "approver@acme.test",
        "email_verified": True,
        "tenant_id": TENANT,
        "role": "approver",
    },
    "clerk": {
        "sub": "u-2",
        "email": "clerk@acme.test",
        "email_verified": True,
        "tenant_id": TENANT,
        "role": "clerk",
    },
}


class FakeVerifier:
    def verify(self, token):
        if token not in CLAIMS:
            raise auth.AuthError("Invalid token")
        return CLAIMS[token]


@pytest.fixture
def client(tmp_path, monkeypatch):
    root = tmp_path / "agent_output"
    shutil.copytree(paths.AGENTIC_FLOW_OUT, root)
    monkeypatch.setattr(
        service, "_cases", FsCaseRepository(root, tenant_id=TENANT)
    )
    monkeypatch.setattr(
        service, "_corrections_path", tmp_path / "corrections.jsonl"
    )
    monkeypatch.setattr(
        service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.delenv("XERO_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("XERO_TENANT_ID", raising=False)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    return TestClient(app, headers={"Authorization": "Bearer boss"})


def approve(client, case_id="case_002"):
    return client.post(
        f"/api/invoices/{case_id}/actions", json={"action": "approve"}
    )


class TestApprovalIsRequired:
    def test_an_unapproved_invoice_cannot_export(self, client):
        """Posting before approval puts an unreviewed liability on the ledger."""
        response = client.post(
            "/api/invoices/case_002/export", json={"target": "csv"}
        )
        assert response.status_code == 409
        assert "not approved" in response.json()["detail"]

    def test_an_approved_invoice_exports(self, client):
        approve(client)
        response = client.post(
            "/api/invoices/case_002/export", json={"target": "csv"}
        )
        assert response.status_code == 200

    def test_a_rejected_invoice_cannot_export(self, client):
        client.post(
            "/api/invoices/case_002/actions",
            json={"action": "reject", "reason": "duplicate"},
        )
        response = client.post(
            "/api/invoices/case_002/export", json={"target": "csv"}
        )
        assert response.status_code == 409

    def test_approval_is_recorded_durably_not_only_audited(self, client):
        """An audit event is a record, not a fact the system can act on."""
        approve(client)
        artifacts = service._cases.get_artifacts(
            auth.Principal(
                "u", "approver@acme.test", TENANT, "approver"
            ).context("case_002")
        )
        state = artifacts["workflow"]
        assert state["decision"] == "approve"
        assert state["decided_by"] == "approver@acme.test"
        assert state["decided_at"]


class TestPermissions:
    def test_a_clerk_cannot_export(self, client):
        """Export creates a payable; it needs approval authority."""
        approve(client)
        response = client.post(
            "/api/invoices/case_002/export",
            json={"target": "csv"},
            headers={"Authorization": "Bearer clerk"},
        )
        assert response.status_code == 403

    def test_export_requires_authentication(self, client):
        response = client.post(
            "/api/invoices/case_002/export",
            json={"target": "csv"},
            headers={"Authorization": ""},
        )
        assert response.status_code == 401

    def test_unknown_case_is_404(self, client):
        response = client.post(
            "/api/invoices/nope/export", json={"target": "csv"}
        )
        assert response.status_code == 404


class TestCsvExport:
    def test_returns_a_parseable_row(self, client):
        approve(client)
        body = client.post(
            "/api/invoices/case_002/export", json={"target": "csv"}
        ).json()
        rows = list(csv.reader(io.StringIO(body["csv"])))
        assert rows[0] == service.CSV_COLUMNS
        assert len(rows) == 2

    def test_carries_the_fields_an_accountant_needs(self, client):
        approve(client)
        body = client.post(
            "/api/invoices/case_002/export", json={"target": "csv"}
        ).json()
        header, row = list(csv.reader(io.StringIO(body["csv"])))
        record = dict(zip(header, row, strict=True))
        assert record["vendor_name"]
        assert record["invoice_number"]
        assert record["total_inc_tax"]

    def test_export_is_audited(self, client, tmp_path):
        approve(client)
        client.post("/api/invoices/case_002/export", json={"target": "csv"})
        events = [
            json.loads(line)
            for line in (tmp_path / "audit.jsonl")
            .read_text()
            .strip()
            .splitlines()
        ]
        assert events[-1]["event_type"] == "invoice_exported"

    def test_an_unknown_target_is_rejected(self, client):
        approve(client)
        response = client.post(
            "/api/invoices/case_002/export", json={"target": "sap"}
        )
        assert response.status_code == 400


class TestXeroExport:
    def test_unconfigured_xero_is_503_not_500(self, client):
        """'Not connected' is a setup state, not a server fault."""
        approve(client)
        response = client.post(
            "/api/invoices/case_002/export", json={"target": "xero"}
        )
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"]

    def test_a_posted_invoice_is_not_posted_again(self, client, monkeypatch):
        """A duplicate bill is a duplicate payment."""
        approve(client)

        posts = []

        class OnceExporter:
            def export(self, invoice, *, approved, external_id=None):
                if external_id:
                    return ExportResult(
                        external_id=external_id, status="already_posted"
                    )
                posts.append(invoice)
                return ExportResult(external_id="XERO-1", status="created")

        monkeypatch.setattr(
            service, "get_exporter", lambda tenant: OnceExporter()
        )

        first = client.post(
            "/api/invoices/case_002/export", json={"target": "xero"}
        ).json()
        second = client.post(
            "/api/invoices/case_002/export", json={"target": "xero"}
        ).json()

        assert first["status"] == "created"
        assert second["status"] == "already_posted"
        assert second["external_id"] == "XERO-1"
        assert len(posts) == 1, "the invoice was posted to Xero twice"

    def test_the_external_id_is_persisted(self, client, monkeypatch):
        """It is what stops a second post after a restart."""
        approve(client)

        class Exporter:
            def export(self, invoice, *, approved, external_id=None):
                return ExportResult(external_id="XERO-9", status="created")

        monkeypatch.setattr(service, "get_exporter", lambda tenant: Exporter())
        client.post("/api/invoices/case_002/export", json={"target": "xero"})

        ctx = auth.Principal("u", "a@b.c", TENANT, "approver").context(
            "case_002"
        )
        assert (
            service._cases.get_artifacts(ctx)["workflow"]["external_id"]
            == "XERO-9"
        )

    def test_a_failed_export_is_502_and_audited(
        self, client, monkeypatch, tmp_path
    ):
        approve(client)

        class Broken:
            def export(self, invoice, *, approved, external_id=None):
                return ExportResult(
                    status="failed", message="Xero rejected the bill"
                )

        monkeypatch.setattr(service, "get_exporter", lambda tenant: Broken())
        response = client.post(
            "/api/invoices/case_002/export", json={"target": "xero"}
        )
        assert response.status_code == 502

        events = [
            json.loads(line)
            for line in (tmp_path / "audit.jsonl")
            .read_text()
            .strip()
            .splitlines()
        ]
        assert events[-1]["event_type"] == "invoice_export_failed"

    def test_a_failed_export_records_no_external_id(self, client, monkeypatch):
        """Recording one would permanently block a legitimate retry."""
        approve(client)

        class Broken:
            def export(self, invoice, *, approved, external_id=None):
                return ExportResult(status="failed", message="nope")

        monkeypatch.setattr(service, "get_exporter", lambda tenant: Broken())
        client.post("/api/invoices/case_002/export", json={"target": "xero"})

        ctx = auth.Principal("u", "a@b.c", TENANT, "approver").context(
            "case_002"
        )
        assert (
            "external_id" not in service._cases.get_artifacts(ctx)["workflow"]
        )


class TestTransport:
    def test_the_connector_has_a_real_http_implementation(self):
        """The Protocol existed from the start; nothing implemented it."""
        assert isinstance(HttpTransport(), Transport)

    def test_the_client_secret_is_never_handled(self):
        """Only an access token reaches this path; the long-lived secret must not."""
        source = (paths.AGENT_PKG_DIR / "connectors" / "xero.py").read_text(
            encoding="utf-8"
        )
        assert "client_secret" not in source
