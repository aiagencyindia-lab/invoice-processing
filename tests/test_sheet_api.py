"""The approved-invoice sheet: what it shows, and what it must never show.

The sheet is a reader over data the existing workflow already writes. These
tests pin two things: that it reads approval out of the workflow artifact
correctly, and that the answer to "which invoices are approved" cannot quietly
become "all of them".
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from invoice_processing.api import auth
from invoice_processing.api.service import app
from invoice_processing.db.models import (
    Base,
    Correction,
    Finding,
    Invoice,
    Tenant,
)
from invoice_processing.db.repositories import SheetRepository, _as_number
from invoice_processing.db.session import create_all, get_engine, tenant_session

TENANT = "tenant-acme"
CLAIMS = {
    f"tok-{role}": {
        "user_id": f"u-{role}",
        "email": f"{role}@acme.test",
        "tenant_id": TENANT,
        "role": role,
    }
    for role in ("viewer", "clerk", "approver", "admin")
}


class FakeVerifier:
    def verify(self, token):
        if token not in CLAIMS:
            raise auth.AuthError("Invalid token")
        return CLAIMS[token]


def approved(when="2026-08-15T09:00:00+00:00", by="approver@acme.test") -> dict:
    """The workflow artifact the approval flow writes. Not invented here."""
    return {"decision": "approve", "decided_by": by, "decided_at": when}


def invoice(session, tenant_id=TENANT, **overrides) -> Invoice:
    values = {
        "tenant_id": tenant_id,
        "case_id": overrides.pop("case_id", "case-1"),
        "vendor_name": "QuickFix HVAC",
        "invoice_number": "INV-1",
        "invoice_date": "2026-08-01",
        "currency": "AUD",
        "total_ex_tax": 1000,
        "tax_amount": 100,
        "total_inc_tax": 1100,
        "po_number": "PO-1",
        "vendor_tax_id": "51824753556",
        "due_date": "2026-09-01",
        "min_confidence": 1,
        "fields": {"workflow": approved()},
    }
    values.update(overrides)
    row = Invoice(**values)
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'sheet.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
    engine = get_engine(url)
    Base.metadata.drop_all(engine)
    create_all(url)
    with tenant_session(TENANT, url) as session:
        session.add(Tenant(id=TENANT, slug="acme", name="Acme"))
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    return url


@pytest.fixture
def client(db):
    def make(role="admin"):
        return TestClient(app, headers={"Authorization": f"Bearer tok-{role}"})

    return make


def rows(client, **params):
    response = client().get("/api/sheet/invoices", params=params)
    assert response.status_code == 200, response.text
    return response.json()


class TestOnlyApprovedByDefault:
    """The page is called "approved invoices". It has to mean it."""

    def test_an_approved_invoice_is_listed(self, db, client):
        with tenant_session(TENANT, db) as session:
            invoice(session)

        body = rows(client)
        assert body["total"] == 1
        assert body["rows"][0]["invoice_number"] == "INV-1"

    def test_an_unapproved_invoice_is_not(self, db, client):
        """Processed by the pipeline, never seen by a person."""
        with tenant_session(TENANT, db) as session:
            invoice(session, case_id="untouched", fields={})

        assert rows(client)["total"] == 0

    def test_a_rejected_invoice_is_not(self, db, client):
        with tenant_session(TENANT, db) as session:
            invoice(
                session,
                case_id="rejected",
                fields={"workflow": {"decision": "reject"}},
            )

        assert rows(client)["total"] == 0

    def test_the_other_statuses_can_be_asked_for_by_name(self, db, client):
        with tenant_session(TENANT, db) as session:
            invoice(session, case_id="a")
            invoice(
                session,
                case_id="r",
                fields={"workflow": {"decision": "reject"}},
            )
            invoice(session, case_id="p", fields={})

        assert rows(client, status="rejected")["total"] == 1
        assert rows(client, status="pending")["total"] == 1
        assert rows(client, status="all")["total"] == 3

    def test_approval_details_come_from_the_workflow_artifact(self, db, client):
        """Nothing was written for the sheet -- it reads what approval wrote."""
        with tenant_session(TENANT, db) as session:
            invoice(session)

        row = rows(client)["rows"][0]
        assert row["approved_by"] == "approver@acme.test"
        assert row["approved_at"] == "2026-08-15T09:00:00+00:00"


class TestFiltering:
    def _corpus(self, db):
        with tenant_session(TENANT, db) as session:
            invoice(
                session,
                case_id="a",
                vendor_name="QuickFix HVAC",
                invoice_number="INV-1",
                invoice_date="2026-07-02",
                total_inc_tax=500,
                currency="AUD",
            )
            invoice(
                session,
                case_id="b",
                vendor_name="Northwind Electrical",
                invoice_number="INV-2",
                invoice_date="2026-08-20",
                total_inc_tax=9000,
                currency="AUD",
                po_number=None,
            )
            invoice(
                session,
                case_id="c",
                vendor_name="Foobar Labs",
                invoice_number="INV-3",
                invoice_date="2026-09-01",
                total_inc_tax=250,
                currency="INR",
                tax_amount=None,
            )

    def test_search_matches_vendor_and_number(self, db, client):
        self._corpus(db)
        assert rows(client, q="northwind")["total"] == 1
        assert rows(client, q="INV-3")["total"] == 1
        assert rows(client, q="nothing here")["total"] == 0

    def test_vendor_and_currency(self, db, client):
        self._corpus(db)
        assert rows(client, vendor="Foobar Labs")["total"] == 1
        assert rows(client, currency="AUD")["total"] == 2

    def test_invoice_date_range(self, db, client):
        self._corpus(db)
        body = rows(client, date_from="2026-08-01", date_to="2026-08-31")
        assert [row["case_id"] for row in body["rows"]] == ["b"]

    def test_amount_range(self, db, client):
        self._corpus(db)
        assert rows(client, min_total=1000)["total"] == 1
        assert rows(client, max_total=600)["total"] == 2

    def test_missing_po_and_missing_tax(self, db, client):
        self._corpus(db)
        assert [r["case_id"] for r in rows(client, po="missing")["rows"]] == [
            "b"
        ]
        assert [r["case_id"] for r in rows(client, tax="missing")["rows"]] == [
            "c"
        ]

    def test_filters_combine(self, db, client):
        """Multi-column filtering is an AND, not a widening OR."""
        self._corpus(db)
        assert rows(client, currency="AUD", min_total=1000)["total"] == 1
        assert rows(client, currency="INR", min_total=1000)["total"] == 0


class TestSortingAndPaging:
    def _three(self, db):
        with tenant_session(TENANT, db) as session:
            for name, total in (("A", 300), ("B", 100), ("C", 200)):
                invoice(
                    session,
                    case_id=name,
                    vendor_name=name,
                    invoice_number=name,
                    total_inc_tax=total,
                )

    def test_sort_ascending_and_descending(self, db, client):
        self._three(db)
        up = rows(client, sort="total_inc_tax", order="asc")["rows"]
        down = rows(client, sort="total_inc_tax", order="desc")["rows"]
        assert [r["total_inc_tax"] for r in up] == [100, 200, 300]
        assert [r["total_inc_tax"] for r in down] == [300, 200, 100]

    def test_an_unsortable_column_falls_back(self, db, client):
        """A bad sort key is a wrong order, not a 500."""
        self._three(db)
        assert rows(client, sort="quality_flags")["total"] == 3
        assert rows(client, sort="nonsense")["total"] == 3

    def test_paging_reports_the_full_total(self, db, client):
        self._three(db)
        page = rows(client, limit=2, sort="vendor_name", order="asc")
        assert len(page["rows"]) == 2
        assert page["total"] == 3

        second = rows(
            client, limit=2, offset=2, sort="vendor_name", order="asc"
        )
        assert [r["case_id"] for r in second["rows"]] == ["C"]

    def test_the_limit_is_capped(self, db, client):
        self._three(db)
        body = rows(client, limit=100000)
        assert body["total"] == 3


class TestQualityFlags:
    def test_missing_fields_are_flagged(self, db, client):
        with tenant_session(TENANT, db) as session:
            invoice(
                session,
                po_number=None,
                tax_amount=None,
                vendor_tax_id=None,
                due_date=None,
            )

        flags = rows(client)["rows"][0]["quality_flags"]
        assert {
            "missing_po",
            "missing_tax",
            "missing_tax_id",
            "missing_due_date",
        } <= set(flags)

    def test_a_failed_check_becomes_a_flag(self, db, client):
        with tenant_session(TENANT, db) as session:
            row = invoice(session)
            session.add(
                Finding(
                    tenant_id=TENANT,
                    invoice_id=row.id,
                    step="4.2",
                    passed=False,
                    severity="blocking",
                )
            )

        assert "lines_mismatch" in rows(client)["rows"][0]["quality_flags"]

    def test_a_passing_check_is_not_a_flag(self, db, client):
        with tenant_session(TENANT, db) as session:
            row = invoice(session)
            session.add(
                Finding(
                    tenant_id=TENANT,
                    invoice_id=row.id,
                    step="4.2",
                    passed=True,
                    severity="pass",
                )
            )

        assert "lines_mismatch" not in rows(client)["rows"][0]["quality_flags"]

    def test_a_repeated_invoice_number_is_flagged_on_both(self, db, client):
        with tenant_session(TENANT, db) as session:
            invoice(session, case_id="first", invoice_number="SAME")
            invoice(session, case_id="second", invoice_number="SAME")

        for row in rows(client)["rows"]:
            assert "duplicate_number" in row["quality_flags"]

    def test_an_edit_is_flagged_and_counted(self, db, client):
        with tenant_session(TENANT, db) as session:
            row = invoice(session)
            session.add(
                Correction(
                    tenant_id=TENANT,
                    invoice_id=row.id,
                    field_path="vendor_name",
                    old_value="Quikfix",
                    new_value="QuickFix HVAC",
                )
            )

        listed = rows(client)["rows"][0]
        assert listed["corrected_fields"] == 1
        assert "edited" in listed["quality_flags"]

    def test_low_confidence_is_flagged(self, db, client):
        with tenant_session(TENANT, db) as session:
            invoice(session, min_confidence=0.3)

        assert "low_confidence" in rows(client)["rows"][0]["quality_flags"]

    def test_a_document_with_no_text_layer_is_flagged(self, db, client):
        with tenant_session(TENANT, db) as session:
            invoice(
                session,
                fields={
                    "workflow": approved(),
                    "extraction": {
                        "invoice": {
                            "_evidence": {"_summary": {"has_text_layer": False}}
                        }
                    },
                },
            )

        assert "no_text_layer" in rows(client)["rows"][0]["quality_flags"]

    def test_filtering_by_a_flag_narrows_the_sheet(self, db, client):
        with tenant_session(TENANT, db) as session:
            invoice(session, case_id="clean")
            invoice(session, case_id="no-po", po_number=None)

        body = rows(client, quality="missing_po")
        assert [row["case_id"] for row in body["rows"]] == ["no-po"]

    def test_high_value_needs_a_threshold_to_mean_anything(self, db, client):
        """Unset, it matches nothing rather than everything."""
        with tenant_session(TENANT, db) as session:
            invoice(session, total_inc_tax=1_000_000)

        assert rows(client, quality="high_value")["total"] == 0
        assert "high_value" not in rows(client)["rows"][0]["quality_flags"]


class TestSummary:
    def test_totals_are_grouped_by_currency(self, db, client):
        """Adding rupees to dollars would produce a number meaning nothing."""
        with tenant_session(TENANT, db) as session:
            invoice(
                session,
                case_id="a",
                currency="AUD",
                total_inc_tax=100,
                tax_amount=10,
            )
            invoice(
                session,
                case_id="b",
                currency="AUD",
                total_inc_tax=200,
                tax_amount=20,
            )
            invoice(
                session,
                case_id="c",
                currency="INR",
                total_inc_tax=500,
                tax_amount=50,
            )

        body = client().get("/api/sheet/summary").json()
        assert body["total"] == 3
        assert body["vendors"] == 1
        by_currency = {c["currency"]: c for c in body["by_currency"]}
        assert by_currency["AUD"]["total"] == 300
        assert by_currency["AUD"]["tax"] == 30
        assert by_currency["INR"]["total"] == 500

    def test_the_summary_follows_the_same_filters_as_the_grid(self, db, client):
        with tenant_session(TENANT, db) as session:
            invoice(session, case_id="a", vendor_name="Kept")
            invoice(session, case_id="b", vendor_name="Filtered out")

        body = (
            client().get("/api/sheet/summary", params={"vendor": "Kept"}).json()
        )
        assert body["total"] == 1


class TestFacets:
    def test_it_lists_the_vendors_present(self, db, client):
        with tenant_session(TENANT, db) as session:
            invoice(session, case_id="a", vendor_name="Alpha")
            invoice(session, case_id="b", vendor_name="Beta")

        body = client().get("/api/sheet/facets").json()
        assert body["vendors"] == ["Alpha", "Beta"]
        assert body["currencies"] == ["AUD"]


class TestColumnCatalogue:
    def test_the_grid_is_told_what_it_may_sort_by(self, db, client):
        """The grid renders from this, so it cannot offer a control the query
        does not support."""
        with tenant_session(TENANT, db) as session:
            invoice(session)

        columns = {c["key"]: c for c in rows(client)["columns"]}
        assert columns["vendor_name"]["sortable"] is True
        assert columns["quality_flags"]["sortable"] is False
        assert columns["approved_at"]["source"] == "workflow"


class TestRoles:
    def test_every_role_can_read_the_sheet(self, db, client):
        with tenant_session(TENANT, db) as session:
            invoice(session)

        for role in ("viewer", "clerk", "approver", "admin"):
            assert client(role).get("/api/sheet/invoices").status_code == 200

    def test_a_viewer_cannot_save_a_view(self, db, client):
        response = client("viewer").post(
            "/api/sheet/views", json={"name": "Mine", "definition": {}}
        )
        assert response.status_code == 403

    def test_a_clerk_can_save_a_private_view(self, db, client):
        response = client("clerk").post(
            "/api/sheet/views", json={"name": "Mine", "definition": {}}
        )
        assert response.status_code == 200
        assert response.json()["shared"] is False

    def test_sharing_a_view_needs_approver(self, db, client):
        refused = client("clerk").post(
            "/api/sheet/views",
            json={"name": "Everyone", "definition": {}, "shared": True},
        )
        assert refused.status_code == 403

        allowed = client("approver").post(
            "/api/sheet/views",
            json={"name": "Everyone", "definition": {}, "shared": True},
        )
        assert allowed.status_code == 200


class TestSavedViews:
    def test_a_private_view_is_invisible_to_a_colleague(self, db, client):
        client("clerk").post(
            "/api/sheet/views", json={"name": "Mine", "definition": {}}
        )

        assert client("approver").get("/api/sheet/views").json()["views"] == []

    def test_a_shared_view_is_visible_to_everyone(self, db, client):
        client("approver").post(
            "/api/sheet/views",
            json={"name": "Ours", "definition": {}, "shared": True},
        )

        names = [
            v["name"]
            for v in client("viewer").get("/api/sheet/views").json()["views"]
        ]
        assert names == ["Ours"]

    def test_a_view_naming_an_unknown_column_is_refused(self, db, client):
        """Caught on write, so a saved view cannot break a page later."""
        response = client("clerk").post(
            "/api/sheet/views",
            json={
                "name": "Broken",
                "definition": {"columns": [{"key": "not_a_column"}]},
            },
        )
        assert response.status_code == 400
        assert "not_a_column" in response.text

    def test_a_view_sorting_by_an_unsortable_column_is_refused(
        self, db, client
    ):
        response = client("clerk").post(
            "/api/sheet/views",
            json={
                "name": "Broken",
                "definition": {"sort": {"key": "quality_flags"}},
            },
        )
        assert response.status_code == 400

    def test_a_view_round_trips(self, db, client):
        definition = {
            "filters": {"currency": "AUD"},
            "sort": {"key": "total_inc_tax", "order": "desc"},
            "columns": [{"key": "vendor_name", "width": 220, "frozen": True}],
        }
        created = (
            client("clerk")
            .post(
                "/api/sheet/views",
                json={"name": "High value", "definition": definition},
            )
            .json()
        )

        [read] = client("clerk").get("/api/sheet/views").json()["views"]
        assert read["id"] == created["id"]
        assert read["definition"] == definition

    def test_renaming_and_deleting(self, db, client):
        view = (
            client("clerk")
            .post("/api/sheet/views", json={"name": "Old", "definition": {}})
            .json()
        )

        renamed = client("clerk").patch(
            f"/api/sheet/views/{view['id']}", json={"name": "New"}
        )
        assert renamed.json()["name"] == "New"

        assert (
            client("clerk").delete(f"/api/sheet/views/{view['id']}").status_code
            == 200
        )
        assert client("clerk").get("/api/sheet/views").json()["views"] == []

    def test_someone_elses_view_cannot_be_edited(self, db, client):
        view = (
            client("clerk")
            .post("/api/sheet/views", json={"name": "Mine", "definition": {}})
            .json()
        )

        response = client("approver").patch(
            f"/api/sheet/views/{view['id']}", json={"name": "Yours"}
        )
        assert response.status_code == 403

    def test_an_admin_may_delete_anyones_view(self, db, client):
        view = (
            client("clerk")
            .post("/api/sheet/views", json={"name": "Mine", "definition": {}})
            .json()
        )

        assert (
            client("admin").delete(f"/api/sheet/views/{view['id']}").status_code
            == 200
        )


class TestWithoutADatabase:
    def test_the_sheet_says_it_needs_one(self, tmp_path, monkeypatch):
        """The filesystem store answers "give me this case" and nothing else.
        Saying so is better than an empty sheet that looks like no invoices."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        monkeypatch.setattr(auth, "_verifier", FakeVerifier())

        response = TestClient(
            app, headers={"Authorization": "Bearer tok-admin"}
        ).get("/api/sheet/invoices")

        assert response.status_code == 503
        assert "database" in response.text.lower()


