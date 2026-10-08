"""The actions endpoint decides one step of a chain and finishes only on the
last. Without rules the endpoint behaves as before: one approver, done."""

import json
import shutil

import pytest
from fastapi.testclient import TestClient
from golden import GOLDEN_CASES

from invoice_processing.adapters import (
    FsCaseRepository,
    JsonDocumentRepository,
    JsonlAuditSink,
    JsonlCorrectionStore,
)
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.repositories import PgApprovalRepository
from invoice_processing.db.session import (
    create_all,
    get_engine,
    tenant_session,
)

TENANT = "tenant-acme"
CASE = "case_005"


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
    url = scratch_db("approvals.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(TENANT, url) as session:
        session.add(
            models.Tenant(id=TENANT, name="Acme", slug="acme", status="active")
        )
    repo = PgApprovalRepository(url)
    monkeypatch.setattr(service, "_approvals", repo)
    monkeypatch.setenv("DATABASE_URL", url)

    root = tmp_path / "agent_output"
    shutil.copytree(GOLDEN_CASES, root)
    for case in root.iterdir():
        (case / "workflow.json").unlink(missing_ok=True)
    monkeypatch.setattr(
        service, "_cases", FsCaseRepository(root, tenant_id=TENANT)
    )
    documents = JsonDocumentRepository(tmp_path / "documents.json")
    documents.create(
        TENANT, id=CASE, source="upload", source_ref="uploader@x.test"
    )
    monkeypatch.setattr(service, "_documents", documents)
    monkeypatch.setattr(
        service,
        "_corrections",
        JsonlCorrectionStore(tmp_path / "corrections.jsonl"),
    )
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(service, "_audit", JsonlAuditSink(audit))
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)

    def client(token):
        return TestClient(app, headers={"Authorization": f"Bearer {token}"})

    def events():
        return [json.loads(x) for x in audit.read_text().splitlines()]

    return {
        "client": client,
        "repo": repo,
        "events": events,
        "root": root,
    }


def act(env, token, case=CASE, **body):
    return env["client"](token).post(
        f"/api/invoices/{case}/actions", json={"action": "approve", **body}
    )


def two_tier(env):
    env["repo"].create_rule(
        TENANT,
        name="Big invoices",
        kind="amount",
        match={"min_amount": 0},
        steps=[
            {"kind": "user", "value": "a@x.test"},
            {"kind": "user", "value": "b@x.test"},
        ],
    )


def workflow(env):
    return json.loads((env["root"] / CASE / "workflow.json").read_text())


def test_no_rules_one_approver_finishes(env):
    r = act(env, "approver:anyone")
    assert r.status_code == 200
    assert r.json()["completed"] is True
    assert workflow(env)["decision"] == "approve"
    types = [e["event_type"] for e in env["events"]()]
    assert "invoice_approved" in types
    assert "approval_chain_completed" in types


def test_two_tier_chain_needs_both_in_order(env):
    two_tier(env)
    first = act(env, "approver:a")
    assert first.status_code == 200
    body = first.json()
    assert body["completed"] is False and body["step"] == 1
    assert body["next"]["assignee_value"] == "b@x.test"
    assert not (env["root"] / CASE / "workflow.json").exists()

    again = act(env, "approver:a")
    assert again.status_code == 403
    assert "earlier step" in again.json()["detail"]

    last = act(env, "approver:b")
    assert last.status_code == 200 and last.json()["completed"] is True
    wf = workflow(env)
    assert wf["decision"] == "approve" and wf["decided_by"] == "b@x.test"
    assert {"decided_at", "reason", "reason_category"} <= set(wf)


def test_uploader_cannot_approve(env):
    r = act(env, "approver:uploader")
    assert r.status_code == 403
    assert "uploaded" in r.json()["detail"]


def test_editor_alone_needs_a_second_approver(env):
    edit = env["client"]("approver:ed").patch(
        f"/api/invoices/{CASE}/fields",
        json={"edits": [{"field": "vendor_name", "value": "Fixed Ltd"}]},
    )
    assert edit.status_code == 200
    r = act(env, "approver:ed")
    assert r.status_code == 200 and r.json()["completed"] is False
    nxt = env["repo"].current(TENANT, CASE)
    assert (nxt["assignee_kind"], nxt["assignee_value"]) == (
        "role",
        "approver",
    )
    assert not (env["root"] / CASE / "workflow.json").exists()
    # The second approver must be someone else, and then it is done.
    assert act(env, "approver:ed").status_code == 403
    assert act(env, "approver:other").json()["completed"] is True


def test_only_an_admin_may_send_override_reason(env):
    r = act(env, "approver:uploader", override_reason="please")
    assert r.status_code == 403
    assert "Only an admin" in r.json()["detail"]
    assert env["repo"].chain(TENANT, CASE) == []


def test_admin_override_passes_a_refusal_and_is_audited(env):
    r = act(env, "admin:uploader", override_reason="CFO asked me to")
    assert r.status_code == 200 and r.json()["completed"] is True
    (event,) = [
        e for e in env["events"]() if e["event_type"] == "approval_overridden"
    ]
    assert event["payload"]["override_reason"] == "CFO asked me to"
    assert env["repo"].chain(TENANT, CASE)[0]["override_reason"]


def test_reject_cancels_the_rest_and_writes_reject(env):
    two_tier(env)
    r = act(env, "approver:a", action="reject", reason="Wrong vendor")
    assert r.status_code == 200
    assert r.json()["completed"] is True
    assert workflow(env)["decision"] == "reject"
    assert [s["status"] for s in env["repo"].chain(TENANT, CASE)] == [
        "rejected",
        "cancelled",
    ]


def test_a_decided_invoice_is_409(env):
    assert act(env, "approver:a").status_code == 200
    again = act(env, "approver:b")
    assert again.status_code == 409
    assert "already decided" in again.json()["detail"]
