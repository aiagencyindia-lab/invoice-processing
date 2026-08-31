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

import shutil
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from invoice_processing import paths
from invoice_processing.adapters import FsCaseRepository
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.ports import RunContext

ACME, GLOBEX = "tenant-acme", "tenant-globex"

CLAIMS = {
    "acme": {
        "sub": "u-acme",
        "email": "admin@acme.test",
        "email_verified": True,
        "tenant_id": ACME,
        "role": "admin",
    },
    "globex": {
        "sub": "u-globex",
        "email": "admin@globex.test",
        "email_verified": True,
        "tenant_id": GLOBEX,
        "role": "admin",
    },
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
    shutil.copytree(paths.AGENTIC_FLOW_OUT, cases_root)

    monkeypatch.setattr(
        service, "_cases", FsCaseRepository(cases_root, tenant_id=ACME)
    )
    monkeypatch.setattr(
        service, "_corrections_path", tmp_path / "corrections.jsonl"
    )
    monkeypatch.setattr(
        service, "_audit", service.JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())

    return (
        TestClient(app, headers={"Authorization": "Bearer acme"}),
        TestClient(app, headers={"Authorization": "Bearer globex"}),
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class TestReadsDoNotCross:
    def test_owner_sees_their_invoices(self, clients):
        acme, _ = clients
        assert acme.get("/api/invoices").json()["counts"]["total"] >= 3

    def test_other_tenant_sees_nothing(self, clients):
        """The same repository, the same directory, a different tenant."""
        _, globex = clients
        assert globex.get("/api/invoices").json()["counts"]["total"] == 0

    def test_a_known_case_id_is_not_readable_across_tenants(self, clients):
        """Guessing an id must not be enough; ids are not a secret."""
        _, globex = clients
        assert globex.get("/api/invoices/case_005").status_code == 404

    def test_the_document_bytes_are_not_readable_across_tenants(self, clients):
        """The PDF is served from a shared directory, so this needs its own check."""
        acme, globex = clients
        assert acme.get("/api/invoices/case_005/document").status_code == 200
        assert globex.get("/api/invoices/case_005/document").status_code == 404

    def test_stats_do_not_leak_volume(self, clients):
        """Even a count tells a competitor how much business the other does."""
        _, globex = clients
        assert globex.get("/api/stats").json()["total"] == 0

    def test_corrections_do_not_leak_between_tenants(self, clients):
        acme, globex = clients
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
        _, globex = clients
        response = globex.patch(
            "/api/invoices/case_002/fields",
            json={
                "edits": [{"field": "vendor_name", "value": "Attacker Pty Ltd"}]
            },
        )
        assert response.status_code == 404

    def test_a_refused_edit_changes_nothing(self, clients):
        """A 404 that still wrote would be the worst of both worlds."""
        acme, globex = clients
        before = acme.get("/api/invoices/case_002").json()
        globex.patch(
            "/api/invoices/case_002/fields",
            json={
                "edits": [{"field": "vendor_name", "value": "Attacker Pty Ltd"}]
            },
        )
        assert acme.get("/api/invoices/case_002").json() == before

    def test_cannot_approve_another_tenants_invoice(self, clients):
        _, globex = clients
        response = globex.post(
            "/api/invoices/case_002/actions", json={"action": "approve"}
        )
        assert response.status_code == 404

    def test_cannot_export_another_tenants_invoice(self, clients):
        """Export creates a payable. Doing it across tenants is a real bill."""
        _, globex = clients
        response = globex.post(
            "/api/invoices/case_002/export", json={"target": "csv"}
        )
        assert response.status_code == 404

    def test_cannot_reject_another_tenants_invoice(self, clients):
        _, globex = clients
        response = globex.post(
            "/api/invoices/case_002/actions",
            json={"action": "reject", "reason": "mine now"},
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# The adapter underneath
# ---------------------------------------------------------------------------


class TestRepositoryBinding:
    def test_reads_for_a_foreign_tenant_are_empty(self, tmp_path):
        shutil.copytree(paths.AGENTIC_FLOW_OUT, tmp_path / "out")
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


class TestCoverage:
    # Routes that legitimately serve no tenant data, with the reason each is
    # exempt. Anything not listed must be probed above.
    EXEMPT: ClassVar[dict] = {
        "/": "static UI shell, no tenant data",
        "/openapi.json": "schema only",
        "/docs": "schema only",
        "/docs/oauth2-redirect": "schema only",
        "/redoc": "schema only",
        "/webhooks/email": "provider callback, covered in test_ingestion.py",
        "/api/documents": "upload, covered in test_ingestion.py",
    }

    PROBED: ClassVar[set] = {
        "/api/invoices",
        "/api/invoices/{case_id}",
        "/api/invoices/{case_id}/document",
        "/api/invoices/{case_id}/fields",
        "/api/invoices/{case_id}/actions",
        "/api/invoices/{case_id}/export",
        "/api/stats",
    }

    def test_every_route_is_covered(self):
        """A new endpoint cannot ship without an isolation probe.

        This is the test that keeps the file honest: without it, coverage here
        decays silently as endpoints are added.
        """
        routes = {r.path for r in app.routes if hasattr(r, "path")}
        uncovered = routes - self.PROBED - set(self.EXEMPT)
        assert not uncovered, (
            f"These routes have no cross-tenant test: {sorted(uncovered)}. "
            "Add a probe above, or list it in EXEMPT with a reason."
        )

    def test_no_endpoint_accepts_a_tenant_parameter(self):
        """Nowhere to put one means nothing to forget to validate."""
        offenders = [
            r.path
            for r in app.routes
            if hasattr(r, "path") and "tenant" in r.path.lower()
        ]
        assert not offenders, f"Routes taking a tenant in the path: {offenders}"
