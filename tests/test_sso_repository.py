"""Who may sign in where: the users directory and the domain bindings."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import DBAPIError

from invoice_processing.db import repositories
from invoice_processing.db.models import AuditEvent, Base, Tenant, User
from invoice_processing.db.repositories import (
    DomainTaken,
    PgSsoDomainRepository,
    PgUserDirectory,
)
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext

ACME = "tenant-acme"
GLOBEX = "tenant-globex"
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(scratch_db):
    url = scratch_db("sso.db")
    engine = get_engine(url)
    Base.metadata.drop_all(engine)
    create_all(url)
    for tenant_id, slug, name in (
        (ACME, "acme", "Acme Pty Ltd"),
        (GLOBEX, "globex", "Globex Inc"),
    ):
        with tenant_session(tenant_id, url) as session:
            session.add(Tenant(id=tenant_id, slug=slug, name=name))
    return url


def user(session, tenant_id=ACME, **overrides) -> User:
    values = {"tenant_id": tenant_id, "email": "pat@acme.com", "role": "clerk"}
    values.update(overrides)
    row = User(**values)
    session.add(row)
    session.flush()
    return row


class TestDirectory:
    def test_find_returns_the_row_with_its_tenant_name(self, db):
        with tenant_session(ACME, db) as session:
            user(session, uid="fb-1")
        found = PgUserDirectory(db).find(ACME, "pat@acme.com")
        assert found["role"] == "clerk" and found["is_active"] is True
        assert found["uid"] == "fb-1" and found["tenant_name"] == "Acme Pty Ltd"
        assert found["last_login_at"] is None

    def test_find_is_case_insensitive_on_email(self, db):
        with tenant_session(ACME, db) as session:
            user(session)
        assert PgUserDirectory(db).find(ACME, "Pat@Acme.com") is not None

    def test_case_variant_rows_resolve_to_the_oldest(self, db):
        with tenant_session(ACME, db) as session:
            user(session, email="pat@acme.com", role="viewer", created_at=NOW)
            user(
                session,
                email="PAT@acme.com",
                role="approver",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        for _ in range(3):
            assert PgUserDirectory(db).find(ACME, "pat@acme.com")["role"] == (
                "approver"
            )

    def test_get_reads_one_row_by_id_within_the_tenant(self, db):
        with tenant_session(ACME, db) as session:
            user_id = user(session, uid="fb-1").id
        directory = PgUserDirectory(db)
        assert directory.get(ACME, user_id)["email"] == "pat@acme.com"
        assert directory.get(GLOBEX, user_id) is None
        assert directory.get(ACME, "nope") is None

    def test_find_never_crosses_tenants(self, db):
        with tenant_session(ACME, db) as session:
            user(session)
        assert PgUserDirectory(db).find(GLOBEX, "pat@acme.com") is None

    def test_provision_creates_a_row_and_links_the_uid(self, db):
        created = PgUserDirectory(db).provision(
            ACME, "new@acme.com", "viewer", "fb-7", "New Person"
        )
        assert created["role"] == "viewer" and created["uid"] == "fb-7"
        assert created["display_name"] == "New Person"
        with tenant_session(ACME, db) as session:
            assert session.query(User).filter_by(email="new@acme.com").one()

    def test_provision_writes_its_audit_event_in_the_same_transaction(self, db):
        ctx = RunContext(tenant_id=ACME, case_id="", actor="new@acme.com")
        payload = {
            "email": "new@acme.com",
            "role": "viewer",
            "domain": "acme.com",
        }
        created = PgUserDirectory(db).provision(
            ACME,
            "new@acme.com",
            "viewer",
            "fb-7",
            None,
            audit=(ctx, "user_provisioned_by_sso", payload),
            now=NOW,
        )
        assert created["last_login_at"] is not None
        with tenant_session(ACME, db) as session:
            events = session.query(AuditEvent).all()
            assert [e.action for e in events] == ["user_provisioned_by_sso"]
            assert events[0].after == {**payload, "user_id": created["id"]}

    def test_an_audit_row_that_cannot_be_built_leaves_no_user(
        self, db, monkeypatch
    ):
        def broken(*args, **kwargs):
            raise RuntimeError("audit down")

        monkeypatch.setattr(repositories, "audit_event_row", broken)
        ctx = RunContext(tenant_id=ACME, case_id="", actor="new@acme.com")
        with pytest.raises(RuntimeError):
            PgUserDirectory(db).provision(
                ACME,
                "new@acme.com",
                "viewer",
                "fb-7",
                None,
                audit=(ctx, "user_provisioned_by_sso", {}),
            )
        assert PgUserDirectory(db).find(ACME, "new@acme.com") is None

    def test_link_uid_and_touch_login(self, db):
        with tenant_session(ACME, db) as session:
            row = user(session)
            user_id = row.id
        directory = PgUserDirectory(db)
        directory.link_uid(ACME, user_id, "fb-2")
        directory.touch_login(ACME, user_id, NOW)
        found = directory.find(ACME, "pat@acme.com")
        assert found["uid"] == "fb-2"
        assert found["last_login_at"].replace(tzinfo=None) == NOW.replace(
            tzinfo=None
        )

    def test_set_role_and_set_active_report_the_previous_value(self, db):
        with tenant_session(ACME, db) as session:
            user_id = user(session).id
        directory = PgUserDirectory(db)
        changed = directory.set_role(ACME, user_id, "approver")
        assert (changed["previous_role"], changed["role"]) == (
            "clerk",
            "approver",
        )
        off = directory.set_active(ACME, user_id, False)
        assert (off["previous_active"], off["is_active"]) == (True, False)
        assert directory.find(ACME, "pat@acme.com")["is_active"] is False

    def test_set_role_and_set_active_write_their_audit_rows(self, db):
        with tenant_session(ACME, db) as session:
            user_id = user(session).id
        ctx = RunContext(tenant_id=ACME, case_id="", actor="admin@acme.com")
        directory = PgUserDirectory(db)
        directory.set_role(
            ACME,
            user_id,
            "approver",
            audit=(
                ctx,
                "user_role_changed",
                {"role": "approver"},
                {"role": "clerk"},
            ),
        )
        directory.set_active(
            ACME, user_id, False, audit=(ctx, "user_deactivated", {}, None)
        )
        with tenant_session(ACME, db) as session:
            events = session.query(AuditEvent).all()
            assert sorted(e.action for e in events) == [
                "user_deactivated",
                "user_role_changed",
            ]
            changed = next(e for e in events if e.action == "user_role_changed")
            assert changed.before == {"role": "clerk"}

    @pytest.mark.parametrize("change", ["role", "active"])
    def test_an_audit_row_that_cannot_be_built_leaves_the_user_unchanged(
        self, db, monkeypatch, change
    ):
        with tenant_session(ACME, db) as session:
            user_id = user(session).id

        def broken(*args, **kwargs):
            raise RuntimeError("audit down")

        monkeypatch.setattr(repositories, "audit_event_row", broken)
        ctx = RunContext(tenant_id=ACME, case_id="", actor="admin@acme.com")
        directory = PgUserDirectory(db)
        with pytest.raises(RuntimeError):
            if change == "role":
                directory.set_role(
                    ACME,
                    user_id,
                    "admin",
                    audit=(ctx, "user_role_changed", {}, None),
                )
            else:
                directory.set_active(
                    ACME,
                    user_id,
                    False,
                    audit=(ctx, "user_deactivated", {}, None),
                )
        found = directory.find(ACME, "pat@acme.com")
        assert (found["role"], found["is_active"]) == ("clerk", True)

    def test_set_role_on_another_tenants_user_is_a_lookup_error(self, db):
        with tenant_session(ACME, db) as session:
            user_id = user(session).id
        with pytest.raises(LookupError):
            PgUserDirectory(db).set_role(GLOBEX, user_id, "admin")


class TestDomains:
    def test_add_list_remove(self, db):
        repo = PgSsoDomainRepository(db)
        added = repo.add(
            ACME,
            domain="acme.com",
            provider="google",
            default_role="viewer",
            created_by="admin@acme.com",
        )
        assert added["domain"] == "acme.com"
        assert [d["domain"] for d in repo.list(ACME)] == ["acme.com"]
        assert repo.list(GLOBEX) == []
        assert repo.remove(ACME, added["id"]) is True
        assert repo.list(ACME) == []

    def test_remove_never_crosses_tenants(self, db):
        repo = PgSsoDomainRepository(db)
        added = repo.add(
            ACME,
            domain="acme.com",
            provider="google",
            default_role="viewer",
            created_by="a",
        )
        assert repo.remove(GLOBEX, added["id"]) is False
        assert len(repo.list(ACME)) == 1

    def test_a_domain_is_unique_across_tenants(self, db):
        repo = PgSsoDomainRepository(db)
        repo.add(
            ACME,
            domain="acme.com",
            provider="google",
            default_role="viewer",
            created_by="a",
        )
        with pytest.raises(DomainTaken):
            repo.add(
                GLOBEX,
                domain="acme.com",
                provider="google",
                default_role="clerk",
                created_by="g",
            )

    def test_only_the_domain_constraint_means_taken(self, db):
        # A foreign-key failure: IntegrityError on SQLite, ProgrammingError
        # through pg8000. Either way it is not "taken".
        with pytest.raises(DBAPIError) as raised:
            PgSsoDomainRepository(db).add(
                "no-such-tenant",
                domain="nobody.com",
                provider="google",
                default_role="viewer",
                created_by="a",
            )
        assert not isinstance(raised.value, DomainTaken)

    def test_lookup_by_domain_needs_no_tenant(self, db):
        """The lookup happens before anyone knows which tenant the caller is."""
        PgSsoDomainRepository(db).add(
            ACME,
            domain="acme.com",
            provider="google",
            default_role="clerk",
            created_by="a",
        )
        found = PgUserDirectory(db).domain("acme.com")
        assert (
            found["tenant_id"] == ACME
            and found["tenant_name"] == "Acme Pty Ltd"
        )
        assert (
            found["default_role"] == "clerk" and found["provider"] == "google"
        )
        assert PgUserDirectory(db).domain("nobody.example") is None


class TestTenantStatus:
    def test_find_domain_and_tenant_status_carry_it(self, db):
        from invoice_processing.db.models import SsoDomain  # noqa: PLC0415

        tid = "33333333-3333-3333-3333-333333333333"
        with tenant_session(tid, db) as session:
            session.add(
                Tenant(id=tid, slug="closed", name="Closed", status="suspended")
            )
            session.flush()
            session.add(
                User(tenant_id=tid, email="ap@closed.test", role="clerk")
            )
            session.add(
                SsoDomain(
                    tenant_id=tid,
                    domain="closed.test",
                    provider="google",
                    default_role="clerk",
                    created_by="admin@closed.test",
                )
            )
        directory = PgUserDirectory(db)
        assert directory.find(tid, "ap@closed.test")["tenant_status"] == (
            "suspended"
        )
        assert directory.domain("closed.test")["tenant_status"] == "suspended"
        assert directory.tenant_status(tid) == "suspended"
        assert directory.tenant_status("no-such-tenant") is None
