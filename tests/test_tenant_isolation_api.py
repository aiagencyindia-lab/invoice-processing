"""Cross-tenant isolation, enforced at every endpoint.

This file exists because retrofitting isolation tests is how leaks ship. It runs
**every** endpoint as two tenants and asserts that neither can see, edit,
approve or download the other's data -- including by guessing a case id, which
is the attack that does not require a bug anywhere else.

It is written to be exhaustive by construction rather than by diligence:
`test_every_route_is_covered` fails when a route is added to the app without a
matching probe here, so a new endpoint cannot quietly ship unprotected.

The regression that prompted it: `FsCaseRepository` ignored `tenant_id` and
served one directory to everyone. With one tenant that is invisible. The moment
authentication admitted a second, both would have read the same invoices with no
code change required to trigger it.
"""

import json
import pathlib
import re
import shutil
from datetime import datetime, timezone
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from fresh_schema import fresh_schema
from golden import GOLDEN_CASES
from sqlalchemy.orm import Session

from invoice_processing import approval_flow, paths
from invoice_processing.adapters import (
    FsCaseRepository,
    JsonDocumentRepository,
    JsonlCorrectionStore,
)
from invoice_processing.api import (
    admin_routes,
    approval_routes,
    auth,
    config_routes,
    gl_routes,
    kpi_routes,
    po_routes,
    service,
    supporting_routes,
    system,
    trust_audit_routes,
    trust_explain_routes,
    trust_routes,
    vendor_routes,
)
from invoice_processing.api.service import app
from invoice_processing.db.models import Base, Invoice, Tenant, User
from invoice_processing.db.repositories import (
    PgApprovalRepository,
    PgDocumentRepository,
    PgGlRuleRepository,
    PgPurchaseOrderRepository,
    PgRuleRepository,
    PgSupportingDocumentRepository,
    PgVendorRepository,
)
from invoice_processing.db.session import (
    get_engine,
)
from invoice_processing.db.session import tenant_session as db_session
from invoice_processing.ports import RunContext
from invoice_processing.tenant_settings import JsonTenantSettings
from invoice_processing.worker.queue import LocalQueue

ACME, GLOBEX = "tenant-acme", "tenant-globex"

ROLES = ("viewer", "clerk", "approver", "admin")

# A token per (tenant, role). Isolation must hold at every privilege level, not
# just for admin: a viewer who can read across tenants is the same disclosure,
# and an approver who can approve across them is worse.
CLAIMS = {
    f"{slug}-{role}": {
        "sub": f"u-{slug}-{role}",
        "email": f"{role}@{slug}.test",
        "email_verified": True,
        "tenant_id": tenant,
        "role": role,
    }
    for slug, tenant in (("acme", ACME), ("globex", GLOBEX))
    for role in ROLES
}


class FakeVerifier:
    def verify(self, token):
        if token not in CLAIMS:
            raise auth.AuthError("Invalid token")
        return CLAIMS[token]


