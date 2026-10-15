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
from golden import GOLDEN_CASES

from invoice_processing import paths
from invoice_processing.adapters import (
    FsCaseRepository,
    JsonlAuditSink,
    JsonlCorrectionStore,
)
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.connectors.xero import (
    ExportResult,
    HttpTransport,
    Transport,
)
from invoice_processing.db import models
from invoice_processing.db.repositories import PgApprovalRepository
from invoice_processing.db.session import (
    create_all,
    get_engine,
    tenant_session,
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
    shutil.copytree(GOLDEN_CASES, root)
    monkeypatch.setattr(
        service, "_cases", FsCaseRepository(root, tenant_id=TENANT)
    )
    monkeypatch.setattr(
        service,
        "_corrections",
        JsonlCorrectionStore(tmp_path / "corrections.jsonl"),
    )
    monkeypatch.setattr(
        service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.delenv("XERO_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("XERO_TENANT_ID", raising=False)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())

    # Start every test from "not yet reviewed". Approval is persisted as an
    # artifact, so a case approved by hand -- in a browser, or by an earlier
    # run -- would otherwise be copied in and make the refusal tests vacuous.
    for case in root.iterdir():
        (case / "workflow.json").unlink(missing_ok=True)

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


def export_csv(client) -> list[dict]:
    body = client.post(
        "/api/invoices/case_002/export", json={"target": "csv"}
    ).json()
    header, *rows = list(csv.reader(io.StringIO(body["csv"])))
    assert header == service.CSV_COLUMNS
    return [dict(zip(header, row, strict=True)) for row in rows]


def rewrite_extraction(edit):
    path = service._cases.root / "case_002" / "02_extraction.json"
    data = json.loads(path.read_text())
    edit(data["invoice"])
    path.write_text(json.dumps(data))


class TestCsvExport:
    def test_header_is_invoice_columns_then_line_columns(self):
        assert service.CSV_COLUMNS == [
            "case_id",
            "vendor_name",
            "vendor_tax_id",
            "invoice_number",
            "invoice_date",
            "due_date",
            "currency",
            "subtotal_ex_tax",
            "tax_total",
            "total_inc_tax",
            "po_number",
            "payment_terms",
            "line_number",
            "line_description",
            "quantity",
            "unit_price",
            "line_amount_ex_tax",
            "line_tax",
            "gl_account",
        ]

    def test_one_row_per_line_repeating_the_invoice(self, client):
        approve(client)
        rows = export_csv(client)
        assert [r["line_number"] for r in rows] == ["1", "2", "3"]
        assert {r["invoice_number"] for r in rows} == {
            rows[0]["invoice_number"]
        }
        assert (
            rows[1]["line_description"] == "Air filter replacement (x2 units)"
        )
        assert (rows[1]["quantity"], rows[1]["unit_price"]) == ("2.0", "45.0")
        assert (rows[1]["line_amount_ex_tax"], rows[1]["line_tax"]) == (
            "90.0",
            "9.0",
        )

    def test_an_invoice_without_lines_is_one_row(self, client):
        rewrite_extraction(lambda inv: inv.update(line_items=[]))
        approve(client)
        rows = export_csv(client)
        assert len(rows) == 1
        assert rows[0]["invoice_number"]
        assert all(rows[0][c] == "" for c in service.LINE_COLUMNS)

    def test_formula_like_text_is_neutralised(self, client):
        """Descriptions are untrusted extraction text; a cell starting with
        = + - @ is a formula to Excel."""

        def edit(inv):
            inv["vendor_name"] = "=HYPERLINK(1)"
            inv["line_items"][0]["description"] = "@SUM(A1)"
            inv["line_items"][1]["description"] = "-2+3"
            inv["line_items"][2]["amount_ex_tax"] = -85.0

        rewrite_extraction(edit)
        approve(client)
        rows = export_csv(client)
        assert rows[0]["vendor_name"] == "'=HYPERLINK(1)"
        assert rows[0]["line_description"] == "'@SUM(A1)"
        assert rows[1]["line_description"] == "'-2+3"
        assert rows[2]["line_amount_ex_tax"] == "-85.0"

    def test_carries_the_fields_an_accountant_needs(self, client):
        approve(client)
        record = export_csv(client)[0]
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


def code_line_one(client, account="6100"):
    """Put a GL account on case_002's first line, as the coder would."""
    path = service._cases.root / "case_002" / "07_transformation.json"
    data = json.loads(path.read_text())
    data["line_items_mapped"][0]["gl_account"] = account
    path.write_text(json.dumps(data))


class TestGlAccountInExports:
    def test_each_csv_row_carries_its_own_lines_account(self, client):
        code_line_one(client)
        approve(client)
        assert [r["gl_account"] for r in export_csv(client)] == ["6100", "", ""]

    def test_uncoded_invoice_has_empty_gl_cells(self, client):
        approve(client)
        assert [r["gl_account"] for r in export_csv(client)] == ["", "", ""]

    def test_xero_receives_the_line_gl_account(self, client, monkeypatch):
        code_line_one(client)
        approve(client)
        posted = []

        class Exporter:
            def export(self, invoice, *, approved, external_id=None):
                posted.append(invoice)
                return ExportResult(external_id="XERO-2", status="created")

        monkeypatch.setattr(service, "get_exporter", lambda tenant: Exporter())
        client.post("/api/invoices/case_002/export", json={"target": "xero"})
        lines = posted[0]["line_items"]
        assert lines[0]["gl_account"] == "6100"
        assert lines[1].get("gl_account") is None

    @pytest.mark.parametrize("change", ["description", "count"])
    def test_unpaired_lines_export_no_accounts(self, client, tmp_path, change):
        """Lines replaced after coding no longer match the projected lines;
        exporting the old accounts against them would post to the wrong GL."""
        code_line_one(client)

        def edit(inv):
            if change == "description":
                inv["line_items"][0]["description"] = "Something else entirely"
            else:
                inv["line_items"].append({"description": "Extra"})

        rewrite_extraction(edit)
        approve(client)
        rows = export_csv(client)
        assert len(rows) == (3 if change == "description" else 4)
        assert {r["gl_account"] for r in rows} == {""}
        events = [
            json.loads(line)
            for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        ]
        assert "gl_export_unpaired" in [e["event_type"] for e in events]


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


class TestXeroCredentialsBelongToOneTenant:
    """`get_exporter(tenant_id)` took a tenant and ignored it.

    The credentials are two process-wide environment variables, so they name
    exactly one Xero organisation. On a deployment with two tenants, both
    would have posted their approved bills into that same organisation: one
    customer's invoices in another customer's ledger.
    """

    def _configured(self, monkeypatch):
        monkeypatch.setenv("XERO_ACCESS_TOKEN", "token")
        monkeypatch.setenv("XERO_TENANT_ID", "xero-org-1")

    def test_the_named_owner_gets_the_exporter(self, monkeypatch):
        self._configured(monkeypatch)
        monkeypatch.setenv("XERO_OWNER_TENANT", "tenant-acme")

        assert service.get_exporter("tenant-acme") is not None

    def test_another_tenant_does_not(self, monkeypatch):
        self._configured(monkeypatch)
        monkeypatch.setenv("XERO_OWNER_TENANT", "tenant-acme")

        assert service.get_exporter("tenant-globex") is None

    def test_a_single_tenant_deployment_still_works_unnamed(self, monkeypatch):
        """The common case: one customer, no owner configured."""
        self._configured(monkeypatch)
        monkeypatch.delenv("XERO_OWNER_TENANT", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)

        assert service.get_exporter("tenant-acme") is not None

    def test_unconfigured_is_still_none(self, monkeypatch):
        monkeypatch.delenv("XERO_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("XERO_TENANT_ID", raising=False)

        assert service.get_exporter("tenant-acme") is None


def test_completion_of_a_chain_still_exports(client, scratch_db, monkeypatch):
    """Export reads the workflow keys chain completion writes, nothing else."""
    url = scratch_db("chain.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(TENANT, url) as session:
        session.add(
            models.Tenant(id=TENANT, name="Acme", slug="acme", status="active")
        )
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(service, "_approvals", PgApprovalRepository(url))

    response = approve(client)
    assert response.status_code == 200 and response.json()["completed"] is True
    exported = client.post(
        "/api/invoices/case_002/export", json={"target": "csv"}
    )
    assert exported.status_code == 200


class TestPaymentHold:
    def test_approval_without_a_chain_is_refused_while_held(
        self, client, monkeypatch
    ):
        from invoice_processing.trust import gate  # noqa: PLC0415

        monkeypatch.setattr(
            gate, "payment_hold", lambda t, c, **k: "Payment is held: PAY-X."
        )
        r = approve(client)
        assert r.status_code == 409 and "PAY-X" in r.json()["detail"]

    def test_export_is_409_while_held(self, client, monkeypatch):
        from invoice_processing.trust import gate  # noqa: PLC0415

        assert approve(client).status_code == 200
        monkeypatch.setattr(
            gate, "payment_hold", lambda t, c, **k: "Payment is held: PAY-X."
        )
        r = client.post("/api/invoices/case_002/export", json={"target": "csv"})
        assert r.status_code == 409 and "PAY-X" in r.json()["detail"]

    def test_an_admin_override_at_approval_carries_to_export(
        self, client, monkeypatch
    ):
        from invoice_processing.trust import gate  # noqa: PLC0415

        monkeypatch.setattr(
            gate, "payment_hold", lambda t, c, **k: "Payment is held: PAY-X."
        )
        monkeypatch.setitem(CLAIMS["boss"], "role", "admin")
        r = client.post(
            "/api/invoices/case_002/actions",
            json={
                "action": "approve",
                "override_reason": "Called the vendor on file",
            },
        )
        assert r.status_code == 200, r.text
        out = client.post(
            "/api/invoices/case_002/export", json={"target": "csv"}
        )
        assert out.status_code == 200, out.text

    def test_a_hold_raised_after_an_override_still_blocks_export(
        self, client, monkeypatch
    ):
        from invoice_processing.trust import gate  # noqa: PLC0415

        monkeypatch.setitem(CLAIMS["boss"], "role", "admin")
        monkeypatch.setattr(
            gate, "payment_hold", lambda t, c, **k: "Payment is held: PAY-X."
        )
        client.post(
            "/api/invoices/case_002/actions",
            json={
                "action": "approve",
                "override_reason": "Called the vendor on file",
            },
        )
        monkeypatch.setattr(
            gate, "payment_hold", lambda t, c, **k: "Payment is held: PAY-Y."
        )
        out = client.post(
            "/api/invoices/case_002/export", json={"target": "csv"}
        )
        assert out.status_code == 409 and "PAY-Y" in out.json()["detail"]
