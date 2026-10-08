"""Dual control over the API. The repository refuses self-approval; these
tests pin the HTTP contract the UI relies on and that masking holds at the
boundary."""

import json
import shutil

import pytest
from fastapi.testclient import TestClient
from golden import GOLDEN_CASES

from invoice_processing import master_files, paths, vendor_master
from invoice_processing.adapters import FsCaseRepository, JsonlAuditSink
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.repositories import (
    PgVendorRepository,
    entity_type_for,
)
from invoice_processing.db.session import (
    create_all,
    get_engine,
    tenant_session,
)
from invoice_processing.ports import RunContext

TENANT = "tenant-acme"
ACME = {
    "name": "Cloudhaven Infra Services Pvt Ltd",
    "tax_id": "29ZOICE4828P1ZD",
    "kind": "service",
}
BANK = {
    "bank_account_name": ACME["name"],
    "bank_account_number": "901031925572",
    "bank_code": "KCBL0230056",
}


class FakeVerifier:
    def verify(self, token):
        role, _, who = token.partition(":")
        if role not in ("viewer", "clerk", "approver", "admin"):
            raise auth.AuthError("Invalid token")
        return {
            "sub": f"u-{token}",
            "email": f"{who or role}@x.test",
            "email_verified": True,
            "tenant_id": TENANT,
            "role": role,
        }


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("vendors.db")
    # CI reruns these on a shared Postgres, so start from an empty schema.
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(TENANT, url) as session:
        session.add(
            models.Tenant(id=TENANT, name="Acme", slug="acme", status="active")
        )
    repo = PgVendorRepository(url)
    monkeypatch.setattr(service, "_vendors", repo)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(
        service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)

    def client(token="admin"):
        return TestClient(app, headers={"Authorization": f"Bearer {token}"})

    client("admin").put("/api/vendors", json={"vendors": [ACME]})
    vendor_id = client("clerk").get("/api/vendors").json()["vendors"][0]["id"]
    return {
        "client": client,
        "repo": repo,
        "vendor_id": vendor_id,
        "tmp": tmp_path,
    }


def propose(env, token="clerk:alice", **extra):
    return env["client"](token).post(
        f"/api/vendors/{env['vendor_id']}/bank-changes",
        json={**BANK, "reason": "New account on letterhead", **extra},
    )


class TestList:
    def test_list_masks_and_reports_status(self, env):
        body = env["client"]("clerk").get("/api/vendors").json()
        (v,) = body["vendors"]
        assert body["source"] == "database"
        assert (
            v["bank_status"] == "unverified"
            and v["bank_account_masked"] is None
        )

    def test_viewer_cannot_list(self, env):
        assert env["client"]("viewer").get("/api/vendors").status_code == 403

    def test_bulk_put_never_writes_bank_fields(self, env):
        env["client"]("admin").put(
            "/api/vendors",
            json={"vendors": [{**ACME, "bank_account_number": "000"}]},
        )
        assert (
            env["repo"].get(TENANT, env["vendor_id"])["bank_account_number"]
            is None
        )


class TestPropose:
    def test_clerk_proposes_and_sees_masked_values(self, env):
        response = propose(env)
        assert response.status_code == 201
        body = response.json()
        assert (
            body["status"] == "pending"
            and body["proposed_by"] == "alice@x.test"
        )
        assert body["proposed_account_number"] == "…5572"
        listed = env["client"]("clerk").get("/api/vendors").json()["vendors"][0]
        assert listed["bank_status"] == "pending"
        assert listed["pending_change_id"] == body["id"]

    def test_unknown_source_case_is_400(self, env):
        response = propose(env, source_case_id="nope")
        assert response.status_code == 400
        assert response.json()["detail"] == "Unknown invoice"

    def test_source_case_cannot_traverse_out_of_the_case_root(
        self, env, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            service,
            "_cases",
            FsCaseRepository(tmp_path / "cases", tenant_id=TENANT),
        )
        response = propose(env, source_case_id="../../etc")
        assert response.status_code == 400
        assert response.json()["detail"] == "Unknown invoice"

    def test_viewer_cannot_propose(self, env):
        assert propose(env, token="viewer").status_code == 403

    def test_reason_is_required(self, env):
        response = env["client"]("clerk").post(
            f"/api/vendors/{env['vendor_id']}/bank-changes",
            json={**BANK, "reason": ""},
        )
        assert response.status_code == 400

    def test_second_pending_is_409(self, env):
        propose(env)
        assert propose(env).status_code == 409

    def test_queue_lists_pending_newest_first(self, env):
        change = propose(env).json()
        body = (
            env["client"]("clerk")
            .get("/api/vendors/bank-changes?status=pending")
            .json()
        )
        assert [c["id"] for c in body["changes"]] == [change["id"]]
        assert body["changes"][0]["vendor_name"] == ACME["name"]
        assert body["changes"][0]["proposed_account_number"] == "…5572"


