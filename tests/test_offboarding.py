"""Suspending, reactivating and purging a tenant.

The purge is the one place this product deletes evidence, so the tests weigh
most on what it refuses and on what it must leave behind: nothing of the
tenant, everything of everyone else.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from invoice_processing import offboarding
from invoice_processing.admin.provision import Provisioner
from invoice_processing.db.models import Base, Tenant
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