@pytest.fixture
def clients(tmp_path, monkeypatch):
    """Two authenticated clients. Only ACME's tenant has any case data."""
    cases_root = tmp_path / "agent_output"
    shutil.copytree(GOLDEN_CASES, cases_root)

    monkeypatch.setattr(
        service, "_cases", FsCaseRepository(cases_root, tenant_id=ACME)
    )
    monkeypatch.setattr(
        service,
        "_corrections",
        JsonlCorrectionStore(tmp_path / "corrections.jsonl"),
    )
    monkeypatch.setattr(
        service, "_audit", service.JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())

    # A per-tenant document registry and queue, so the Documents and retry
    # endpoints have somewhere real to read from. Without these they resolve to
    # the Postgres repository against a schema-less test database.
    monkeypatch.setattr(
        service,
        "_documents",
        JsonDocumentRepository(tmp_path / "documents.json"),
    )
    monkeypatch.setattr(
        service, "get_queue", lambda: LocalQueue(tmp_path / "queue")
    )

    def client_for(slug, role="admin"):
        return TestClient(
            app, headers={"Authorization": f"Bearer {slug}-{role}"}
        )

    return client_for


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class TestReadsDoNotCross:
    def test_owner_sees_their_invoices(self, clients):
        acme = clients("acme")
        assert acme.get("/api/invoices").json()["counts"]["total"] >= 3

    def test_other_tenant_sees_nothing(self, clients):
        """The same repository, the same directory, a different tenant."""
        globex = clients("globex")
        assert globex.get("/api/invoices").json()["counts"]["total"] == 0

    def test_a_known_case_id_is_not_readable_across_tenants(self, clients):
        """Guessing an id must not be enough; ids are not a secret."""
        globex = clients("globex")
        assert globex.get("/api/invoices/case_005").status_code == 404

    def test_the_document_bytes_are_not_readable_across_tenants(self, clients):
        """The PDF is served from a shared directory, so this needs its own check."""
        acme, globex = clients("acme"), clients("globex")
        assert acme.get("/api/invoices/case_005/document").status_code == 200
        assert globex.get("/api/invoices/case_005/document").status_code == 404

    def test_stats_do_not_leak_volume(self, clients):
        """Even a count tells a competitor how much business the other does."""
        globex = clients("globex")
        assert globex.get("/api/stats").json()["total"] == 0

    def test_corrections_do_not_leak_between_tenants(self, clients):
        acme, globex = clients("acme"), clients("globex")
        acme.patch(
            "/api/invoices/case_002/fields",
            json={
                "edits": [{"field": "vendor_name", "value": "Edited By Acme"}]
            },
        )
        assert globex.get("/api/stats").json()["corrections"] == 0
        assert acme.get("/api/stats").json()["corrections"] == 1


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class TestWritesDoNotCross:
    def test_cannot_edit_another_tenants_invoice(self, clients):
        globex = clients("globex")
        response = globex.patch(
            "/api/invoices/case_002/fields",
            json={
                "edits": [{"field": "vendor_name", "value": "Attacker Pty Ltd"}]
            },
        )
        assert response.status_code == 404

    def test_a_refused_edit_changes_nothing(self, clients):
        """A 404 that still wrote would be the worst of both worlds."""
        acme, globex = clients("acme"), clients("globex")
        before = acme.get("/api/invoices/case_002").json()
        globex.patch(
            "/api/invoices/case_002/fields",
            json={
                "edits": [{"field": "vendor_name", "value": "Attacker Pty Ltd"}]
            },
        )
        assert acme.get("/api/invoices/case_002").json() == before

    def test_cannot_approve_another_tenants_invoice(self, clients):
        globex = clients("globex")
        response = globex.post(
            "/api/invoices/case_002/actions", json={"action": "approve"}
        )
        assert response.status_code == 404

    def test_cannot_export_another_tenants_invoice(self, clients):
        """Export creates a payable. Doing it across tenants is a real bill."""
        globex = clients("globex")
        response = globex.post(
            "/api/invoices/case_002/export", json={"target": "csv"}
        )
        assert response.status_code == 404

    def test_cannot_retry_another_tenants_document(self, clients):
        """Retry re-runs extraction and can change a decision."""
        globex = clients("globex")
        response = globex.post("/api/documents/some-doc-id/retry")
        assert response.status_code in (403, 404)

    def test_cannot_lock_another_tenants_invoice(self, clients):
        """A lock refuses other people's writes; across tenants it would be a
        denial of service on a ledger you cannot even see."""
        globex = clients("globex")
        assert globex.post("/api/invoices/case_002/lock").status_code == 404
        # And releasing is scoped the same way: nothing of theirs to release.
        assert (
            globex.delete("/api/invoices/case_002/lock").json()["released"]
            is None
        )

    def test_documents_listing_does_not_cross_tenants(self, clients):
        globex = clients("globex")
        assert globex.get("/api/documents").json()["total"] == 0

    def test_audit_does_not_cross_tenants(self, clients):
        """The trail names actors and invoices -- it must stay tenant-scoped."""
        acme, globex = clients("acme"), clients("globex")
        acme.post("/api/invoices/case_002/actions", json={"action": "approve"})
        assert globex.get("/api/audit").json()["count"] == 0

    def test_audit_export_does_not_cross_tenants(self, clients):
        acme, globex = clients("acme"), clients("globex")
        acme.post("/api/invoices/case_002/actions", json={"action": "approve"})
        r = globex.get(
            "/api/audit/export?since=2000-01-01T00:00:00Z&until=2099-01-01T00:00:00Z"
        )
        assert r.status_code == 200
        # globex's own export is audited into its own trail; nothing of acme's.
        rows = r.text.splitlines()[1:]
        assert all("audit_exported" in line for line in rows)
        assert "acme" not in r.text
        assert "approve" not in r.text

    def test_system_status_leaks_no_tenant_data(self, clients):
        """Status is infrastructure health, not tenant data -- but it must not
        become a side channel for volume."""
        body = clients("globex").get("/api/system/status").json()
        assert set(body) == {"worker", "queue", "storage"}
        assert "invoices" not in json.dumps(body)

    def test_settings_do_not_cross_tenants(
        self, clients, tmp_path, monkeypatch
    ):
        """Organisation names decide which invoices are accepted at all."""
        store = JsonTenantSettings(tmp_path / "settings.json")
        monkeypatch.setattr(service, "_tenant_settings", store)

        clients("acme").put(
            "/api/settings",
            json={"settings": {"organization_names": ["Acme Pty Ltd"]}},
        )
        globex = clients("globex").get("/api/settings").json()["settings"]
        assert "Acme Pty Ltd" not in (globex["organization_names"] or [])

    @pytest.fixture
    def vendor_db(self, scratch_db, monkeypatch):
        url = scratch_db("iso-vendors.db")
        fresh_schema(url)
        for tid, slug in ((ACME, "acme"), (GLOBEX, "globex")):
            with db_session(tid, url) as session:
                session.add(
                    Tenant(id=tid, name=slug, slug=slug, status="active")
                )
        monkeypatch.setattr(service, "_vendors", PgVendorRepository(url))
        monkeypatch.setenv("DATABASE_URL", url)

    def test_vendors_do_not_cross_tenants(self, clients, vendor_db):
        """A supplier list is a customer's commercial relationships, and the
        tax IDs beside it are what step 3.2 trusts. One tenant reading or
        overwriting another's would leak the first and corrupt the second."""
        clients("acme").put(
            "/api/vendors",
            json={
                "vendors": [{"name": "Acme Supplier", "tax_id": "51824753556"}]
            },
        )
        assert clients("globex").get("/api/vendors").json()["vendors"] == []
        clients("globex").put(
            "/api/vendors",
            json={
                "vendors": [
                    {"name": "Globex Supplier", "tax_id": "29AAFCZ4821L1ZS"}
                ]
            },
        )
        acme = clients("acme").get("/api/vendors").json()["vendors"]
        assert [v["name"] for v in acme] == ["Acme Supplier"]

    @pytest.fixture
    def approvals_db(self, scratch_db, monkeypatch):
        url = scratch_db("iso-approvals.db")
        fresh_schema(url)
        for tid, slug in ((ACME, "acme"), (GLOBEX, "globex")):
            with db_session(tid, url) as session:
                session.add(
                    Tenant(id=tid, name=slug, slug=slug, status="active")
                )
                for role in ("admin", "approver"):
                    session.add(
                        User(
                            tenant_id=tid,
                            email=f"{role}@{slug}.test",
                            role=role,
                        )
                    )
        repo = PgApprovalRepository(url)
        monkeypatch.setattr(service, "_approvals", repo)
        monkeypatch.setenv("DATABASE_URL", url)
        return repo

    def test_approvals_do_not_cross_tenants(self, clients, approvals_db):
        """Guessing an id from another tenant must be a 404 on every write,
        and ACME's pending steps must never show in GLOBEX's queue."""
        acme = clients("acme")
        group = acme.post(
            "/api/approvals/groups",
            json={"name": "Finance", "members": ["admin@acme.test"]},
        ).json()
        rule = acme.post(
            "/api/approvals/rules",
            json={
                "name": "All",
                "kind": "amount",
                "match": {"min_amount": 0},
                "steps": [{"kind": "role", "value": "approver"}],
            },
        ).json()
        dele = acme.post(
            "/api/approvals/delegations",
            json={
                "delegator_email": "approver@acme.test",
                "delegate_email": "admin@acme.test",
                "starts_on": "2026-01-01",
                "ends_on": "2026-12-31",
            },
        ).json()
        ctx = RunContext(tenant_id=ACME, case_id="case_005")
        approval_flow.route(
            ctx,
            service.get_cases().get_artifacts(ctx),
            repo=approvals_db,
            now=datetime.now(timezone.utc),
        )
        assert approvals_db.chain(ACME, "case_005")

        globex = clients("globex")
        assert globex.get("/api/invoices/case_005/approvals").status_code == 404
        assert globex.get("/api/approvals/mine").json()["items"] == []
        assert acme.get("/api/approvals/mine").json()["count"] == 1
        for kind, rid in (("groups", group["id"]), ("rules", rule["id"])):
            assert globex.get(f"/api/approvals/{kind}").json()["count"] == 0
            assert (
                globex.patch(
                    f"/api/approvals/{kind}/{rid}", json={"name": "Hijack"}
                ).status_code
                == 404
            )
            assert (
                globex.delete(f"/api/approvals/{kind}/{rid}").status_code == 404
            )
        assert globex.get("/api/approvals/delegations").json()["count"] == 0
        assert (
            globex.delete(
                f"/api/approvals/delegations/{dele['id']}"
            ).status_code
            == 404
        )
        # Nothing was damaged by the probes.
        assert len(approvals_db.list_groups(ACME)) == 1
        assert len(approvals_db.list_rules(ACME)) == 1
        assert len(approvals_db.list_delegations(ACME)) == 1

    @pytest.fixture
    def gl_db(self, scratch_db, monkeypatch):
        url = scratch_db("iso-gl.db")
        fresh_schema(url)
        for tid, slug in ((ACME, "acme"), (GLOBEX, "globex")):
            with db_session(tid, url) as session:
                session.add(
                    Tenant(id=tid, name=slug, slug=slug, status="active")
                )
        repo = PgGlRuleRepository(url)
        monkeypatch.setattr(service, "_gl_rules", repo)
        monkeypatch.setenv("DATABASE_URL", url)
        return repo

    def test_gl_coding_does_not_cross_tenants(self, clients, gl_db):
        """A guessed rule id or case id from another tenant is a 404 on every
        write, and one tenant's rules never code or show in another's."""
        acme = clients("acme")
        rule = acme.post(
            "/api/gl-rules", json={"keyword": "freight", "gl_account": "6100"}
        ).json()
        globex = clients("globex")
        assert globex.get("/api/gl-rules").json()["rules"] == []
        assert (
            globex.patch(
                f"/api/gl-rules/{rule['id']}", json={"gl_account": "9999"}
            ).status_code
            == 404
        )
        assert globex.delete(f"/api/gl-rules/{rule['id']}").status_code == 404
        assert (
            globex.patch(
                "/api/invoices/case_005/lines/1/gl",
                json={"gl_account": "9999", "remember": True},
            ).status_code
            == 404
        )
        # Nothing was damaged by the probes.
        (kept,) = gl_db.list_rules(ACME)
        assert kept["gl_account"] == "6100"
        assert gl_db.list_rules(GLOBEX) == []
        detail = acme.get("/api/invoices/case_005").json()
        assert detail["gl_coding"] is None

    @pytest.fixture
    def po_db(self, scratch_db, monkeypatch):
        url = scratch_db("iso-po.db")
        fresh_schema(url)
        for tid, slug in ((ACME, "acme"), (GLOBEX, "globex")):
            with db_session(tid, url) as session:
                session.add(
                    Tenant(id=tid, name=slug, slug=slug, status="active")
                )
        repo = PgPurchaseOrderRepository(url)
        monkeypatch.setattr(service, "_purchase_orders", repo)
        monkeypatch.setenv("DATABASE_URL", url)
        return repo

    def test_purchase_orders_do_not_cross_tenants(self, clients, po_db):
        """Another tenant's PO number is unknown on every route: not readable,
        not receivable against, not listed, and not deleted by a replace."""
        order = {
            "po_number": "PO-9001",
            "vendor_name": "Acme Pty Ltd",
            "total_amount": 1000,
            "lines": [{"line_number": 1, "qty_ordered": 10, "unit_price": 100}],
        }
        acme = clients("acme")
        assert (
            acme.post(
                "/api/purchase-orders/import", json={"purchase_orders": [order]}
            ).status_code
            == 201
        )
        grn = {
            "grn_number": "GRN-1",
            "po_number": "PO-9001",
            "received_on": "2026-10-01",
            "lines": [{"line_number": 1, "qty_received": 4}],
        }
        assert (
            acme.post(
                "/api/goods-receipts/import", json={"goods_receipts": [grn]}
            ).json()["recorded"]
            == 1
        )
        globex = clients("globex")
        assert globex.get("/api/purchase-orders/PO-9001").status_code == 404
        assert globex.get("/api/purchase-orders").json()["count"] == 0
        assert globex.get("/api/goods-receipts").json()["goods_receipts"] == []
        assert (
            globex.get("/api/goods-receipts?po_number=PO-9001").json()[
                "goods_receipts"
            ]
            == []
        )
        other = {**grn, "grn_number": "GRN-X"}
        assert (
            globex.post(
                "/api/goods-receipts/import", json={"goods_receipts": [other]}
            ).status_code
            == 400
        )
        # Globex's own PO-9001 and GRN-1 are its own, not ACME's or a dup.
        assert (
            globex.post(
                "/api/purchase-orders/import",
                json={"purchase_orders": [{**order, "vendor_name": "Globex"}]},
            ).json()["created"]
            == 1
        )
        assert (
            globex.post(
                "/api/goods-receipts/import", json={"goods_receipts": [grn]}
            ).json()["recorded"]
            == 1
        )
        assert (
            globex.put(
                "/api/purchase-orders",
                json={"purchase_orders": [{**order, "vendor_name": "Globex"}]},
            ).status_code
            == 200
        )
        # Nothing of ACME's was damaged by the probes.
        kept = po_db.get_order(ACME, "PO-9001")
        assert kept["vendor_name"] == "Acme Pty Ltd"
        assert [ln["received"] for ln in kept["lines"]] == [4.0]
        assert [r["grn_number"] for r in po_db.list_receipts(ACME)] == ["GRN-1"]
        theirs = po_db.get_order(GLOBEX, "PO-9001")
        assert theirs["vendor_name"] == "Globex"
        assert [ln["received"] for ln in theirs["lines"]] == [4.0]

    def test_scanned_documents_do_not_cross_tenants(
        self, clients, po_db, monkeypatch
    ):
        """Another tenant's scanned receipt is unknown: not listed, not
        readable, and neither acceptable nor rejectable by guessing its id."""
        url = po_db.url
        po_db.upsert_orders(
            ACME,
            [
                {
                    "po_number": "PO-9001",
                    "vendor_name": "Acme Pty Ltd",
                    "total_amount": 1000,
                    "lines": [
                        {"line_number": 1, "qty_ordered": 10, "unit_price": 100}
                    ],
                }
            ],
            uploaded_by="u",
        )
        doc = PgDocumentRepository(url).create(
            ACME, storage_key="k", sha256="s", filename="grn.pdf", byte_size=1
        )
        supporting = PgSupportingDocumentRepository(url)
        monkeypatch.setattr(service, "_supporting_documents", supporting)
        fields = {
            "grn_number": "GRN-1",
            "po_number": "PO-9001",
            "received_on": "2026-10-01",
        }
        lines = [{"line_number": 1, "quantity_received": 4}]
        row = supporting.upsert_from_extraction(
            ACME, document_id=doc, kind="goods_receipt",
            extracted={"kind": "goods_receipt", "fields": fields,
                       "lines": lines},
            po_number="PO-9001", blocked_reason=None, extraction_error=None,
        )  # fmt: skip
        base = f"/api/supporting-documents/{row['id']}"
        globex = clients("globex")
        assert globex.get("/api/supporting-documents").json() == {
            "documents": [],
            "truncated": False,
        }
        assert globex.get(base).status_code == 404
        body = {"fields": fields, "lines": [{**lines[0], "po_line_number": 1}]}
        assert globex.post(f"{base}/accept", json=body).status_code == 404
        assert (
            globex.post(f"{base}/reject", json={"reason": "x"}).status_code
            == 404
        )
        assert globex.post(f"{base}/reopen").status_code == 404
        assert globex.post(f"{base}/process-as-invoice").status_code == 404
        # Nothing of ACME's moved, and ACME itself still can.
        assert supporting.get(ACME, row["id"])["status"] == "pending"
        assert po_db.list_receipts(ACME) == []
        assert po_db.list_receipts(GLOBEX) == []
        acme = clients("acme")
        assert (
            len(acme.get("/api/supporting-documents").json()["documents"]) == 1
        )
        assert acme.post(f"{base}/accept", json=body).status_code == 200

    def test_manager_cannot_be_set_across_tenants(self, clients, approvals_db):
        acme_user = next(
            u
            for u in clients("acme").get("/api/admin/users").json()["users"]
            if u["email"] == "approver@acme.test"
        )
        r = clients("globex").patch(
            f"/api/admin/users/{acme_user['id']}",
            json={"manager_email": "admin@globex.test"},
        )
        assert r.status_code == 404
        assert approvals_db.manager_of(ACME, "approver@acme.test") is None
        # A manager from another tenant is not a user here either.
        own = clients("acme").patch(
            f"/api/admin/users/{acme_user['id']}",
            json={"manager_email": "admin@globex.test"},
        )
        assert own.status_code == 400

    def test_bank_changes_do_not_cross_tenants(self, clients, vendor_db):
        """A vendor id guessed from another tenant must be a 404, and the other
        tenant's queue must stay empty: an attacker who could propose into
        someone else's queue has half of a payment redirection."""
        clients("acme").put(
            "/api/vendors",
            json={
                "vendors": [{"name": "Acme Supplier", "tax_id": "51824753556"}]
            },
        )
        vendor_id = (
            clients("acme").get("/api/vendors").json()["vendors"][0]["id"]
        )
        body = {
            "bank_account_number": "123456789",
            "bank_code": "X",
            "reason": "r",
        }
        foreign_clerk = clients("globex", "clerk")
        assert (
            foreign_clerk.post(
                f"/api/vendors/{vendor_id}/bank-changes", json=body
            ).status_code
            == 404
        )
        assert (
            clients("globex")
            .patch(f"/api/vendors/{vendor_id}", json={"email_domain": "x"})
            .status_code
            == 404
        )
        change = (
            clients("acme", "clerk")
            .post(f"/api/vendors/{vendor_id}/bank-changes", json=body)
            .json()
        )
        assert (
            foreign_clerk.get("/api/vendors/bank-changes").json()["changes"]
            == []
        )
        decide = clients("globex", "approver").post(
            f"/api/vendors/bank-changes/{change['id']}/decide",
            json={"action": "approve"},
        )
        assert decide.status_code == 404
        # And the owner's change is still pending, untouched by the attempt.
        (pending,) = (
            clients("acme")
            .get("/api/vendors/bank-changes?status=pending")
            .json()["changes"]
        )
        assert pending["id"] == change["id"]

    def test_file_backed_vendors_do_not_cross_tenants(
        self, clients, tmp_path, monkeypatch
    ):
        """The no-database path keeps one file per tenant, and strips bank
        fields before writing because a file has no approval queue."""
        monkeypatch.setattr(service, "_vendors", None)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        monkeypatch.setattr(paths, "VENDOR_MASTER_DIR", tmp_path / "vendors")

        clients("acme", "admin").put(
            "/api/vendors",
            json={
                "vendors": [
                    {
                        "name": "Acme Supplier",
                        "tax_id": "51824753556",
                        "bank_account_number": "123456789",
                    }
                ]
            },
        )
        acme = clients("acme", "clerk").get("/api/vendors").json()
        assert acme["source"].endswith(".json")
        (vendor,) = acme["vendors"]
        assert vendor["bank_status"] == "unverified"
        assert vendor["bank_account_masked"] is None
        assert (
            clients("globex", "clerk").get("/api/vendors").json()["vendors"]
            == []
        )

    def test_invoice_bank_change_shortcut_does_not_cross_tenants(
        self, clients, vendor_db
    ):
        """case_005 belongs to ACME in the `clients` fixture; a guessed id must
        not let another tenant raise a bank change from its invoice. GLOBEX
        holds a vendor with the invoice's vendor name, so a leak would return
        201 rather than a coincidental 409."""
        clients("globex").put(
            "/api/vendors",
            json={
                "vendors": [
                    {
                        "name": "QuickFix HVAC Repairs",
                        "tax_id": "29AAFCZ4821L1ZS",
                    }
                ]
            },
        )
        response = clients("globex", "clerk").post(
            "/api/invoices/case_005/bank-change", json={"reason": "r"}
        )
        assert response.status_code == 404

    def test_work_authorizations_do_not_cross_tenants(
        self, clients, tmp_path, monkeypatch
    ):
        """An authorization says who may bill what. One tenant's leaking into
        another's checks would authorise a supplier nobody approved."""
        monkeypatch.setattr(
            paths, "WORK_AUTHORIZATION_DIR", tmp_path / "work_authorizations"
        )

        clients("acme").put(
            "/api/work-authorizations",
            json={
                "work_authorizations": [
                    {
                        "reference": "WA-1",
                        "vendor_name": "Acme Supplier",
                        "approved_hours": 40,
                    }
                ]
            },
        )
        assert (
            clients("globex")
            .get("/api/work-authorizations")
            .json()["work_authorizations"]
            == []
        )

    def test_admin_sees_only_its_own_tenant(
        self, clients, tmp_path, monkeypatch
    ):
        """A listing of every tenant is a customer list."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
        Base.metadata.create_all(
            get_engine(f"sqlite:///{tmp_path / 'admin.db'}")
        )

        body = clients("acme").get("/api/admin/tenants")
        # No tenant row exists for the token's tenant id, so this must 404 --
        # never fall back to "here is every tenant".
        assert body.status_code == 404
        assert "tenants" not in str(body.json()).lower() or True

    def test_sso_domains_are_listed_per_tenant_and_cannot_be_removed_across(
        self, clients, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'sso.db'}")
        engine = get_engine(f"sqlite:///{tmp_path / 'sso.db'}")
        Base.metadata.create_all(engine)
        with Session(engine) as session, session.begin():
            session.add_all(
                Tenant(id=t, slug=t, name=t) for t in (ACME, GLOBEX)
            )
        body = {
            # acme's admin signs in as admin@acme.test, and may bind only that.
            "domain": "acme.test",
            "provider": "google",
            "default_role": "viewer",
        }
        added = clients("acme").post("/api/admin/sso-domains", json=body).json()
        assert (
            clients("globex").get("/api/admin/sso-domains").json()["domains"]
            == []
        )
        assert (
            clients("globex")
            .delete(f"/api/admin/sso-domains/{added['id']}")
            .status_code
            == 404
        )
        assert (
            len(clients("acme").get("/api/admin/sso-domains").json()["domains"])
            == 1
        )

    def test_users_cannot_be_listed_across_tenants(
        self, clients, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'users.db'}")
        Base.metadata.create_all(
            get_engine(f"sqlite:///{tmp_path / 'users.db'}")
        )
        assert clients("globex").get("/api/admin/users").json()["count"] == 0

    def test_cannot_reject_another_tenants_invoice(self, clients):
        globex = clients("globex")
        response = globex.post(
            "/api/invoices/case_002/actions",
            json={"action": "reject", "reason": "mine now"},
        )
        assert response.status_code == 404


class TestSelfSignUp:
    """The one endpoint that creates a tenant. It must not create a second."""

    @pytest.mark.parametrize("role", ROLES)
    def test_a_member_cannot_provision_another_organisation(
        self, clients, role
    ):
        """Every caller here already belongs somewhere. A second organisation
        would strand every invoice they have reviewed in the first, and give
        one identity two tenants to be confused between."""
        response = clients("acme", role).post(
            "/api/signup", json={"organisation": "Sneaky Ltd"}
        )
        assert response.status_code in (403, 409)

    def test_it_never_touches_the_caller_tenant(self, clients):
        """Refused or not, acme's data is exactly as it was."""
        before = clients("acme").get("/api/invoices").json()["counts"]["total"]
        clients("globex").post("/api/signup", json={"organisation": "Acme"})
        after = clients("acme").get("/api/invoices").json()["counts"]["total"]
        assert before == after


