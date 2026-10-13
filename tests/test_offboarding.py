"""Suspending, reactivating and purging a tenant.

The purge is the one place this product deletes evidence, so the tests weigh
most on what it refuses and on what it must leave behind: nothing of the
tenant, everything of everyone else.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Integer,
    Numeric,
    func,
    insert,
    select,
)

from invoice_processing import offboarding
from invoice_processing.adapters import LocalBlobStore
from invoice_processing.admin.provision import Provisioner
from invoice_processing.db.models import TENANT_TABLES, Base, Tenant
from invoice_processing.db.repositories import PgAuditSink
from invoice_processing.db.session import create_all, get_engine, tenant_session

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def url(scratch_db):
    target = scratch_db("offboarding.db")
    engine = get_engine(target)
    Base.metadata.drop_all(engine)
    create_all(target)
    return target


@pytest.fixture
def acme(url):
    return Provisioner(url=url).create_tenant("acme", "Acme Pty Ltd")["id"]


def _tenant(url, tenant_id):
    with tenant_session("", url) as session:
        tenant = session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        session.expunge(tenant)
        return tenant


def _actions(url, tenant_id):
    return [e.action for e in PgAuditSink(url).query(tenant_id)]


class TestSuspend:
    def test_suspends_records_and_audits(self, url, acme):
        result = offboarding.suspend(
            "acme", operator="vineet", reason="contract ended", url=url, now=NOW
        )
        assert result["status"] == "suspended"
        tenant = _tenant(url, acme)
        assert tenant.status == "suspended"
        assert tenant.settings["offboarding"] == {
            "suspended_at": NOW.isoformat(),
            "suspended_by": "vineet",
            "reason": "contract ended",
        }
        assert _actions(url, acme) == ["tenant_suspended"]

    def test_keeps_the_tenants_own_settings(self, url, acme):
        with tenant_session("", url) as session:
            tenant = session.get(Tenant, acme)
            tenant.settings = {"currency": "AUD", "tolerance": 5}
        offboarding.suspend(
            "acme", operator="vineet", reason="r", url=url, now=NOW
        )
        offboarding.reactivate("acme", operator="vineet", url=url, now=NOW)
        settings = _tenant(url, acme).settings
        assert settings["currency"] == "AUD"
        assert settings["tolerance"] == 5

    def test_refuses_a_tenant_that_is_not_active(self, url, acme):
        offboarding.suspend(
            "acme", operator="vineet", reason="r", url=url, now=NOW
        )
        with pytest.raises(offboarding.OffboardingError, match="suspended"):
            offboarding.suspend(
                "acme", operator="vineet", reason="r", url=url, now=NOW
            )

    def test_refuses_an_unknown_slug(self, url):
        with pytest.raises(offboarding.OffboardingError, match="No tenant"):
            offboarding.suspend("nobody", operator="v", reason="r", url=url)

    @pytest.mark.parametrize(("operator", "reason"), [("", "r"), ("v", " ")])
    def test_needs_an_operator_and_a_reason(self, url, acme, operator, reason):
        with pytest.raises(offboarding.OffboardingError, match="required"):
            offboarding.suspend(
                "acme", operator=operator, reason=reason, url=url
            )
        assert _tenant(url, acme).status == "active"


class TestReactivate:
    def test_reactivates_and_keeps_the_history(self, url, acme):
        offboarding.suspend(
            "acme", operator="vineet", reason="r", url=url, now=NOW
        )
        later = NOW + timedelta(days=2)
        offboarding.reactivate("acme", operator="ops", url=url, now=later)
        tenant = _tenant(url, acme)
        assert tenant.status == "active"
        record = tenant.settings["offboarding"]
        assert record["suspended_at"] == NOW.isoformat()
        assert record["reactivated_at"] == later.isoformat()
        assert record["reactivated_by"] == "ops"
        # sorted: two rows in one second have no reliable order
        assert sorted(_actions(url, acme)) == [
            "tenant_reactivated",
            "tenant_suspended",
        ]

    def test_refuses_an_active_tenant(self, url, acme):
        with pytest.raises(offboarding.OffboardingError, match="active"):
            offboarding.reactivate("acme", operator="ops", url=url)


class TestCli:
    def test_suspend_and_reactivate_from_the_command_line(
        self, url, acme, monkeypatch, capsys
    ):
        from invoice_processing.admin import provision  # noqa: PLC0415

        monkeypatch.setenv("DATABASE_URL", url)
        assert (
            provision.main(
                [
                    "suspend-tenant",
                    "--tenant",
                    "acme",
                    "--operator",
                    "vineet",
                    "--reason",
                    "contract ended",
                ]
            )
            == 0
        )
        assert _tenant(url, acme).status == "suspended"
        assert (
            provision.main(
                ["reactivate-tenant", "--tenant", "acme", "--operator", "v"]
            )
            == 0
        )
        assert _tenant(url, acme).status == "active"
        assert (
            provision.main(
                ["reactivate-tenant", "--tenant", "acme", "--operator", "v"]
            )
            == 1
        )
        assert "active" in capsys.readouterr().err


class TestTheApiRefusesASuspendedTenant:
    @pytest.fixture
    def api(self, url, tmp_path, monkeypatch):
        from invoice_processing.adapters import (  # noqa: PLC0415
            JsonlAuditSink,
        )
        from invoice_processing.api import auth, service  # noqa: PLC0415
        from tests.test_signup_api import FakeVerifier  # noqa: PLC0415

        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        monkeypatch.setattr(auth, "_verifier", FakeVerifier())
        monkeypatch.setattr(
            service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
        )

    def _get(self, tenant_id):
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from invoice_processing.api.service import app  # noqa: PLC0415

        token = f"ap@acme.test|uid-1|{tenant_id}|"
        return TestClient(app).get(
            "/api/invoices", headers={"Authorization": f"Bearer {token}"}
        )

    @pytest.mark.parametrize("with_row", [True, False])
    def test_every_request_is_refused_once_suspended(
        self, url, acme, api, with_row
    ):
        if with_row:
            Provisioner(url=url).add_user(
                "acme", "ap@acme.test", "admin", "uid-1"
            )
        assert self._get(acme).status_code == 200
        offboarding.suspend("acme", operator="v", reason="r", url=url)
        refused = self._get(acme)
        assert refused.status_code == 403
        assert "suspended" in refused.json()["detail"]
        offboarding.reactivate("acme", operator="v", url=url)
        assert self._get(acme).status_code == 200


def seed_every_tenant_table(url, tenant_id):
    """One row in every tenant table, built from the schema itself.

    So a table added later is seeded, and so checked by the purge test,
    without anyone remembering to.
    """
    ids = {}
    with tenant_session(tenant_id, url) as session:
        for table in Base.metadata.sorted_tables:
            if table.name not in TENANT_TABLES:
                continue
            row = {}
            for col in table.columns:
                fk = next(iter(col.foreign_keys), None)
                if col.name == "tenant_id":
                    row[col.name] = tenant_id
                elif fk is not None:
                    parent = fk.column.table.name
                    if parent in ids:
                        row[col.name] = ids[parent]
                    elif not col.nullable:
                        raise AssertionError(f"{table.name}.{col.name}")
                elif col.primary_key and not isinstance(col.type, Integer):
                    row[col.name] = str(uuid.uuid4())
                elif col.nullable or col.default or col.server_default:
                    continue
                elif isinstance(col.type, Boolean):
                    row[col.name] = False
                elif isinstance(col.type, Integer):
                    row[col.name] = 1
                elif isinstance(col.type, Numeric):
                    row[col.name] = Decimal("1")
                elif isinstance(col.type, DateTime):
                    row[col.name] = NOW
                elif isinstance(col.type, Date):
                    row[col.name] = NOW.date()
                elif col.type.__class__.__name__ in ("JSON", "JSONB"):
                    row[col.name] = {}
                else:
                    length = getattr(col.type, "length", None) or 64
                    row[col.name] = f"{col.name}-{uuid.uuid4().hex}"[:length]
            result = session.execute(insert(table).values(row))
            ids[table.name] = row.get("id", result.inserted_primary_key[0])


def _count(url, tenant_id, table):
    model = Base.metadata.tables[table]
    with tenant_session(tenant_id, url) as session:
        return session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.c.tenant_id == tenant_id)
        )


@pytest.fixture
def stores(tmp_path):
    made = {}

    def blobs_for(tenant_id):
        return made.setdefault(
            tenant_id, LocalBlobStore(tmp_path / "blobs" / tenant_id)
        )

    return blobs_for


@pytest.fixture
def two_tenants(url, stores):
    """Acme (to be purged) and Globex (to be left alone), each with a row in
    every tenant table and two documents."""
    p = Provisioner(url=url)
    acme = p.create_tenant("acme", "Acme Pty Ltd")["id"]
    globex = p.create_tenant("globex", "Globex")["id"]
    for tenant_id in (acme, globex):
        seed_every_tenant_table(url, tenant_id)
        stores(tenant_id).put("documents/a.pdf", b"%PDF a")
        stores(tenant_id).put("cases/c1/page-1.png", b"png")
    offboarding.suspend(
        "acme", operator="vineet", reason="contract ended", url=url, now=NOW
    )
    return acme, globex


def _purge(url, stores, *, now=NOW + timedelta(days=31), **kwargs):
    kwargs.setdefault("operator", "vineet")
    kwargs.setdefault("confirm", "acme")
    return offboarding.purge(
        "acme", blobs_for=stores, url=url, now=now, **kwargs
    )


class TestLocalBlobStore:
    def test_lists_and_deletes_inside_its_root(self, tmp_path):
        store = LocalBlobStore(tmp_path / "t1")
        assert store.list_keys() == []
        store.put("documents/a.pdf", b"a")
        store.put("cases/c1/p.png", b"p")
        assert store.list_keys() == ["cases/c1/p.png", "documents/a.pdf"]
        store.delete("documents/a.pdf")
        assert store.list_keys() == ["cases/c1/p.png"]

    def test_delete_refuses_a_key_that_escapes(self, tmp_path):
        (tmp_path / "other.pdf").write_bytes(b"x")
        with pytest.raises(ValueError):
            LocalBlobStore(tmp_path / "t1").delete("../other.pdf")
        assert (tmp_path / "other.pdf").exists()


class TestPurge:
    def test_removes_every_row_and_document_of_the_tenant(
        self, url, stores, two_tenants
    ):
        acme, _ = two_tenants
        record = _purge(url, stores)
        for table in TENANT_TABLES:
            assert _count(url, acme, table) == 0, table
        assert stores(acme).list_keys() == []
        assert record["blobs_deleted"] == 2
        assert set(record["rows_deleted"]) == set(TENANT_TABLES)
        # suspend wrote one audit row on top of the seeded one
        assert record["rows_deleted"]["audit_events"] == 2
        assert all(n >= 1 for n in record["rows_deleted"].values())

    def test_leaves_every_other_tenant_alone(self, url, stores, two_tenants):
        _, globex = two_tenants
        _purge(url, stores)
        for table in TENANT_TABLES:
            assert _count(url, globex, table) >= 1, table
        assert len(stores(globex).list_keys()) == 2

    def test_keeps_the_tenants_row_as_the_record(
        self, url, stores, two_tenants
    ):
        acme, _ = two_tenants
        later = NOW + timedelta(days=31)
        record = _purge(url, stores, now=later)
        tenant = _tenant(url, acme)
        assert (tenant.slug, tenant.name, tenant.status) == (
            "acme",
            "Acme Pty Ltd",
            "purged",
        )
        assert list(tenant.settings) == ["offboarding"]
        kept = tenant.settings["offboarding"]
        assert kept["suspended_at"] == NOW.isoformat()
        assert kept["purged_at"] == later.isoformat()
        assert kept["purged_by"] == "vineet"
        assert kept["skip_grace"] is False
        assert kept == {
            k: v for k, v in record.items() if k not in ("tenant_id", "slug")
        }

    def test_refuses_inside_the_recovery_window(self, url, stores, two_tenants):
        acme, _ = two_tenants
        with pytest.raises(offboarding.OffboardingError, match="30 days"):
            _purge(url, stores, now=NOW + timedelta(days=29))
        assert _tenant(url, acme).status == "suspended"
        assert len(stores(acme).list_keys()) == 2

    def test_the_window_counts_from_the_latest_suspension(
        self, url, stores, two_tenants
    ):
        offboarding.reactivate("acme", operator="v", url=url, now=NOW)
        again = NOW + timedelta(days=20)
        offboarding.suspend(
            "acme", operator="v", reason="r", url=url, now=again
        )
        with pytest.raises(offboarding.OffboardingError, match="30 days"):
            _purge(url, stores, now=NOW + timedelta(days=31))
        _purge(url, stores, now=again + timedelta(days=30))

    def test_skip_grace_needs_a_reason_and_is_recorded(
        self, url, stores, two_tenants
    ):
        with pytest.raises(offboarding.OffboardingError, match="reason"):
            _purge(url, stores, now=NOW, skip_grace=True)
        record = _purge(
            url,
            stores,
            now=NOW,
            skip_grace=True,
            reason="customer asked in writing, ticket 12",
        )
        assert record["skip_grace"] is True
        assert record["skip_reason"] == "customer asked in writing, ticket 12"

    def test_refuses_a_wrong_confirmation(self, url, stores, two_tenants):
        with pytest.raises(offboarding.OffboardingError, match="confirm"):
            _purge(url, stores, confirm="acme2")

    def test_refuses_an_active_tenant(self, url, stores, two_tenants):
        offboarding.reactivate("acme", operator="v", url=url)
        with pytest.raises(offboarding.OffboardingError, match="active"):
            _purge(url, stores)

    def test_refuses_a_purged_tenant(self, url, stores, two_tenants):
        _purge(url, stores)
        with pytest.raises(offboarding.OffboardingError, match="purged"):
            _purge(url, stores)

    def test_a_failure_after_the_documents_reruns_to_completion(
        self, url, stores, two_tenants, monkeypatch
    ):
        acme, _ = two_tenants
        real = offboarding._delete_rows

        def crash(*args, **kwargs):
            raise RuntimeError("connection lost")

        monkeypatch.setattr(offboarding, "_delete_rows", crash)
        with pytest.raises(RuntimeError):
            _purge(url, stores)
        assert _tenant(url, acme).status == "suspended"
        assert stores(acme).list_keys() == []
        monkeypatch.setattr(offboarding, "_delete_rows", real)
        record = _purge(url, stores)
        assert record["blobs_deleted"] == 0
        assert _count(url, acme, "invoices") == 0

    def test_rows_that_survive_the_deletes_abort_the_purge(
        self, url, stores, two_tenants, monkeypatch
    ):
        """What a missing DELETE policy looks like on Postgres: the delete
        removes nothing and says nothing. The re-count catches it."""
        acme, _ = two_tenants
        monkeypatch.setattr(
            offboarding, "_delete_rows", lambda session, tenant_id: {}
        )
        with pytest.raises(offboarding.OffboardingError, match="still holds"):
            _purge(url, stores)
        assert _tenant(url, acme).status == "suspended"


class TestOwnerRole:
    def test_refuses_postgres_without_the_owner_url(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+pg8000://app@h/db")
        monkeypatch.delenv("MIGRATE_DATABASE_URL", raising=False)
        monkeypatch.delenv("MIGRATE_CLOUD_SQL_USER", raising=False)
        with pytest.raises(offboarding.OffboardingError, match="owner"):
            offboarding.require_owner_role()

    def test_switches_to_the_owner_url(self, monkeypatch):
        import os  # noqa: PLC0415

        monkeypatch.setenv("DATABASE_URL", "postgresql+pg8000://app@h/db")
        monkeypatch.setenv(
            "MIGRATE_DATABASE_URL", "postgresql+pg8000://owner@h/db"
        )
        offboarding.require_owner_role()
        assert os.environ["DATABASE_URL"].startswith(
            "postgresql+pg8000://owner"
        )

    def test_sqlite_needs_no_owner(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'x.db'}")
        monkeypatch.delenv("MIGRATE_DATABASE_URL", raising=False)
        monkeypatch.delenv("MIGRATE_CLOUD_SQL_USER", raising=False)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        offboarding.require_owner_role()


class TestPurgeCli:
    def test_prints_one_json_record(
        self, url, two_tenants, monkeypatch, capsys, tmp_path
    ):
        from invoice_processing import paths  # noqa: PLC0415
        from invoice_processing.admin import provision  # noqa: PLC0415

        acme, _ = two_tenants
        monkeypatch.setenv("DATABASE_URL", url)
        # The owner URL is the same database here; on CI's Postgres the
        # command would otherwise refuse to run as the app role.
        monkeypatch.setenv("MIGRATE_DATABASE_URL", url)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        monkeypatch.delenv("GCS_BUCKET", raising=False)
        monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
        code = provision.main(
            [
                "purge-tenant",
                "--tenant",
                "acme",
                "--operator",
                "vineet",
                "--confirm",
                "acme",
                "--skip-grace",
                "--reason",
                "test",
            ]
        )
        assert code == 0
        line = capsys.readouterr().out.strip().splitlines()[-1]
        record = json.loads(line)
        assert record["tenant_id"] == acme
        assert record["slug"] == "acme"
