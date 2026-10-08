"""One editor per invoice, and what happens to everyone else.

Two reviewers correcting the same invoice is not a rare race: it is a Monday
morning with two people working the same exception queue. Both read the
extraction artifact, both write it back whole, and the second save silently
discards the first person's corrections -- while the corrections store keeps
both, so the trail says two fields were fixed and the invoice shows one.

These tests pin the refusal, the expiry that stops a closed tab stranding a
record, and the admin's ability to break a lock somebody left behind.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.db.models import Base, Invoice, Tenant
from invoice_processing.db.repositories import PgCaseRepository, PgRecordLocks
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import LockHeld, RunContext

TENANT = "tenant-acme"
CASE = "case_001"
CLAIMS = {
    f"tok-{who}": {
        "user_id": f"u-{who}",
        "email": f"{who}@acme.test",
        "tenant_id": TENANT,
        "role": role,
    }
    for who, role in (
        ("dana", "clerk"),
        ("sam", "clerk"),
        ("ada", "approver"),
        ("root", "admin"),
        ("ren", "viewer"),
    )
}


class FakeVerifier:
    def verify(self, token):
        if token not in CLAIMS:
            raise auth.AuthError("Invalid token")
        return CLAIMS[token]


@pytest.fixture
def db(scratch_db, monkeypatch):
    url = scratch_db("locks.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
    engine = get_engine(url)
    Base.metadata.drop_all(engine)
    create_all(url)
    with tenant_session(TENANT, url) as session:
        session.add(Tenant(id=TENANT, slug="acme", name="Acme"))
    with tenant_session(TENANT, url) as session:
        session.add(
            Invoice(
                tenant_id=TENANT,
                case_id=CASE,
                vendor_name="QuickFix HVAC",
                invoice_number="INV-1",
                currency="AUD",
                total_inc_tax=1100,
                fields={},
            )
        )
    # The artifact the edit path reads and writes back.
    PgCaseRepository(url).put_artifact(
        RunContext(tenant_id=TENANT, case_id=CASE),
        "extraction",
        {"invoice": {"vendor_name": "QuickFix HVAC", "po_number": "PO-1"}},
    )
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    return url


@pytest.fixture
def client(db):
    def make(who="dana"):
        return TestClient(app, headers={"Authorization": f"Bearer tok-{who}"})

    return make


def edit(client, who="dana", field="po_number", value="PO-2"):
    return client(who).patch(
        f"/api/invoices/{CASE}/fields",
        json={"edits": [{"field": field, "value": value}]},
    )


class TestTakingTheLock:
    def test_the_first_caller_gets_it(self, db, client):
        response = client("dana").post(f"/api/invoices/{CASE}/lock")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["mine"] is True
        assert body["lock"]["holder"] == "dana@acme.test"

    def test_the_second_caller_is_told_who_has_it(self, db, client):
        client("dana").post(f"/api/invoices/{CASE}/lock")

        response = client("sam").post(f"/api/invoices/{CASE}/lock")
        assert response.status_code == 423
        # Named, not just refused: "locked" with no holder is unactionable.
        assert response.json()["detail"]["holder"] == "dana@acme.test"

    def test_the_holder_can_renew_without_losing_it(self, db, client):
        first = client("dana").post(f"/api/invoices/{CASE}/lock").json()
        again = client("dana").post(f"/api/invoices/{CASE}/lock")

        assert again.status_code == 200
        assert again.json()["lock"]["expires_at"] >= first["lock"]["expires_at"]

    def test_an_unknown_case_cannot_be_locked(self, db, client):
        assert client("dana").post("/api/invoices/nope/lock").status_code == 404

    def test_locking_needs_clerk(self, db, client):
        assert (
            client("ren").post(f"/api/invoices/{CASE}/lock").status_code == 403
        )


class TestWhatALockRefuses:
    def test_someone_else_cannot_edit(self, db, client):
        client("dana").post(f"/api/invoices/{CASE}/lock")

        response = edit(client, "sam")
        assert response.status_code == 423
        assert response.json()["detail"]["holder"] == "dana@acme.test"

    def test_someone_else_cannot_approve(self, db, client):
        """A decision on a record being corrected is the case for locking at
        all: the approval lands on values that are about to change."""
        client("dana").post(f"/api/invoices/{CASE}/lock")

        response = client("ada").post(
            f"/api/invoices/{CASE}/actions", json={"action": "approve"}
        )
        assert response.status_code == 423

    def test_the_holder_still_can(self, db, client):
        client("dana").post(f"/api/invoices/{CASE}/lock")

        assert edit(client, "dana").status_code == 200

    def test_an_unlocked_record_is_editable_by_anyone_who_may(self, db, client):
        """Not holding a lock stays legal. A lock excludes other people; it is
        not a permit its holder must first obtain."""
        assert edit(client, "sam").status_code == 200


class TestACorrectionReachesTheSheet:
    """The artifact is the record; the columns are what the sheet queries.

    Correcting a field wrote the artifact and left the column behind, so an
    edit made in the drawer showed the old value in the row beside it -- a
    save that reads as broken.
    """

    def test_the_typed_column_follows_the_edit(self, db, client):
        edit(client, "dana", field="po_number", value="PO-9999")

        with tenant_session(TENANT, db) as session:
            stored = session.scalar(
                select(Invoice.po_number).where(
                    Invoice.tenant_id == TENANT, Invoice.case_id == CASE
                )
            )
        assert stored == "PO-9999"

    def test_the_sheet_can_then_filter_on_it(self, db, client):
        edit(client, "dana", field="po_number", value="PO-9999")

        rows = client("dana").get(
            "/api/sheet/invoices", params={"status": "all", "q": "PO-9999"}
        )
        assert [row["po_number"] for row in rows.json()["rows"]] == ["PO-9999"]


class TestExpiry:
    def test_a_lapsed_lock_stops_refusing(self, db, client):
        """A closed tab must not strand a record, and nobody is watching to
        notice that it did."""
        locks = PgRecordLocks(db)
        locks.acquire(TENANT, CASE, "dana@acme.test", ttl=timedelta(seconds=-1))

        assert locks.get(TENANT, CASE) is None
        assert edit(client, "sam").status_code == 200

    def test_a_lapsed_lock_can_be_taken_over(self, db, client):
        locks = PgRecordLocks(db)
        locks.acquire(TENANT, CASE, "dana@acme.test", ttl=timedelta(seconds=-1))

        mine, lock = locks.acquire(TENANT, CASE, "sam@acme.test")
        assert mine is True
        assert lock["holder"] == "sam@acme.test"

    def test_a_lock_lasts_a_quarter_of_an_hour(self, db):
        _, lock = PgRecordLocks(db).acquire(TENANT, CASE, "dana@acme.test")

        expires = datetime.fromisoformat(lock["expires_at"])
        remaining = expires - datetime.now(timezone.utc)
        assert timedelta(minutes=14) < remaining <= timedelta(minutes=15)


class TestReleasing:
    def test_the_holder_can_release(self, db, client):
        client("dana").post(f"/api/invoices/{CASE}/lock")

        released = client("dana").delete(f"/api/invoices/{CASE}/lock")
        assert released.status_code == 200
        assert released.json()["released"]["holder"] == "dana@acme.test"
        assert edit(client, "sam").status_code == 200

    def test_a_clerk_cannot_release_someone_elses(self, db, client):
        client("dana").post(f"/api/invoices/{CASE}/lock")

        response = client("sam").delete(f"/api/invoices/{CASE}/lock")
        assert response.status_code == 423

    def test_an_admin_can_break_one(self, db, client):
        """Somebody has to be able to, or a lock outlives the person who left
        it and the invoice cannot be paid."""
        client("dana").post(f"/api/invoices/{CASE}/lock")

        broken = client("root").delete(
            f"/api/invoices/{CASE}/lock", params={"force": "true"}
        )
        assert broken.status_code == 200
        assert broken.json()["released"]["holder"] == "dana@acme.test"
        assert edit(client, "sam").status_code == 200

    def test_breaking_needs_admin(self, db, client):
        client("dana").post(f"/api/invoices/{CASE}/lock")

        response = client("sam").delete(
            f"/api/invoices/{CASE}/lock", params={"force": "true"}
        )
        assert response.status_code == 403

    def test_releasing_nothing_is_not_an_error(self, db, client):
        response = client("dana").delete(f"/api/invoices/{CASE}/lock")
        assert response.status_code == 200
        assert response.json()["released"] is None


class TestTheDetailSaysWhoIsEditing:
    def test_the_lock_is_on_the_invoice(self, db, client):
        client("dana").post(f"/api/invoices/{CASE}/lock")

        detail = client("sam").get(f"/api/invoices/{CASE}").json()
        assert detail["lock"]["holder"] == "dana@acme.test"

    def test_no_lock_reads_as_none(self, db, client):
        assert client("sam").get(f"/api/invoices/{CASE}").json()["lock"] is None


class TestAudit:
    def test_taking_and_breaking_are_both_recorded(self, db, client):
        events = []

        class RecordingAudit:
            def emit(self, ctx, event, payload, before=None):
                events.append(event)

        client("dana").post(f"/api/invoices/{CASE}/lock")
        service._audit = RecordingAudit()
        try:
            client("dana").post(f"/api/invoices/{CASE}/lock")
            client("root").delete(
                f"/api/invoices/{CASE}/lock", params={"force": "true"}
            )
        finally:
            service._audit = None

        assert events == ["record_locked", "record_lock_broken"]


class TestWithoutTheTable:
    def test_locking_reports_itself_unavailable(self, db, client, monkeypatch):
        """An unmigrated database must not turn every edit into a 500.

        It says locking is unavailable instead of handing back a lock that
        excludes nobody -- and the edit path keeps working, because refusing
        writes over a missing feature would be worse than the race it prevents.
        """
        monkeypatch.setattr(service, "_locks", None)
        monkeypatch.setattr(service, "_table_exists", lambda *a: False)

        response = client("dana").post(f"/api/invoices/{CASE}/lock")
        assert response.status_code == 200
        assert response.json() == {
            "lock": None,
            "mine": False,
            "locking": "unavailable",
        }
        assert edit(client, "sam").status_code == 200


class TestTheRepositoryDirectly:
    def test_release_raises_rather_than_lying(self, db):
        locks = PgRecordLocks(db)
        locks.acquire(TENANT, CASE, "dana@acme.test")

        with pytest.raises(LockHeld) as refused:
            locks.release(TENANT, CASE, "sam@acme.test")
        assert refused.value.lock["holder"] == "dana@acme.test"