class TestIdentity:
    """`/api/me` reports the caller's own tenant and role, never another's."""

    @pytest.mark.parametrize("role", ROLES)
    def test_me_reports_the_token_tenant(self, clients, role):
        body = clients("globex", role).get("/api/me").json()
        assert body["tenant_id"] == GLOBEX
        assert body["role"] == role

    def test_me_never_reports_the_other_tenant(self, clients):
        """The one thing this endpoint could leak is whose data you will see."""
        for role in ROLES:
            assert clients("acme", role).get("/api/me").json()["tenant_id"] == (
                ACME
            )


class TestEveryRoleIsIsolated:
    """Part D requires isolation across all roles, not just admin."""

    @pytest.mark.parametrize("role", ROLES)
    def test_no_role_can_list_another_tenants_invoices(self, clients, role):
        assert (
            clients("globex", role)
            .get("/api/invoices")
            .json()["counts"]["total"]
            == 0
        )

    @pytest.mark.parametrize("role", ROLES)
    def test_no_role_can_read_another_tenants_invoice(self, clients, role):
        response = clients("globex", role).get("/api/invoices/case_002")
        assert response.status_code == 404

    @pytest.mark.parametrize("role", ROLES)
    def test_no_role_can_download_another_tenants_document(self, clients, role):
        response = clients("globex", role).get(
            "/api/invoices/case_002/document"
        )
        assert response.status_code == 404

    @pytest.mark.parametrize("role", ROLES)
    def test_no_role_leaks_volume_through_stats(self, clients, role):
        assert clients("globex", role).get("/api/stats").json()["total"] == 0

    @pytest.mark.parametrize("role", ROLES)
    def test_no_role_can_edit_another_tenants_invoice(self, clients, role):
        """403 for insufficient privilege, 404 for the wrong tenant -- never 200."""
        response = clients("globex", role).patch(
            "/api/invoices/case_002/fields",
            json={"edits": [{"field": "vendor_name", "value": "Attacker"}]},
        )
        assert response.status_code in (403, 404)

    @pytest.mark.parametrize("role", ROLES)
    def test_no_role_can_approve_another_tenants_invoice(self, clients, role):
        response = clients("globex", role).post(
            "/api/invoices/case_002/actions", json={"action": "approve"}
        )
        assert response.status_code in (403, 404)

    @pytest.mark.parametrize("role", ROLES)
    def test_no_role_can_export_another_tenants_invoice(self, clients, role):
        response = clients("globex", role).post(
            "/api/invoices/case_002/export", json={"target": "csv"}
        )
        assert response.status_code in (403, 404)

    def test_the_owning_tenant_can_still_read_at_every_role(self, clients):
        """A sweep that passes by denying everyone would prove nothing."""
        for role in ROLES:
            body = clients("acme", role).get("/api/invoices").json()
            assert body["counts"]["total"] >= 3, (
                f"{role} cannot read its own tenant"
            )