class TestThePageCostsAFixedNumberOfQueries:
    """A query per row is invisible here and ruinous in production.

    SQLite answers in microseconds, so nothing in this suite would ever notice
    thirty extra statements. Against Cloud SQL each one is a round trip of
    about three hundred milliseconds -- which is how the document list and the
    review queue each came to take five and a half seconds before being
    batched. Counting the statements is the only thing that stays true.
    """

    def _count_selects(self, db, run):
        engine = get_engine(db)
        seen = []

        def record(conn, cursor, statement, params, context, executemany):
            if statement.strip().lower().startswith("select"):
                seen.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            run()
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len(seen)

    def _corpus(self, db, count):
        with tenant_session(TENANT, db) as session:
            for n in range(count):
                row = invoice(
                    session,
                    case_id=f"case-{n}",
                    invoice_number=f"INV-{n}",
                    vendor_name=f"Vendor {n}",
                )
                session.add(
                    Finding(
                        tenant_id=TENANT,
                        invoice_id=row.id,
                        step="4.2",
                        passed=False,
                        severity="blocking",
                    )
                )
                session.add(
                    Correction(
                        tenant_id=TENANT,
                        invoice_id=row.id,
                        field_path="vendor_name",
                        old_value="a",
                        new_value="b",
                    )
                )

    def test_the_cost_does_not_grow_with_the_page(self, db, client):
        sheet = SheetRepository(db)
        filters = {"status": "approved", "high_value_threshold": 0}

        self._corpus(db, 3)
        small = self._count_selects(
            db, lambda: sheet.page(TENANT, filters, "approved_at", "desc", 100)
        )

        self._corpus(db, 20)
        large = self._count_selects(
            db, lambda: sheet.page(TENANT, filters, "approved_at", "desc", 100)
        )

        assert small == large, (
            f"{small} queries for 3 rows, {large} for 23 -- the page is "
            "querying per row again"
        )
        # count + rows + findings + corrections + duplicates.
        assert large <= 6, f"a page of rows should not cost {large} queries"

    def test_an_empty_page_asks_nothing_extra(self, db, client):
        """No rows means no batch reads to do."""
        sheet = SheetRepository(db)
        count = self._count_selects(
            db,
            lambda: sheet.page(
                TENANT,
                {"status": "approved", "high_value_threshold": 0},
                "approved_at",
                "desc",
                100,
            ),
        )
        assert count <= 2


