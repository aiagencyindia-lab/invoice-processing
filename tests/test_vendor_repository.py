"""Vendors live in the database, not a file: the API and the worker are
separate containers in production, so a JSON master written by one was
invisible to the other. Bank details and the dual-control queue that governs
them need a record both can read."""

import pytest
from fresh_schema import fresh_schema
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from invoice_processing.api import service
from invoice_processing.db import models
from invoice_processing.db.repositories import (
    ChangeNotPending,
    PendingChangeExists,
    PgVendorRepository,
    SelfApproval,
)
from invoice_processing.db.session import create_all, tenant_session


class TestSchema:
    def test_vendor_tables_are_tenant_tables(self):
        assert "vendors" in models.TENANT_TABLES
        assert "vendor_bank_changes" in models.TENANT_TABLES

    def test_create_all_builds_the_tables_and_document_columns(
        self, scratch_db
    ):
        engine = create_all(scratch_db("schema.db"))
        inspector = inspect(engine)
        assert inspector.has_table("vendors")
        assert inspector.has_table("vendor_bank_changes")
        document_columns = {
            c["name"] for c in inspector.get_columns("documents")
        }
        assert {"sender", "reply_to"} <= document_columns
        vendor_columns = {c["name"] for c in inspector.get_columns("vendors")}
        assert {
            "name",
            "tax_id",
            "kind",
            "normalized_name",
            "email_domain",
            "bank_account_name",
            "bank_account_number",
            "bank_code",
            "bank_iban",
            "bank_verified_at",
            "bank_verified_by",
        } <= vendor_columns


T = "tenant-a"
ACME = {
    "name": "Cloudhaven Infra Services Pvt Ltd",
    "tax_id": "29ZOICE4828P1ZD",
    "kind": "service",
}
BANK = {
    "bank_account_name": "Cloudhaven Infra Services Pvt Ltd",
    "bank_account_number": "901031925572",
    "bank_code": "KCBL0230056",
    "bank_iban": None,
}


@pytest.fixture
def repo(scratch_db):
    url = scratch_db("vendors.db")
    fresh_schema(url)
    for tenant_id, slug in ((T, "a"), ("tenant-b", "b")):
        with tenant_session(tenant_id, url) as session:
            session.add(models.Tenant(id=tenant_id, name=slug, slug=slug))
    return PgVendorRepository(url)


def _acme(repo):
    repo.upsert_many(T, [ACME])
    return repo.find_by_name(T, ACME["name"])


