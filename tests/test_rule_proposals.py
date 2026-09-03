"""From a rejection to a live rule, without a chat session.

The loop the product was sold on ran through an ADK agent on somebody's
laptop, and wrote rules to a file nothing read. This is the same loop with the
two ends connected: a reviewer overrules the pipeline and says it was policy,
that becomes a proposal, a rule is drafted against the tenant's own corpus,
and a person approves it -- at which point it is live.

The test that matters most here is the collateral gate. The safety loop
reports success even when the rule it wrote still matches invoices nobody
meant it to, so the approval screen is the only thing standing between a
well-meant exemption and a hole in a control.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from invoice_processing.api import auth, service
from invoice_processing.api import rules as rules_api
from invoice_processing.api.service import app
from invoice_processing.db.models import Base, Tenant
from invoice_processing.db.repositories import (
    PgCaseRepository,
    PgRuleRepository,
)
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext

TENANT = "tenant-acme"
CASE = "case_005"
CLAIMS = {
    f"tok-{who}": {
        "user_id": f"u-{who}",
        "email": f"{who}@acme.test",
        "tenant_id": TENANT,
        "role": role,
    }
    for who, role in (
        ("dana", "clerk"),
        ("ada", "approver"),
        ("root", "admin"),
    )
}


class FakeVerifier:
    def verify(self, token):
        if token not in CLAIMS:
            raise auth.AuthError("Invalid token")
        return CLAIMS[token]


DRAFTED_RULE = {
    "id": "ignored",
    "name": "WAF exemption for low-value repairs",
    "description": "Policy exempts repairs under $2,000.",
    "scope": "WAF_EXEMPTION",
    "priority": 50,
    "conditions": [
        {"field": "decision_phase4", "operator": "equals", "value": "REJECT"}
    ],
    "actions": [
        {
            "type": "set_field",
            "target": "Invoice Processing.Invoice Status",
            "value": "Pending Payment",
        }
    ],
}


@pytest.fixture
def db(scratch_db, monkeypatch):
    url = scratch_db("proposals.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
    engine = get_engine(url)
    Base.metadata.drop_all(engine)
    create_all(url)
    with tenant_session(TENANT, url) as session:
        session.add(Tenant(id=TENANT, slug="acme", name="Acme"))

    cases = PgCaseRepository(url)
    ctx = RunContext(tenant_id=TENANT, case_id=CASE)
    cases.put_artifact(ctx, "extraction", {"invoice": {"vendor_name": "QF"}})
    cases.put_artifact(ctx, "decision", {"final_decision": "REJECT"})

    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    return url


@pytest.fixture
def client(db):
    def make(who="root"):
        return TestClient(app, headers={"Authorization": f"Bearer tok-{who}"})

    return make


def overrule(client, category="policy_exception"):
    """A reviewer approves what the pipeline rejected."""
    return client("ada").post(
        f"/api/invoices/{CASE}/actions",
        json={
            "action": "approve",
            "reason": "Emergency repair under $2,000, policy exempts it",
            "reason_category": category,
        },
    )


def drafted(db, rule_id, *, collateral=False):
    """Skip the model: put a drafted rule on the proposal directly."""
    return PgRuleRepository(db).update_proposal(
        TENANT,
        rule_id,
        status="draft",
        name=DRAFTED_RULE["name"],
        conditions=DRAFTED_RULE["conditions"],
        actions=DRAFTED_RULE["actions"],
        metadata={
            "impact": {"target_matched": True, "collateral_matches": []},
            "has_collateral": collateral,
            "collateral_warning": "2 other cases also match"
            if collateral
            else None,
        },
    )


class TestARejectionBecomesAProposal:
    def test_a_policy_exception_proposes_a_rule(self, db, client):
        assert overrule(client).status_code == 200

        proposals = client().get("/api/rules/proposals").json()["proposals"]
        assert len(proposals) == 1
        assert proposals[0]["source_case_id"] == CASE
        assert proposals[0]["status"] == "pending_draft"
        assert proposals[0]["author"] == "ada@acme.test"
        assert "Emergency repair" in proposals[0]["note"]

    def test_another_reason_proposes_nothing(self, db, client):
        """ "We read it wrong" is a data problem. Teaching a rule from it is
        how a working check turns into a hole."""
        assert overrule(client, category="extraction_wrong").status_code == 200

        assert client().get("/api/rules/proposals").json()["proposals"] == []

    def test_agreeing_with_the_pipeline_proposes_nothing(self, db, client):
        response = client("ada").post(
            f"/api/invoices/{CASE}/actions",
            json={
                "action": "reject",
                "reason": "no work authorization",
                "reason_category": "policy_exception",
            },
        )
        assert response.status_code == 200

        assert client().get("/api/rules/proposals").json()["proposals"] == []

    def test_a_proposal_is_not_a_live_rule(self, db, client):
        """It has been suggested, not approved. Nothing may act on it."""
        overrule(client)

        assert client().get("/api/rules").json()["rules"] == []


class TestApproval:
    def test_approving_makes_the_rule_live(self, db, client):
        overrule(client)
        rule_id = (
            client()
            .get("/api/rules/proposals")
            .json()["proposals"][0]["rule_id"]
        )
        drafted(db, rule_id)

        response = client("root").post(
            f"/api/rules/proposals/{rule_id}/decision",
            json={"decision": "approve"},
        )
        assert response.status_code == 200, response.text

        live = client().get("/api/rules").json()["rules"]
        assert [r["id"] for r in live] == [rule_id]

    def test_rejecting_leaves_nothing_live(self, db, client):
        overrule(client)
        rule_id = (
            client()
            .get("/api/rules/proposals")
            .json()["proposals"][0]["rule_id"]
        )
        drafted(db, rule_id)

        client("root").post(
            f"/api/rules/proposals/{rule_id}/decision",
            json={"decision": "reject", "note": "too broad"},
        )

        assert client().get("/api/rules").json()["rules"] == []

    def test_an_undrafted_proposal_cannot_be_approved(self, db, client):
        """There is no rule yet to approve."""
        overrule(client)
        rule_id = (
            client()
            .get("/api/rules/proposals")
            .json()["proposals"][0]["rule_id"]
        )

        response = client("root").post(
            f"/api/rules/proposals/{rule_id}/decision",
            json={"decision": "approve"},
        )
        assert response.status_code == 409

    def test_approving_needs_admin(self, db, client):
        overrule(client)
        rule_id = (
            client()
            .get("/api/rules/proposals")
            .json()["proposals"][0]["rule_id"]
        )
        drafted(db, rule_id)

        response = client("ada").post(
            f"/api/rules/proposals/{rule_id}/decision",
            json={"decision": "approve"},
        )
        assert response.status_code == 403


class TestTheCollateralGate:
    """The single most important refusal in the feature.

    The safety loop reports success even when the rule it wrote still matches
    invoices nobody meant it to. Approving that blind is how an exemption for
    one supplier silently re-decides forty other invoices.
    """

    def test_a_rule_with_collateral_cannot_be_approved_silently(
        self, db, client
    ):
        overrule(client)
        rule_id = (
            client()
            .get("/api/rules/proposals")
            .json()["proposals"][0]["rule_id"]
        )
        drafted(db, rule_id, collateral=True)

        response = client("root").post(
            f"/api/rules/proposals/{rule_id}/decision",
            json={"decision": "approve"},
        )
        assert response.status_code == 409
        assert "also match" in response.json()["detail"]["message"]
        assert client().get("/api/rules").json()["rules"] == []

    def test_it_can_be_approved_deliberately(self, db, client):
        overrule(client)
        rule_id = (
            client()
            .get("/api/rules/proposals")
            .json()["proposals"][0]["rule_id"]
        )
        drafted(db, rule_id, collateral=True)

        response = client("root").post(
            f"/api/rules/proposals/{rule_id}/decision",
            json={"decision": "approve", "accept_collateral": True},
        )
        assert response.status_code == 200
        assert len(client().get("/api/rules").json()["rules"]) == 1

    def test_rejecting_one_never_needs_the_acknowledgement(self, db, client):
        overrule(client)
        rule_id = (
            client()
            .get("/api/rules/proposals")
            .json()["proposals"][0]["rule_id"]
        )
        drafted(db, rule_id, collateral=True)

        response = client("root").post(
            f"/api/rules/proposals/{rule_id}/decision",
            json={"decision": "reject"},
        )
        assert response.status_code == 200


class TestScopeExclusion:
    """Only one rule per scope fires. Approving a second into an occupied
    scope does not add a rule, it silently shadows one somebody else
    approved -- and the shadowed rule stops correcting anything."""

    def _approved_rule_in_scope(self, db, client):
        overrule(client)
        first = (
            client()
            .get("/api/rules/proposals")
            .json()["proposals"][0]["rule_id"]
        )
        drafted(db, first)
        PgRuleRepository(db).update_proposal(
            TENANT, first, scope="WAF_EXEMPTION"
        )
        client("root").post(
            f"/api/rules/proposals/{first}/decision",
            json={"decision": "approve"},
        )
        return first

    def test_a_second_rule_in_the_same_scope_is_refused(self, db, client):
        self._approved_rule_in_scope(db, client)

        second = PgRuleRepository(db).propose(
            TENANT, case_id=CASE, author="ada@acme.test", note="another"
        )["rule_id"]
        drafted(db, second)
        PgRuleRepository(db).update_proposal(
            TENANT, second, scope="WAF_EXEMPTION"
        )

        response = client("root").post(
            f"/api/rules/proposals/{second}/decision",
            json={"decision": "approve"},
        )
        assert response.status_code == 409
        assert "Only one rule per scope" in response.json()["detail"]["message"]

    def test_a_different_scope_is_fine(self, db, client):
        self._approved_rule_in_scope(db, client)

        second = PgRuleRepository(db).propose(
            TENANT, case_id=CASE, author="ada@acme.test", note="another"
        )["rule_id"]
        drafted(db, second)
        PgRuleRepository(db).update_proposal(
            TENANT, second, scope="SOMETHING_ELSE"
        )

        response = client("root").post(
            f"/api/rules/proposals/{second}/decision",
            json={"decision": "approve"},
        )
        assert response.status_code == 200


class TestTheKillSwitch:
    def test_disabling_stops_a_live_rule(self, db, client):
        overrule(client)
        rule_id = (
            client()
            .get("/api/rules/proposals")
            .json()["proposals"][0]["rule_id"]
        )
        drafted(db, rule_id)
        client("root").post(
            f"/api/rules/proposals/{rule_id}/decision",
            json={"decision": "approve"},
        )
        assert client().get("/api/rules").json()["rules"]

        assert (
            client("root").post(f"/api/rules/{rule_id}/disable").status_code
            == 200
        )
        assert client().get("/api/rules").json()["rules"] == []


class TestTheTrailNamesBothPeople:
    def test_self_approval_is_recorded_as_such(self, db, client, monkeypatch):
        """Allowed, because a pilot has one admin and a rule nobody can
        approve is a feature nobody has. Visible, because a tenant that later
        wants two people should be able to see whether it ever had them."""
        events = []

        class RecordingAudit:
            def emit(self, ctx, event, payload, before=None):
                events.append((event, payload))

        overrule(client)
        rule_id = (
            client()
            .get("/api/rules/proposals")
            .json()["proposals"][0]["rule_id"]
        )
        drafted(db, rule_id)
        monkeypatch.setattr(service, "_audit", RecordingAudit())

        client("ada").post(
            f"/api/rules/proposals/{rule_id}/decision",
            json={"decision": "approve"},
        )  # refused: approver, not admin
        client("root").post(
            f"/api/rules/proposals/{rule_id}/decision",
            json={"decision": "approve"},
        )

        approved = next(p for name, p in events if name == "rule_approved")
        assert approved["author"] == "ada@acme.test"
        assert approved["approver"] == "root@acme.test"
        assert approved["self_approved"] is False


class TestDrafting:
    def test_the_reviewers_words_reach_the_rule_writer(self):
        """The note is the prompt. Without it the writer is guessing at a
        policy from an invoice that looks like every other invoice."""
        feedback = rules_api._feedback(
            {"note": "Emergency repair under $2,000, policy exempts it"}
        )

        assert "Emergency repair under $2,000" in feedback
        assert "policy exception" in feedback

    def test_a_failed_draft_says_why_and_can_be_retried(
        self, db, client, monkeypatch
    ):
        """Vertex hangs and models fail. A proposal that failed must say so
        and stay retryable, not vanish."""
        overrule(client)
        rule_id = (
            client()
            .get("/api/rules/proposals")
            .json()["proposals"][0]["rule_id"]
        )

        def explode(*args, **kwargs):
            raise RuntimeError("Vertex timed out")

        monkeypatch.setattr(
            "invoice_processing.core.safe_rule_orchestrator."
            "SafeRuleOrchestrator.discover",
            explode,
        )
        rules_api.draft(TENANT, rule_id)

        proposal = client().get(f"/api/rules/proposals/{rule_id}").json()
        assert proposal["status"] == "failed"
        assert "Vertex timed out" in proposal["error"]

        assert (
            client("root")
            .post(f"/api/rules/proposals/{rule_id}/draft")
            .status_code
            == 200
        )