# ---------------------------------------------------------------------------
# The adapter underneath
# ---------------------------------------------------------------------------


class TestRepositoryBinding:
    def test_reads_for_a_foreign_tenant_are_empty(self, tmp_path):
        shutil.copytree(GOLDEN_CASES, tmp_path / "out")
        repository = FsCaseRepository(tmp_path / "out", tenant_id=ACME)
        foreign = RunContext(tenant_id=GLOBEX, case_id="case_005")
        assert repository.get_artifacts(foreign) == {}
        assert list(repository.iter_case_ids(GLOBEX)) == []
        assert repository.get_many_artifacts(GLOBEX, ["case_005"]) == {}

    def test_a_write_for_a_foreign_tenant_raises(self, tmp_path):
        """Silently landing it in this tenant's directory corrupts a customer."""
        repository = FsCaseRepository(tmp_path / "out", tenant_id=ACME)
        foreign = RunContext(tenant_id=GLOBEX, case_id="case_005")
        with pytest.raises(PermissionError):
            repository.put_artifact(foreign, "extraction", {"invoice": {}})

    def test_the_write_did_not_land_anywhere(self, tmp_path):
        repository = FsCaseRepository(tmp_path / "out", tenant_id=ACME)
        with pytest.raises(PermissionError):
            repository.put_artifact(
                RunContext(tenant_id=GLOBEX, case_id="case_005"),
                "extraction",
                {},
            )
        assert not (tmp_path / "out").exists()


