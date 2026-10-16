"""The check run after any restore, however the restore was made."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from invoice_processing.db import restore_check
from tests.pg_roles import bootstrapped_database, pg_only

TENANT = "66666666-6666-6666-6666-666666666666"


class TestCompare:
    def test_equal_counts_pass(self):
        rows = {TENANT: {"invoices": 3}}
        assert restore_check.compare(rows, rows) == []

    def test_a_difference_names_the_tenant_and_table(self):
        failures = restore_check.compare(
            {TENANT: {"invoices": 3}}, {TENANT: {"invoices": 2}}
        )
        assert failures == [f"{TENANT} invoices: source 3, restored 2"]

    def test_a_missing_tenant_is_a_difference(self):
        failures = restore_check.compare({TENANT: {"invoices": 1}}, {})
        assert failures == [f"{TENANT} invoices: source 1, restored 0"]


def test_the_head_is_the_newest_migration():
    assert restore_check.repo_head() == "f4a5b6c7d8e9"


@pg_only
class TestOnPostgres:
    @pytest.fixture
    def db(self):
        with bootstrapped_database() as db:
            su = create_engine(db.url, isolation_level="AUTOCOMMIT")
            with su.connect() as c:
                c.execute(
                    text(
                        "insert into tenants (id, slug, name, region, status, "
                        "settings, created_at, updated_at) values "
                        f"('{TENANT}', 'acme', 'Acme', 'au', 'suspended', "
                        "'{}', now(), now())"
                    )
                )
                c.execute(
                    text(
                        "insert into audit_events (id, tenant_id, "
                        "occurred_at, actor_type, action) values "
                        f"('e1', '{TENANT}', now(), 'user', 'x')"
                    )
                )
            db.su = su
            yield db
            su.dispose()

    def _check(self, db):
        owner = create_engine(db.owner_url)
        try:
            with owner.connect() as c:
                return restore_check.check(c, db.app)
        finally:
            owner.dispose()

    def test_a_correctly_built_database_passes(self, db):
        assert self._check(db) == []

    def test_counts_are_read_per_tenant_as_the_owner(self, db):
        owner = create_engine(db.owner_url)
        try:
            with owner.connect() as c:
                rows, newest = restore_check.counts(c)
        finally:
            owner.dispose()
        assert rows[TENANT]["audit_events"] == 1
        assert rows[TENANT]["invoices"] == 0
        assert newest is not None

    def test_rls_not_forced_fails(self, db):
        with db.su.connect() as c:
            c.execute(text("alter table invoices no force row level security"))
        assert any(f.startswith("invoices:") for f in self._check(db))

    def test_an_app_role_that_can_delete_audit_rows_fails(self, db):
        with db.su.connect() as c:
            c.execute(text(f"grant delete on audit_events to {db.app}"))
        assert f"app role {db.app} can DELETE audit_events" in self._check(db)

    def test_a_missing_purge_policy_fails(self, db):
        with db.su.connect() as c:
            c.execute(text("drop policy audit_events_purge on audit_events"))
        assert any("audit_events_purge" in f for f in self._check(db))

    def test_a_wrong_head_fails(self, db):
        with db.su.connect() as c:
            c.execute(text("update alembic_version set version_num = 'x'"))
        assert any(f.startswith("schema is at x") for f in self._check(db))

    def test_main_compares_and_reports(self, db, capsys):
        owner = db.owner_url.render_as_string(hide_password=False)
        code = restore_check.main(
            ["--restored", owner, "--source", owner, "--app-role", db.app]
        )
        out = capsys.readouterr().out
        assert code == 0, out
        assert "acme: suspended" in out
        assert out.strip().endswith("PASS")
