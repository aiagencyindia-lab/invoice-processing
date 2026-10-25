"""When a reviewer overrules the pipeline, and what the system does with it.

The event worth learning from is a disagreement: an invoice the machine
rejected that a person approved (the rulebook is missing an exemption), or one
the machine accepted that a person rejected (a check is missing). Until this
existed both looked exactly like agreement in every store -- the human said
`approve`, the machine said `ACCEPT`, and nothing ever compared the two.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from fresh_schema import fresh_schema
from sqlalchemy import select

from invoice_processing.api import auth
from invoice_processing.api.service import app
from invoice_processing.db.models import Invoice, Tenant
from invoice_processing.db.repositories import PgCaseRepository
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext

TENANT = "tenant-acme"
CASE = "case_001"
CLAIMS = {
    "tok-ada": {
        "user_id": "u-ada",
        "email": "ada@acme.test",
        "email_verified": True,
        "tenant_id": TENANT,
        "role": "approver",
    }
}


class FakeVerifier:
    def verify(self, token):
        if token not in CLAIMS:
            raise auth.AuthError("Invalid token")
        return CLAIMS[token]


def pipeline_said(verdict: str) -> dict:
    """The artifacts a finished pipeline leaves behind."""
    return {
        "extraction": {
            "invoice": {
                "vendor_name": "QuickFix HVAC",
                "invoice_number": "INV-1",
                "invoice_total_inc_tax": "1100.00",
            }
        },
        "decision": {
            "final_decision": verdict,
            "decision_class": verdict,
            "rejection_template": "Invoice does not match work authorization"
            if verdict == "REJECT"
            else None,
        },
    }


@pytest.fixture
def db(scratch_db, monkeypatch):
    url = scratch_db("overrides.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
    fresh_schema(url)
    with tenant_session(TENANT, url) as session:
        session.add(Tenant(id=TENANT, slug="acme", name="Acme"))
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    return url


@pytest.fixture
def seed(db):
    """Put a processed case in front of a reviewer."""

    def make(verdict: str):
        cases = PgCaseRepository(db)
        ctx = RunContext(tenant_id=TENANT, case_id=CASE)
        artifacts = pipeline_said(verdict)
        for key, payload in artifacts.items():
            cases.put_artifact(ctx, key, payload)
        cases.project_case(ctx, artifacts)
        return TestClient(app, headers={"Authorization": "Bearer tok-ada"})

    return make


def column(db, name: str):
    with tenant_session(TENANT, db) as session:
        return session.scalar(
            select(getattr(Invoice, name)).where(
                Invoice.tenant_id == TENANT, Invoice.case_id == CASE
            )
        )


def decide(client, action: str, category="policy_exception", reason="because"):
    return client.post(
        f"/api/invoices/{CASE}/actions",
        json={
            "action": action,
            "reason": reason,
            "reason_category": category,
        },
    )


class TestTheDisagreementIsRecorded:
    def test_approving_a_machine_rejection_is_an_exemption(self, db, seed):
        """The case ALF exists for: the rulebook said no, the policy says yes."""
        client = seed("REJECT")

        response = decide(client, "approve")
        assert response.status_code == 200, response.text
        assert response.json()["agreement"] == "exemption"
        assert column(db, "decision_agreement") == "exemption"
        assert column(db, "human_decision") == "approve"

    def test_rejecting_a_machine_acceptance_is_too_permissive(self, db, seed):
        """The expensive direction: the machine would have paid this."""
        client = seed("ACCEPT")

        assert decide(client, "reject").json()["agreement"] == "too_permissive"
        assert column(db, "decision_agreement") == "too_permissive"

    def test_agreeing_is_recorded_as_agreement(self, db, seed):
        client = seed("REJECT")

        assert decide(client, "reject").json()["agreement"] == "agree"
        assert column(db, "decision_agreement") == "agree"

    def test_the_category_is_stored_on_the_row_and_in_the_artifact(
        self, db, seed
    ):
        client = seed("ACCEPT")
        decide(client, "reject", category="duplicate", reason="paid in March")

        assert column(db, "decision_category") == "duplicate"
        workflow = PgCaseRepository(db).get_artifacts(
            RunContext(tenant_id=TENANT, case_id=CASE)
        )["workflow"]
        assert workflow["reason_category"] == "duplicate"
        assert workflow["reason"] == "paid in March"

    def test_pending_payment_counts_as_a_machine_acceptance(self, db, seed):
        """An auto-accepted invoice says so only in the postprocessing status.

        `final_decision` is absent on that path, so if "Pending Payment" is not
        read as an acceptance the machine reads as undecided -- and every
        rejection of an auto-accepted invoice is filed as agreement instead of
        as the machine having been too permissive. The whole signal inverts,
        silently, for exactly the invoices that were never reviewed.
        """
        cases = PgCaseRepository(db)
        ctx = RunContext(tenant_id=TENANT, case_id=CASE)
        artifacts = {
            "extraction": {"invoice": {"vendor_name": "QuickFix HVAC"}},
            "postprocessing": {
                "Invoice Processing": {"Invoice Status": "Pending Payment"}
            },
        }
        for key, payload in artifacts.items():
            cases.put_artifact(ctx, key, payload)
        cases.project_case(ctx, artifacts)
        client = TestClient(app, headers={"Authorization": "Bearer tok-ada"})

        assert decide(client, "reject").json()["agreement"] == "too_permissive"


class TestTheCategoryIsConstrained:
    def test_an_unknown_category_is_refused(self, db, seed):
        client = seed("ACCEPT")

        response = decide(client, "reject", category="because-i-said-so")
        assert response.status_code == 400
        assert "because-i-said-so" in response.json()["detail"]

    def test_a_caller_that_sends_none_still_works(self, db, seed):
        """The endpoint predates the taxonomy; an old client must not break."""
        client = seed("ACCEPT")

        response = client.post(
            f"/api/invoices/{CASE}/actions",
            json={"action": "reject", "reason": "no purchase order"},
        )
        assert response.status_code == 200
        assert response.json()["reason_category"] == "other"


class TestReprocessingDoesNotForgetTheReviewer:
    def test_a_retry_keeps_the_human_decision(self, db, seed):
        """The worker reprojects from the pipeline's own output files, which
        never contain the workflow artifact. Nulling the columns from that
        would erase a decision every time a document was retried."""
        client = seed("REJECT")
        decide(client, "approve")
        assert column(db, "decision_agreement") == "exemption"

        cases = PgCaseRepository(db)
        ctx = RunContext(tenant_id=TENANT, case_id=CASE)
        cases.project_case(ctx, pipeline_said("REJECT"))

        assert column(db, "decision_agreement") == "exemption"
        assert column(db, "human_decision") == "approve"


class TestAudit:
    def test_the_trail_carries_the_category_and_the_disagreement(
        self, db, seed
    ):
        client = seed("REJECT")
        decide(client, "approve", category="policy_exception")

        events = client.get("/api/audit").json()["events"]
        approval = next(e for e in events if e["action"] == "invoice_approved")
        assert approval["payload"]["reason_category"] == "policy_exception"
        assert approval["payload"]["agreement"] == "exemption"


class TestTheSheetCanFindThem:
    """The report this phase exists to make possible: which of our checks do
    reviewers overrule, and how often."""

    def test_overruled_invoices_can_be_filtered(self, db, seed):
        client = seed("REJECT")
        decide(client, "approve")

        body = client.get(
            "/api/sheet/invoices",
            params={"status": "all", "quality": "overruled"},
        ).json()
        assert [row["case_id"] for row in body["rows"]] == [CASE]
        assert body["rows"][0]["decision_agreement"] == "exemption"
        assert "overruled" in body["rows"][0]["quality_flags"]

    def test_an_agreed_decision_is_not_flagged(self, db, seed):
        client = seed("REJECT")
        decide(client, "reject")

        body = client.get(
            "/api/sheet/invoices",
            params={"status": "all", "quality": "overruled"},
        ).json()
        assert body["rows"] == []