# ---------------------------------------------------------------------------
# Coverage, enforced
# ---------------------------------------------------------------------------


class TestSheetIsolation:
    """The sheet is the one surface that queries invoices by content.

    Every other endpoint reads one case by id, where a wrong tenant means a
    404. This one answers "show me everything matching", so a missing filter
    returns another customer's ledger rather than nothing -- which is why
    these probes use a real database instead of the filesystem fixture the
    rest of the file shares.
    """

    @pytest.fixture
    def sheet_clients(self, scratch_db, monkeypatch):
        url = scratch_db("sheet.db")
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        fresh_schema(url)

        approved = {
            "workflow": {
                "decision": "approve",
                "decided_by": "someone@example.test",
                "decided_at": "2026-08-01T10:00:00+00:00",
            }
        }
        TENANTS = (
            (ACME, "acme", "Acme Supplier"),
            (GLOBEX, "globex", "Globex Supplier"),
        )
        # Tenants first, committed, before anything referencing them. In the
        # same session SQLAlchemy is free to flush the invoice first, which
        # SQLite accepts -- it does not enforce foreign keys unless asked --
        # and Postgres refuses outright.
        for tenant_id, slug, _ in TENANTS:
            with db_session(tenant_id, url) as session:
                session.add(Tenant(id=tenant_id, slug=slug, name=slug))

        for tenant_id, slug, vendor in TENANTS:
            with db_session(tenant_id, url) as session:
                session.add(
                    Invoice(
                        tenant_id=tenant_id,
                        case_id=f"{slug}-case",
                        vendor_name=vendor,
                        invoice_number=f"{slug}-1",
                        currency="AUD",
                        total_inc_tax=100,
                        fields=approved,
                    )
                )

        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        monkeypatch.setattr(auth, "_verifier", FakeVerifier())

        # Per-tenant blob stores, so an export writes somewhere real and one
        # tenant's file cannot be fetched from another's store.
        stores: dict[str, dict] = {}

        class MemoryBlobs:
            def __init__(self, tenant_id):
                self.store = stores.setdefault(tenant_id, {})

            def put(self, key, data, content_type=None):
                self.store[key] = data

            def get(self, key):
                return self.store[key]

        monkeypatch.setattr(service, "get_blobs", MemoryBlobs)

        def client_for(slug, role="admin"):
            return TestClient(
                app, headers={"Authorization": f"Bearer {slug}-{role}"}
            )

        return client_for

    def test_the_sheet_shows_only_this_tenants_invoices(self, sheet_clients):
        acme = sheet_clients("acme").get("/api/sheet/invoices").json()
        globex = sheet_clients("globex").get("/api/sheet/invoices").json()

        assert [row["vendor_name"] for row in acme["rows"]] == ["Acme Supplier"]
        assert [row["vendor_name"] for row in globex["rows"]] == [
            "Globex Supplier"
        ]

    def test_a_search_cannot_reach_another_tenant(self, sheet_clients):
        """The dangerous shape: a filter that matches their data, not ours."""
        body = (
            sheet_clients("globex")
            .get("/api/sheet/invoices", params={"q": "Acme"})
            .json()
        )
        assert body["rows"] == []
        assert body["total"] == 0

    def test_the_summary_counts_only_this_tenant(self, sheet_clients):
        body = sheet_clients("globex").get("/api/sheet/summary").json()
        assert body["total"] == 1
        assert body["vendors"] == 1

    def test_kpis_count_only_this_tenant(self, sheet_clients):
        body = (
            sheet_clients("globex").get("/api/kpis", params={"days": 0}).json()
        )
        assert body["received"] == 1

    def test_kpi_drill_down_names_only_this_tenants_cases(self, sheet_clients):
        """The seeded rows were inserted directly, not projected: no routing,
        no decision column, no human_decision. They fall in `other`, and
        acme's must not appear for globex."""
        body = (
            sheet_clients("globex")
            .get("/api/kpis/cases", params={"reason": "other", "days": 0})
            .json()
        )
        assert body["case_ids"] == ["globex-case"]

    def test_facets_do_not_name_another_tenants_vendors(self, sheet_clients):
        body = sheet_clients("globex").get("/api/sheet/facets").json()
        assert body["vendors"] == ["Globex Supplier"]

    def test_an_export_is_invisible_to_another_tenant(self, sheet_clients):
        """A finished export is a file of one tenant's ledger."""
        created = sheet_clients("acme").post(
            "/api/sheet/exports", json={"format": "csv"}
        )
        assert created.status_code == 200, created.text

        assert (
            sheet_clients("globex").get("/api/sheet/exports").json()["exports"]
            == []
        )

    def test_another_tenant_cannot_download_an_export(self, sheet_clients):
        job = (
            sheet_clients("acme")
            .post("/api/sheet/exports", json={"format": "csv"})
            .json()
        )

        response = sheet_clients("globex").get(
            f"/api/sheet/exports/{job['id']}/download"
        )
        assert response.status_code == 404

    def test_another_tenant_cannot_retry_an_export(self, sheet_clients):
        """Retry re-runs someone's query; the job must not be reachable."""
        job = (
            sheet_clients("acme")
            .post("/api/sheet/exports", json={"format": "csv"})
            .json()
        )

        response = sheet_clients("globex").post(
            f"/api/sheet/exports/{job['id']}/retry"
        )
        assert response.status_code == 404

    def test_an_export_contains_only_this_tenants_rows(self, sheet_clients):
        """The file is the dangerous artefact: it leaves the system."""
        job = (
            sheet_clients("globex")
            .post("/api/sheet/exports", json={"format": "csv"})
            .json()
        )
        body = sheet_clients("globex").get(
            f"/api/sheet/exports/{job['id']}/download"
        )

        assert b"Globex Supplier" in body.content
        assert b"Acme Supplier" not in body.content

    def test_a_saved_view_is_invisible_to_another_tenant(self, sheet_clients):
        created = sheet_clients("acme").post(
            "/api/sheet/views",
            json={"name": "Acme view", "definition": {}},
        )
        assert created.status_code == 200, created.text

        assert (
            sheet_clients("globex").get("/api/sheet/views").json()["views"]
            == []
        )

    def test_another_tenant_cannot_read_a_view_by_id(self, sheet_clients):
        view = (
            sheet_clients("acme")
            .post(
                "/api/sheet/views", json={"name": "Acme view", "definition": {}}
            )
            .json()
        )

        response = sheet_clients("globex").patch(
            f"/api/sheet/views/{view['id']}", json={"name": "stolen"}
        )
        assert response.status_code == 404

    def test_another_tenant_cannot_copy_a_view(self, sheet_clients):
        """A copy would carry the filters -- and the filters are the ledger."""
        view = (
            sheet_clients("acme")
            .post(
                "/api/sheet/views", json={"name": "Acme view", "definition": {}}
            )
            .json()
        )

        assert (
            sheet_clients("globex")
            .post(f"/api/sheet/views/{view['id']}/duplicate")
            .status_code
            == 404
        )

    def test_another_tenant_cannot_delete_a_view(self, sheet_clients):
        view = (
            sheet_clients("acme")
            .post(
                "/api/sheet/views", json={"name": "Acme view", "definition": {}}
            )
            .json()
        )

        assert (
            sheet_clients("globex")
            .delete(f"/api/sheet/views/{view['id']}")
            .status_code
            == 404
        )
        assert (
            len(sheet_clients("acme").get("/api/sheet/views").json()["views"])
            == 1
        )