class TestLengths:
    def test_over_long_account_number_is_422(self, env):
        assert propose(env, bank_account_number="9" * 65).status_code == 422

    def test_over_long_reason_is_422(self, env):
        assert propose(env, reason="r" * 2001).status_code == 422

    def test_over_long_email_domain_is_422(self, env):
        response = env["client"]("admin").patch(
            f"/api/vendors/{env['vendor_id']}",
            json={"email_domain": "d" * 256},
        )
        assert response.status_code == 422


class TestApproverSeesFullNumbers:
    def test_approver_reads_the_full_number_and_clerk_the_mask(self, env):
        change = propose(env).json()
        assert change["proposed_account_number"] == "…5572"
        queue = "/api/vendors/bank-changes?status=pending"
        (as_approver,) = (
            env["client"]("approver:bob").get(queue).json()["changes"]
        )
        (as_clerk,) = env["client"]("clerk:carol").get(queue).json()["changes"]
        assert as_approver["proposed_account_number"] == "901031925572"
        assert as_clerk["proposed_account_number"] == "…5572"

    def test_decide_returns_full_number_and_audit_stays_masked(self, env):
        change = propose(env).json()
        response = env["client"]("approver:bob").post(
            f"/api/vendors/bank-changes/{change['id']}/decide",
            json={"action": "approve"},
        )
        assert response.json()["proposed_account_number"] == "901031925572"
        assert "901031925572" not in (env["tmp"] / "audit.jsonl").read_text()


class TestAuditEntity:
    def test_vendor_events_are_filed_under_the_vendor(self, env):
        change = propose(env, source_case_id=None).json()
        env["client"]("approver:bob").post(
            f"/api/vendors/bank-changes/{change['id']}/decide",
            json={"action": "approve"},
        )
        env["client"]("admin").patch(
            f"/api/vendors/{env['vendor_id']}", json={"email_domain": "x.test"}
        )
        events = [
            json.loads(line)
            for line in (env["tmp"] / "audit.jsonl").read_text().splitlines()
        ]
        wanted = {
            "vendor_bank_change_proposed",
            "vendor_bank_change_approved",
            "vendor_updated",
        }
        seen = [e for e in events if e["event_type"] in wanted]
        assert {e["event_type"] for e in seen} == wanted
        for e in seen:
            assert e["case_id"] == env["vendor_id"]
            assert entity_type_for(e["event_type"], e["case_id"]) == "vendor"