class TestAmountFiltersArriveAsText:
    """Found against production, not in a fixture.

    An amount reaches the API from a query string, so it is a string. Postgres
    refuses `numeric >= character varying` outright; SQLite casts it quietly.
    The whole page 500'd on the real database while every test passed on the
    test one -- so this asserts the coercion directly rather than relying on a
    dialect that cannot tell the difference.
    """

    def test_a_string_amount_becomes_a_number(self):
        assert _as_number("50000") == Decimal("50000")
        assert _as_number("1,234.56") == Decimal("1234.56")
        assert _as_number(" 42 ") == Decimal("42")
        assert _as_number(99.5) == Decimal("99.5")

    def test_nothing_unparseable_reaches_the_query(self):
        """A typo in a saved view narrows nothing rather than failing the page."""
        assert _as_number("") is None
        assert _as_number(None) is None
        assert _as_number("about fifty") is None

    def test_the_filter_still_selects_the_right_rows(self, db, client):
        with tenant_session(TENANT, db) as session:
            invoice(
                session,
                case_id="big",
                invoice_number="INV-BIG",
                total_inc_tax=5000,
            )
            invoice(
                session,
                case_id="small",
                invoice_number="INV-SMALL",
                total_inc_tax=100,
            )

        found = rows(client, min_total="1000")["rows"]

        assert [r["invoice_number"] for r in found] == ["INV-BIG"]