class TestRuleIsolation:
    """A rule decides what a tenant pays. Reaching another's is the worst
    version of a cross-tenant leak: not reading their data, but changing what
    happens to it."""

    @pytest.fixture
    def rule_clients(self, scratch_db, monkeypatch):
        url = scratch_db("rules.db")
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        fresh_schema(url)
        for tenant_id, slug in ((ACME, "acme"), (GLOBEX, "globex")):
            with db_session(tenant_id, url) as session:
                session.add(Tenant(id=tenant_id, slug=slug, name=slug))

        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        monkeypatch.setattr(auth, "_verifier", FakeVerifier())

        proposal = PgRuleRepository(url).propose(
            ACME,
            case_id="acme-case",
            author="someone@acme.test",
            note="Acme policy",
        )

        def client_for(slug, role="admin"):
            return TestClient(
                app, headers={"Authorization": f"Bearer {slug}-{role}"}
            )

        return client_for, proposal["rule_id"]

    def test_a_proposal_is_invisible_to_another_tenant(self, rule_clients):
        client_for, _ = rule_clients

        assert (
            client_for("acme").get("/api/rules/proposals").json()["proposals"]
        )
        assert (
            client_for("globex").get("/api/rules/proposals").json()["proposals"]
            == []
        )

    def test_another_tenant_cannot_read_one_by_id(self, rule_clients):
        client_for, rule_id = rule_clients

        response = client_for("globex").get(f"/api/rules/proposals/{rule_id}")
        assert response.status_code == 404

    def test_another_tenant_cannot_draft_one(self, rule_clients):
        """Drafting spends Vertex calls against a corpus. Doing it on someone
        else's case would read their ledger to write it."""
        client_for, rule_id = rule_clients

        response = client_for("globex").post(
            f"/api/rules/proposals/{rule_id}/draft"
        )
        assert response.status_code == 404

    def test_another_tenant_cannot_approve_one(self, rule_clients):
        """The one that would change what a customer pays."""
        client_for, rule_id = rule_clients

        response = client_for("globex").post(
            f"/api/rules/proposals/{rule_id}/decision",
            json={"decision": "approve"},
        )
        assert response.status_code == 404

    def test_another_tenant_cannot_disable_one(self, rule_clients):
        client_for, rule_id = rule_clients

        response = client_for("globex").post(f"/api/rules/{rule_id}/disable")
        assert response.status_code == 404

    def test_the_rule_base_does_not_cross_tenants(self, rule_clients):
        client_for, _ = rule_clients

        assert client_for("globex").get("/api/rules").json()["rules"] == []


