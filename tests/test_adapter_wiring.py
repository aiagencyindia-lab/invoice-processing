"""Which storage adapter the application actually uses.

Every adapter here was written, tested, and then not connected. `PgCaseRepository`
and `PgDocumentRepository` existed for a full phase without being instantiated
anywhere outside their own tests, so case artifacts went to a package-relative
directory that is ephemeral on Cloud Run -- every processed invoice would have
been lost on restart, with a green test suite the whole time.

These tests assert the wiring itself, which is the part unit tests of the
adapters can never cover.

They also pin a property that the adapters' own tests missed: the local and
Postgres document repositories must agree about refusing an unknown tenant. The
test fake was stricter than the local implementation, so the suite proved a
property the production code did not have.
"""

import re

import pytest

from invoice_processing import paths
from invoice_processing.adapters import (
    FsCaseRepository,
    JsonDocumentRepository,
    JsonlAuditSink,
)
from invoice_processing.api import service
from invoice_processing.db.repositories import (
    PgAuditSink,
    PgCaseRepository,
    PgDocumentRepository,
)


@pytest.fixture(autouse=True)
def reset_adapters(monkeypatch):
    """Adapters memoize after first use; each test resolves them afresh."""
    monkeypatch.setattr(service, "_cases", None)
    monkeypatch.setattr(service, "_documents", None)
    monkeypatch.setattr(service, "_audit", None)


@pytest.fixture
def no_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)


@pytest.fixture
def with_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")


class TestResolution:
    def test_local_adapters_without_a_database(self, no_database):
        assert isinstance(service.get_cases(), FsCaseRepository)
        assert isinstance(service.get_documents(), JsonDocumentRepository)
        assert isinstance(service.get_audit(), JsonlAuditSink)

    def test_postgres_adapters_when_a_database_is_configured(
        self, with_database
    ):
        assert isinstance(service.get_cases(), PgCaseRepository)
        assert isinstance(service.get_documents(), PgDocumentRepository)
        assert isinstance(service.get_audit(), PgAuditSink)

    def test_cloud_sql_instance_also_selects_postgres(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("CLOUD_SQL_INSTANCE", "p:r:i")
        assert isinstance(service.get_cases(), PgCaseRepository)
        assert isinstance(service.get_documents(), PgDocumentRepository)

    def test_adapters_are_memoized(self, no_database):
        """Resolving per request would rebuild a connection pool each time."""
        assert service.get_cases() is service.get_cases()
        assert service.get_documents() is service.get_documents()

    def test_import_constructs_nothing(self):
        """A module import that dials a database breaks every offline tool."""
        assert service._cases is None
        assert service._documents is None
        assert service._audit is None


class TestNoHardcodedAdapters:
    """The regression that motivated this file."""

    def test_service_has_no_module_level_adapter_construction(self):
        """A constant pinned to the filesystem is how the Postgres path died.

        Anchored to column zero: the same assignments *inside* the accessors are
        the correct implementation, so only module-level ones are the defect.
        """
        source = (paths.AGENT_PKG_DIR / "api" / "service.py").read_text()
        offenders = [
            line
            for line in source.splitlines()
            if re.match(
                r"^_(cases|documents|audit)\s*=\s*\w+Repository\(|"
                r"^_audit\s*=\s*\w*AuditSink\(",
                line,
            )
        ]
        assert not offenders, (
            f"adapter pinned at import: {offenders}. Use the accessor so the "
            "Postgres implementation stays reachable."
        )

    def test_the_worker_uses_the_resolved_audit_sink(self):
        """A worker writing audit events to container-local disk loses them."""
        source = (paths.AGENT_PKG_DIR / "worker" / "__main__.py").read_text()
        assert "service.get_audit()" in source
        assert "JsonlAuditSink()" not in source

    def test_the_worker_uses_the_resolved_case_repository(self):
        source = (paths.AGENT_PKG_DIR / "worker" / "runner.py").read_text()
        assert "service.get_cases()" in source
        assert "service._cases" not in source


class TestTenantResolutionParity:
    """Local and Postgres must agree about refusing an unknown tenant.

    Inbound email is the one channel an attacker reaches without credentials.
    If the local implementation resolves any slug, a deployment that fell back
    to it would file mail under invented tenants.
    """

    def test_the_known_tenant_resolves(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTH_DEV_TENANT", "acme")
        repo = JsonDocumentRepository(tmp_path / "documents.json")
        assert repo.tenant_id_for_slug("acme") == "acme"

    @pytest.mark.parametrize("slug", ["globex", "attacker", "admin", ""])
    def test_an_unknown_slug_is_refused(self, tmp_path, monkeypatch, slug):
        monkeypatch.setenv("AUTH_DEV_TENANT", "acme")
        repo = JsonDocumentRepository(tmp_path / "documents.json")
        assert repo.tenant_id_for_slug(slug) is None

    def test_a_tenant_holding_documents_still_resolves(
        self, tmp_path, monkeypatch
    ):
        """A local corpus predating AUTH_DEV_TENANT keeps working."""
        monkeypatch.setenv("AUTH_DEV_TENANT", "acme")
        repo = JsonDocumentRepository(tmp_path / "documents.json")
        repo.create("legacy", storage_key="k", sha256="s")
        assert repo.tenant_id_for_slug("legacy") == "legacy"

    def test_both_implementations_expose_the_same_method(self):
        for repo in (JsonDocumentRepository, PgDocumentRepository):
            assert hasattr(repo, "tenant_id_for_slug")
            assert hasattr(repo, "find_by_sha")
            assert hasattr(repo, "create")