class TestDecide:
    def test_proposer_cannot_approve(self, env):
        change = propose(env, token="clerk:alice").json()
        # alice is also an approver in this scenario; the rule is about
        # identity, not role.
        response = env["client"]("approver:alice").post(
            f"/api/vendors/bank-changes/{change['id']}/decide",
            json={"action": "approve"},
        )
        assert response.status_code == 403
        assert "proposed" in response.json()["detail"]
        assert "cannot decide" in response.json()["detail"]

    def test_clerk_cannot_decide(self, env):
        change = propose(env).json()
        response = env["client"]("clerk:bob").post(
            f"/api/vendors/bank-changes/{change['id']}/decide",
            json={"action": "approve"},
        )
        assert response.status_code == 403

    def test_approve_verifies_the_vendor(self, env):
        change = propose(env).json()
        response = env["client"]("approver:bob").post(
            f"/api/vendors/bank-changes/{change['id']}/decide",
            json={"action": "approve", "note": "Called the number on file"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "approved"
        (v,) = env["client"]("clerk").get("/api/vendors").json()["vendors"]
        assert v["bank_status"] == "verified"
        assert v["bank_account_masked"] == "…5572"
        assert v["bank_code"] == "KCBL0230056"
        audit = (env["tmp"] / "audit.jsonl").read_text()
        assert "vendor_bank_change_approved" in audit
        assert "901031925572" not in audit

    def test_reject_needs_a_note_and_is_terminal(self, env):
        change = propose(env).json()
        url = f"/api/vendors/bank-changes/{change['id']}/decide"
        bob = env["client"]("approver:bob")
        assert bob.post(url, json={"action": "reject"}).status_code == 400
        assert (
            bob.post(
                url, json={"action": "reject", "note": "unverified"}
            ).status_code
            == 200
        )
        assert bob.post(url, json={"action": "approve"}).status_code == 409


class TestEmailDomain:
    def test_admin_patches_domain(self, env):
        response = env["client"]("admin").patch(
            f"/api/vendors/{env['vendor_id']}",
            json={"email_domain": "Cloudhaven.example"},
        )
        assert response.status_code == 200
        assert response.json()["email_domain"] == "cloudhaven.example"

    def test_approver_cannot_patch(self, env):
        response = env["client"]("approver").patch(
            f"/api/vendors/{env['vendor_id']}",
            json={"email_domain": "x.example"},
        )
        assert response.status_code == 403


class TestWithoutADatabase:
    @pytest.mark.parametrize(
        ("method", "url", "body"),
        [
            ("patch", "/api/vendors/x", {"email_domain": "x.example"}),
            ("post", "/api/vendors/x/bank-changes", {**BANK, "reason": "r"}),
            ("get", "/api/vendors/bank-changes", None),
            (
                "post",
                "/api/vendors/bank-changes/x/decide",
                {"action": "approve"},
            ),
            ("post", "/api/invoices/case_005/bank-change", {"reason": "r"}),
        ],
    )
    def test_bank_change_routes_say_so(self, monkeypatch, method, url, body):
        monkeypatch.setattr(service, "_vendors", None)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        monkeypatch.setattr(auth, "_verifier", FakeVerifier())
        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        client = TestClient(app, headers={"Authorization": "Bearer admin"})
        response = getattr(client, method)(
            url, **({"json": body} if body else {})
        )
        assert response.status_code == 503
        assert "database" in response.json()["detail"].lower()

    def test_file_list_tolerates_a_hand_edited_record(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(service, "_vendors", None)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        monkeypatch.setattr(paths, "VENDOR_MASTER_DIR", tmp_path / "vendors")
        monkeypatch.setattr(auth, "_verifier", FakeVerifier())
        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        master_files.write_records(
            paths.vendor_master_file(TENANT),
            vendor_master.RECORD_KEY,
            [{"name": "No Tax Id Ltd"}],
        )
        client = TestClient(app, headers={"Authorization": "Bearer clerk"})
        response = client.get("/api/vendors")
        assert response.status_code == 200
        assert response.json()["vendors"][0]["name"] == "No Tax Id Ltd"


class TestWorkbenchShortcut:
    @pytest.fixture
    def with_case(self, env, tmp_path, monkeypatch):
        cases_root = tmp_path / "agent_output"
        shutil.copytree(GOLDEN_CASES, cases_root)
        cases = FsCaseRepository(cases_root, tenant_id=TENANT)
        monkeypatch.setattr(service, "_cases", cases)
        case_id = "case_005"
        # Point the case at our vendor and give it bank details to propose.
        ctx = RunContext(tenant_id=TENANT, case_id=case_id)
        extraction = cases.get_artifacts(ctx)["extraction"]
        extraction["invoice"].update({"vendor_name": ACME["name"], **BANK})
        cases.put_artifact(ctx, "extraction", extraction)
        return case_id

    def test_proposes_from_the_invoice(self, env, with_case):
        response = env["client"]("clerk:alice").post(
            f"/api/invoices/{with_case}/bank-change",
            json={"reason": "Printed on invoice"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["source_case_id"] == with_case
        assert body["proposed_account_number"] == "…5572"

    def test_unknown_case_is_404(self, env):
        response = env["client"]("clerk").post(
            "/api/invoices/nope/bank-change", json={"reason": "r"}
        )
        assert response.status_code == 404

    def _edit_invoice(self, with_case, **fields):
        cases = service._cases
        ctx = RunContext(tenant_id=TENANT, case_id=with_case)
        extraction = cases.get_artifacts(ctx)["extraction"]
        extraction["invoice"].update(fields)
        cases.put_artifact(ctx, "extraction", extraction)

    def test_vendor_not_in_master_is_409(self, env, with_case):
        self._edit_invoice(with_case, vendor_name="Someone Else Ltd")
        response = env["client"]("clerk").post(
            f"/api/invoices/{with_case}/bank-change", json={"reason": "r"}
        )
        assert response.status_code == 409

    def test_invoice_without_bank_details_is_409(self, env, with_case):
        self._edit_invoice(with_case, bank_account_number=None, bank_iban=None)
        response = env["client"]("clerk").post(
            f"/api/invoices/{with_case}/bank-change", json={"reason": "r"}
        )
        assert response.status_code == 409

    def test_second_proposal_is_409(self, env, with_case):
        url = f"/api/invoices/{with_case}/bank-change"
        clerk = env["client"]("clerk")
        body = {"reason": "Printed on invoice"}
        assert clerk.post(url, json=body).status_code == 201
        assert clerk.post(url, json=body).status_code == 409

    @pytest.mark.parametrize("reason", ["", "   "])
    def test_empty_reason_is_400(self, env, with_case, reason):
        response = env["client"]("clerk").post(
            f"/api/invoices/{with_case}/bank-change", json={"reason": reason}
        )
        assert response.status_code == 400
        assert "needs a reason" in response.json()["detail"]

    def test_over_long_extracted_value_is_409(self, env, with_case):
        self._edit_invoice(with_case, bank_account_number="9" * 65)
        response = env["client"]("clerk").post(
            f"/api/invoices/{with_case}/bank-change", json={"reason": "r"}
        )
        assert response.status_code == 409
        assert "too long to propose" in response.json()["detail"]