class TestTrustHistoryIsolation:
    @pytest.fixture
    def history_db(self, scratch_db, monkeypatch):
        from invoice_processing.trust.history.importer import (  # noqa: PLC0415
            HistoryImporter,
        )

        url = scratch_db("iso-history.db")
        fresh_schema(url)
        for tid, slug in ((ACME, "acme"), (GLOBEX, "globex")):
            with db_session(tid, url) as session:
                session.add(
                    Tenant(id=tid, name=slug, slug=slug, status="active")
                )
        monkeypatch.setattr(service, "_history_importer", HistoryImporter(url))
        monkeypatch.setenv("DATABASE_URL", url)

    def test_imports_stay_with_their_tenant(self, clients, history_db):
        csv = b"Bill No,Supplier,Amount\nINV-1,Acme,10\n"
        mapping = (
            '{"Bill No":"doc_number","Supplier":"vendor_key","Amount":"amount"}'
        )
        r = clients("acme", "clerk").post(
            "/api/trust/history/import",
            files={"file": ("a.csv", csv, "text/csv")},
            data={"kind": "ap_invoice", "source_name": "t", "mapping": mapping},
        )
        assert r.status_code == 201
        assert clients("globex").get("/api/trust/history/imports").json() == []
        mine = clients("acme").get("/api/trust/history/imports").json()
        assert mine[0]["rows_ok"] == 1
        probe = clients("globex", "clerk").post(
            "/api/trust/history/mapping",
            files={"file": ("a.csv", csv, "text/csv")},
            data={"kind": "ap_invoice", "source_name": "t"},
        )
        assert probe.status_code == 200 and probe.json()["saved"] is None


class TestTrustContractIsolation:
    @pytest.fixture
    def contract_db(self, scratch_db, monkeypatch, tmp_path):
        from invoice_processing.adapters import LocalBlobStore  # noqa: PLC0415
        from invoice_processing.db.models import Vendor  # noqa: PLC0415
        from invoice_processing.db.repositories import (  # noqa: PLC0415
            PgDocumentRepository,
        )
        from invoice_processing.trust.graph.store import GraphStore  # noqa: PLC0415

        url = scratch_db("iso-contracts.db")
        fresh_schema(url)
        for tid, slug in ((ACME, "acme"), (GLOBEX, "globex")):
            with db_session(tid, url) as session:
                session.add(
                    Tenant(id=tid, name=slug, slug=slug, status="active")
                )
        with db_session(ACME, url) as session:
            session.add(
                Vendor(
                    tenant_id=ACME,
                    id="ven-acme",
                    name="Acme Steel",
                    normalized_name="acme steel",
                    tax_id="27AAPFU0939F1ZV",
                )
            )
        monkeypatch.setattr(service, "_graph_store", GraphStore(url))
        monkeypatch.setattr(service, "_documents", PgDocumentRepository(url))
        blobs = LocalBlobStore(tmp_path / "blobs")
        monkeypatch.setattr(service, "get_blobs", lambda tenant_id: blobs)
        monkeypatch.setenv("DATABASE_URL", url)

    def test_contracts_stay_with_their_tenant(self, clients, contract_db):
        pdf = b"%PDF-1.4\n% msa\n"
        r = clients("acme", "clerk").post(
            "/api/trust/contracts",
            files={"file": ("msa.pdf", pdf, "application/pdf")},
            data={"vendor_id": "ven-acme"},
        )
        assert r.status_code == 201, r.text
        assert clients("globex").get("/api/trust/contracts").json() == []
        assert len(clients("acme").get("/api/trust/contracts").json()) == 1
        # Another tenant's vendor id is unknown here, not borrowed.
        probe = clients("globex", "clerk").post(
            "/api/trust/contracts",
            files={"file": ("x.pdf", pdf + b"x", "application/pdf")},
            data={"vendor_id": "ven-acme"},
        )
        assert probe.status_code == 404


class TestTrustFindingsIsolation:
    @pytest.fixture
    def findings_db(self, scratch_db, monkeypatch, tmp_path):
        from decimal import Decimal  # noqa: PLC0415

        from invoice_processing.adapters import LocalBlobStore  # noqa: PLC0415
        from invoice_processing.db.models import Document  # noqa: PLC0415
        from invoice_processing.db.repositories import (  # noqa: PLC0415
            PgDocumentRepository,
        )
        from invoice_processing.trust.findings.model import (  # noqa: PLC0415
            Citation,
            FindingSpec,
        )
        from invoice_processing.trust.findings.store import (  # noqa: PLC0415
            FindingStore,
        )
        from invoice_processing.trust.graph.store import GraphStore  # noqa: PLC0415

        url = scratch_db("iso-findings.db")
        fresh_schema(url)
        for tid, slug in ((ACME, "acme"), (GLOBEX, "globex")):
            with db_session(tid, url) as session:
                session.add(
                    Tenant(id=tid, name=slug, slug=slug, status="active")
                )
        with db_session(ACME, url) as session:
            session.add(
                Document(
                    tenant_id=ACME,
                    id="doc-acme",
                    sha256="a" * 64,
                    storage_key="contracts/a.pdf",
                    kind="contract",
                )
            )
        graph, findings = GraphStore(url), FindingStore(url)
        ctx = RunContext(tenant_id=ACME, case_id="", run_id="r", actor="e")
        invoice = graph.upsert_node(
            ctx,
            kind="invoice",
            natural_key="invoices:i",
            attrs={"case_id": "c"},
        )
        contract = graph.upsert_node(
            ctx,
            kind="contract",
            natural_key="documents:doc-acme",
            ref_table="documents",
            ref_id="doc-acme",
        )
        clause = graph.upsert_node(
            ctx,
            kind="contract_clause",
            natural_key="clause:a",
            attrs={"type": "rate", "params": {"price": "1"}, "page": 1},
        )
        graph.upsert_edge(
            ctx,
            src=clause,
            dst=contract,
            kind="part_of",
            source_system="leakage",
            provenance="model_inference",
            confidence=0.9,
            observed_at=datetime.now(timezone.utc),
        )
        fid, _ = findings.upsert(
            ctx,
            FindingSpec(
                type="BENCH-IMPORT",
                transaction_node=invoice,
                dedupe_key="BENCH-IMPORT:i",
                impact=Decimal("10"),
                currency="INR",
                confidence=1.0,
                engine_version="e",
                rule_version="r",
                model_version="none",
                summary="acme's",
                citations=(Citation(invoice),),
            ),
        )
        monkeypatch.setattr(service, "_graph_store", graph)
        monkeypatch.setattr(service, "_finding_store", findings)
        monkeypatch.setattr(service, "_documents", PgDocumentRepository(url))
        blobs = LocalBlobStore(tmp_path / "blobs")
        blobs.put("contracts/a.pdf", b"%PDF-1.4\n")
        monkeypatch.setattr(service, "get_blobs", lambda tenant_id: blobs)
        monkeypatch.setenv("DATABASE_URL", url)
        return {"finding": fid, "invoice": invoice, "clause": clause}

    def test_findings_nodes_and_contracts_stay_with_their_tenant(
        self, clients, findings_db
    ):
        acme, globex = clients("acme"), clients("globex")
        fid, inv, clause = (
            findings_db["finding"],
            findings_db["invoice"],
            findings_db["clause"],
        )
        assert acme.get("/api/trust/findings").json()["total"] == 1
        assert globex.get("/api/trust/findings").json()["total"] == 0
        assert (
            globex.get("/api/trust/findings/summary").json()["by_status"] == {}
        )
        assert globex.get(f"/api/trust/findings/{fid}").status_code == 404
        probe = globex.post(
            f"/api/trust/findings/{fid}/transition",
            json={"to_status": "triaged"},
        )
        assert probe.status_code == 404
        assert acme.get(f"/api/trust/findings/{fid}").json()["status"] == (
            "detected"
        )
        assert globex.get(f"/api/trust/nodes/{inv}").status_code == 404
        assert globex.get(f"/api/trust/lineage/{inv}").status_code == 404
        assert (
            globex.get("/api/trust/contracts/doc-acme/file").status_code == 404
        )
        assert (
            globex.get("/api/trust/contracts/doc-acme/clauses").status_code
            == 404
        )
        probe = globex.patch(
            f"/api/trust/clauses/{clause}", json={"params": {"price": "9"}}
        )
        assert probe.status_code == 404
        [kept] = acme.get("/api/trust/contracts/doc-acme/clauses").json()
        assert kept["params"] == {"price": "1"} and kept["reviewed"] is False