class TestGuard:
    def test_no_repository_when_the_vendors_table_is_missing(self):
        # The suite's default DATABASE_URL is an empty in-memory SQLite.
        assert service.get_vendors() is None

    def test_repository_when_the_table_exists(self, repo, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", repo.url)
        monkeypatch.setattr(service, "_vendors", None)
        assert isinstance(service.get_vendors(), PgVendorRepository)


class TestUpsert:
    def test_upsert_is_keyed_by_normalised_name_and_keeps_bank_fields(
        self, repo
    ):
        assert repo.upsert_many(T, [ACME]) == 1
        vendor = repo.find_by_name(
            T, "Cloudhaven Infra Services Private Limited"
        )
        assert vendor["tax_id"] == ACME["tax_id"]
        change = repo.propose(
            T,
            vendor["id"],
            proposed=BANK,
            reason="onboarding",
            proposed_by="clerk@a",
        )
        repo.decide(
            T, change["id"], action="approve", decided_by="boss@a", note=None
        )
        # A re-import of the plain list must not wipe what two people verified.
        repo.upsert_many(T, [{**ACME, "bank_account_number": "000"}])
        again = repo.get(T, vendor["id"])
        assert again["bank_account_number"] == BANK["bank_account_number"]
        assert again["bank_verified_by"] == "boss@a"

    def test_list_is_tenant_scoped(self, repo):
        repo.upsert_many(T, [ACME])
        assert repo.list("tenant-b") == []


class TestDualControl:
    def test_pending_change_does_not_touch_the_vendor(self, repo):
        vendor = _acme(repo)
        change = repo.propose(
            T,
            vendor["id"],
            proposed=BANK,
            reason="new account",
            proposed_by="clerk@a",
        )
        assert change["status"] == "pending"
        assert repo.get(T, vendor["id"])["bank_account_number"] is None
        assert repo.get(T, vendor["id"])["pending_change_id"] == change["id"]

    def test_second_pending_change_is_refused(self, repo):
        vendor = _acme(repo)
        repo.propose(
            T, vendor["id"], proposed=BANK, reason="x", proposed_by="clerk@a"
        )
        with pytest.raises(PendingChangeExists):
            repo.propose(
                T,
                vendor["id"],
                proposed=BANK,
                reason="y",
                proposed_by="clerk@a",
            )

    def test_proposer_cannot_approve(self, repo):
        vendor = _acme(repo)
        change = repo.propose(
            T, vendor["id"], proposed=BANK, reason="x", proposed_by="clerk@a"
        )
        with pytest.raises(SelfApproval):
            repo.decide(
                T,
                change["id"],
                action="approve",
                decided_by="clerk@a",
                note=None,
            )

    def test_approve_copies_values_and_stamps_verification(self, repo):
        vendor = _acme(repo)
        change = repo.propose(
            T, vendor["id"], proposed=BANK, reason="x", proposed_by="clerk@a"
        )
        decided = repo.decide(
            T,
            change["id"],
            action="approve",
            decided_by="boss@a",
            note="called them",
        )
        assert decided["status"] == "approved"
        after = repo.get(T, vendor["id"])
        assert after["bank_code"] == "KCBL0230056"
        assert after["bank_verified_by"] == "boss@a"
        assert after["bank_verified_at"] is not None
        assert after["pending_change_id"] is None

    def test_first_verification_is_not_a_recent_change(self, repo):
        vendor = _acme(repo)
        first = repo.propose(
            T, vendor["id"], proposed=BANK, reason="x", proposed_by="clerk@a"
        )
        repo.decide(
            T, first["id"], action="approve", decided_by="boss@a", note=None
        )
        (record,) = repo.load_records(T)
        assert record["last_bank_change_at"] is None
        second = repo.propose(
            T,
            vendor["id"],
            proposed={**BANK, "bank_account_number": "111122223333"},
            reason="new account",
            proposed_by="clerk@a",
        )
        repo.decide(
            T, second["id"], action="approve", decided_by="boss@a", note=None
        )
        (record,) = repo.load_records(T)
        assert record["last_bank_change_at"] is not None

    def test_reject_is_terminal(self, repo):
        vendor = _acme(repo)
        change = repo.propose(
            T, vendor["id"], proposed=BANK, reason="x", proposed_by="clerk@a"
        )
        repo.decide(
            T,
            change["id"],
            action="reject",
            decided_by="boss@a",
            note="not verified",
        )
        with pytest.raises(ChangeNotPending):
            repo.decide(
                T,
                change["id"],
                action="approve",
                decided_by="boss@a",
                note=None,
            )
        assert repo.get(T, vendor["id"])["bank_account_number"] is None

    def test_load_records_carry_bank_and_change_state(self, repo):
        vendor = _acme(repo)
        repo.propose(
            T, vendor["id"], proposed=BANK, reason="x", proposed_by="clerk@a"
        )
        (record,) = repo.load_records(T)
        assert record["name"] == ACME["name"]
        assert record["pending_change"] is True
        assert record["bank_account_number"] is None

    def test_self_approval_ignores_case_and_whitespace(self, repo):
        vendor = _acme(repo)
        change = repo.propose(
            T,
            vendor["id"],
            proposed=BANK,
            reason="x",
            proposed_by="Clerk@A.test",
        )
        with pytest.raises(SelfApproval):
            repo.decide(
                T,
                change["id"],
                action="approve",
                decided_by=" clerk@a.test ",
                note=None,
            )

    def test_database_refuses_a_second_pending_row(self, repo):
        vendor = _acme(repo)

        def pending():
            return models.VendorBankChange(
                tenant_id=T,
                vendor_id=vendor["id"],
                reason="x",
                proposed_by="c@a",
                status="pending",
            )

        with tenant_session(T, repo.url) as session:
            session.add(pending())
        with (
            pytest.raises(IntegrityError),
            tenant_session(T, repo.url) as session,
        ):
            session.add(pending())

    def test_propose_turns_a_lost_race_into_pending_change_exists(
        self, repo, monkeypatch
    ):
        vendor = _acme(repo)
        repo.propose(
            T, vendor["id"], proposed=BANK, reason="x", proposed_by="c@a"
        )
        # Blind the pre-check so only the index can object, as in a real race.
        monkeypatch.setattr(PgVendorRepository, "_pending_id", lambda *a: None)
        with pytest.raises(PendingChangeExists):
            repo.propose(
                T, vendor["id"], proposed=BANK, reason="y", proposed_by="c@a"
            )

    def test_upsert_counts_only_usable_records(self, repo):
        assert repo.upsert_many(T, [ACME, {"name": "  ", "tax_id": "x"}]) == 1