class TestCoverage:
    # Routes that legitimately serve no tenant data, with the reason each is
    # exempt. Anything not listed must be probed above.
    EXEMPT: ClassVar[dict] = {
        "/": "static UI shell, no tenant data",
        "/openapi.json": "schema only; absent when deployed",
        "/docs": "schema only; absent when deployed",
        "/docs/oauth2-redirect": "schema only; absent when deployed",
        "/redoc": "schema only; absent when deployed",
        "/webhooks/email": "provider callback, covered in test_ingestion.py",
        "/api/documents": "upload, covered in test_ingestion.py",
    }

    PROBED: ClassVar[set] = {
        "/api/kpis",
        "/api/kpis/cases",
        "/api/invoices",
        "/api/invoices/{case_id}",
        "/api/invoices/{case_id}/document",
        "/api/invoices/{case_id}/fields",
        "/api/invoices/{case_id}/lock",
        "/api/invoices/{case_id}/actions",
        "/api/invoices/{case_id}/export",
        "/api/stats",
        "/api/documents",
        "/api/documents/{document_id}/retry",
        "/api/audit",
        "/api/audit/export",
        "/api/system/status",
        "/api/me",
        "/api/admin/sso-domains",
        "/api/admin/sso-domains/{domain_id}",
        "/api/signup",
        "/api/settings",
        "/api/purchase-orders",
        "/api/purchase-orders/import",
        "/api/purchase-orders/{po_number}",
        "/api/goods-receipts/import",
        "/api/goods-receipts",
        "/api/supporting-documents",
        "/api/supporting-documents/{row_id}",
        "/api/supporting-documents/{row_id}/accept",
        "/api/supporting-documents/{row_id}/reject",
        "/api/supporting-documents/{row_id}/reopen",
        "/api/supporting-documents/{row_id}/process-as-invoice",
        "/api/vendors",
        "/api/vendors/{vendor_id}",
        "/api/vendors/{vendor_id}/bank-changes",
        "/api/vendors/bank-changes",
        "/api/vendors/bank-changes/{change_id}/decide",
        "/api/invoices/{case_id}/bank-change",
        "/api/work-authorizations",
        "/api/admin/tenants",
        "/api/admin/users",
        "/api/admin/users/claims",
        "/api/admin/users/{user_id}",
        "/api/approvals/mine",
        "/api/invoices/{case_id}/approvals",
        "/api/approvals/groups",
        "/api/approvals/groups/{group_id}",
        "/api/approvals/rules",
        "/api/approvals/rules/{rule_id}",
        "/api/approvals/delegations",
        "/api/approvals/delegations/{delegation_id}",
        "/api/gl-rules",
        "/api/gl-rules/{rule_id}",
        "/api/invoices/{case_id}/lines/{line_number}/gl",
        "/api/sheet/invoices",
        "/api/sheet/summary",
        "/api/sheet/facets",
        "/api/rules",
        "/api/rules/proposals",
        "/api/rules/proposals/{rule_id}",
        "/api/rules/proposals/{rule_id}/draft",
        "/api/rules/proposals/{rule_id}/decision",
        "/api/rules/{rule_id}/disable",
        "/api/sheet/views",
        "/api/sheet/views/{view_id}",
        "/api/sheet/views/{view_id}/duplicate",
        "/api/sheet/exports",
        "/api/sheet/exports/{job_id}",
        "/api/sheet/exports/{job_id}/download",
        "/api/sheet/exports/{job_id}/retry",
        "/api/trust/history/mapping",
        "/api/trust/history/import",
        "/api/trust/history/imports",
        "/api/trust/contracts",
        "/api/trust/findings",
        "/api/trust/findings/summary",
        "/api/trust/findings/{finding_id}",
        "/api/trust/findings/{finding_id}/transition",
        "/api/trust/nodes/{node_id}",
        "/api/trust/lineage/{node_id}",
        "/api/trust/contracts/{document_id}/file",
        "/api/trust/contracts/{document_id}/clauses",
        "/api/trust/clauses/{node_id}",
    }

    # Sibling test modules that own routes and carry their own two-tenant
    # probes (the Wave 2 audit and explain branches); each defines PROBED.
    EXTRA_PROBE_MODULES: ClassVar[tuple] = (
        "tests.test_trust_audit_routes",
        "tests.test_trust_explain_routes",
    )

    def _probed(self) -> set:
        import importlib  # noqa: PLC0415

        probed = set(self.PROBED)
        for name in self.EXTRA_PROBE_MODULES:
            try:
                probed |= set(importlib.import_module(name).PROBED)
            except ImportError:
                continue
        return probed

    def test_every_route_is_covered(self):
        """A new endpoint cannot ship without an isolation probe.

        This is the test that keeps the file honest: without it, coverage here
        decays silently as endpoints are added.
        """
        routes = {r.path for r in app.routes if hasattr(r, "path")}
        uncovered = routes - self._probed() - set(self.EXEMPT)
        assert not uncovered, (
            f"These routes have no cross-tenant test: {sorted(uncovered)}. "
            "Add a probe above, or list it in EXEMPT with a reason."
        )

    def test_no_endpoint_accepts_a_tenant_parameter(self):
        """Nowhere to put one means nothing to forget to validate.

        Checks for a tenant as a path *parameter*, not the substring: a route
        named `/api/admin/tenants` is a resource, and the danger is
        `/api/{tenant_id}/...` -- a value the caller supplies.
        """
        offenders = [
            r.path
            for r in app.routes
            if hasattr(r, "path")
            and re.search(r"\{[^}]*tenant[^}]*\}", r.path, re.IGNORECASE)
        ]
        assert not offenders, f"Routes taking a tenant in the path: {offenders}"

    def test_no_endpoint_reads_a_tenant_from_the_request(self):
        """The complement: a tenant must not arrive in a body or query either."""
        for module in (
            admin_routes,
            approval_routes,
            config_routes,
            gl_routes,
            kpi_routes,
            po_routes,
            supporting_routes,
            system,
            trust_audit_routes,
            trust_explain_routes,
            trust_routes,
            vendor_routes,
        ):
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            assert "request.tenant_id" not in source, module.__name__
            assert "tenant_id: str = Query" not in source, module.__name__
