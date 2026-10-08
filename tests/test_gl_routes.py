"""GL rules are tenant policy an admin writes; a line's coding is a person's
call, closed once the invoice is decided, and every change is on the trail."""

import json
import shutil
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from golden import GOLDEN_CASES

from invoice_processing import approval_flow
from invoice_processing.adapters import (
    FsCaseRepository,
    JsonDocumentRepository,
    JsonlAuditSink,
    JsonlCorrectionStore,
)
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.repositories import (
    PgApprovalRepository,
    PgGlRuleRepository,
    entity_type_for,
)
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext

TENANT = "tenant-acme"
CASE = "case_005"  # QuickFix HVAC Repairs, two lines
VENDOR = "QuickFix HVAC Repairs"


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


def _files(tmp_path, monkeypatch):
    root = tmp_path / "agent_output"
    shutil.copytree(GOLDEN_CASES, root)
    for case in root.iterdir():
        (case / "workflow.json").unlink(missing_ok=True)
    monkeypatch.setattr(
        service, "_cases", FsCaseRepository(root, tenant_id=TENANT)
    )
    monkeypatch.setattr(
        service, "_documents", JsonDocumentRepository(tmp_path / "docs.json")
    )
    monkeypatch.setattr(
        service, "_corrections", JsonlCorrectionStore(tmp_path / "c.jsonl")
    )
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(service, "_audit", JsonlAuditSink(audit))
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    return root, audit


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("gl.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(TENANT, url) as session:
        session.add(
            models.Tenant(id=TENANT, name="Acme", slug="acme", status="active")
        )
        session.add(
            models.User(tenant_id=TENANT, email="a@x.test", role="approver")
        )
    repo = PgGlRuleRepository(url)
    approvals = PgApprovalRepository(url)
    monkeypatch.setattr(service, "_gl_rules", repo)
    monkeypatch.setattr(service, "_approvals", approvals)
    monkeypatch.setenv("DATABASE_URL", url)
    root, audit = _files(tmp_path, monkeypatch)

    def client(token):
        return TestClient(app, headers={"Authorization": f"Bearer {token}"})

    def events():
        return [json.loads(x) for x in audit.read_text().splitlines()]

    return {
        "client": client,
        "repo": repo,
        "approvals": approvals,
        "events": events,
        "root": root,
    }


def rule(env, **body):
    return env["client"]("admin").post(
        "/api/gl-rules", json={"gl_account": "6100", **body}
    )


def code(env, token="clerk", line=1, **body):
    return env["client"](token).patch(
        f"/api/invoices/{CASE}/lines/{line}/gl", json=body
    )


# -- rules ----------------------------------------------------------------------


def test_admin_creates_and_anyone_from_clerk_reads(env):
    r = rule(env, vendor_name=" Acme Parts ", keyword=None)
    assert r.status_code == 201
    body = r.json()
    assert body["vendor_name"] == "Acme Parts"
    assert body["created_by"] == "admin@x.test"
    listed = env["client"]("clerk").get("/api/gl-rules").json()
    assert [x["id"] for x in listed["rules"]] == [body["id"]]
    assert env["client"]("viewer").get("/api/gl-rules").status_code == 403
    assert (
        env["client"]("clerk")
        .post("/api/gl-rules", json={"gl_account": "1", "keyword": "x"})
        .status_code
        == 403
    )
    created = [
        e for e in env["events"]() if e["event_type"] == "gl_rule_created"
    ]
    assert created and created[0]["payload"]["id"] == body["id"]


@pytest.mark.parametrize(
    "body",
    [
        {"gl_account": "  ", "keyword": "freight"},
        {"gl_account": "6100", "keyword": " ", "vendor_name": None},
        {"gl_account": "6100"},
    ],
)
def test_a_rule_that_could_never_code_is_refused(env, body):
    r = env["client"]("admin").post("/api/gl-rules", json=body)
    assert r.status_code == 400
    assert env["repo"].list_rules(TENANT) == []


def test_oversized_strings_are_refused_before_storage(env):
    assert rule(env, keyword="x" * 201).status_code == 422
    assert rule(env, vendor_name="x" * 256).status_code == 422
    assert rule(env, keyword="k", gl_account="9" * 65).status_code == 422


def test_patch_validates_the_resulting_rule(env):
    rid = rule(env, keyword="freight").json()["id"]
    admin = env["client"]("admin")
    r = admin.patch(f"/api/gl-rules/{rid}", json={"gl_account": " 6200 "})
    assert r.status_code == 200 and r.json()["gl_account"] == "6200"
    assert r.json()["keyword"] == "freight"
    # Clearing the only match would leave a rule that matches nothing.
    r = admin.patch(f"/api/gl-rules/{rid}", json={"keyword": None})
    assert r.status_code == 400
    r = admin.patch(f"/api/gl-rules/{rid}", json={"enabled": False})
    assert r.json()["enabled"] is False
    assert admin.patch("/api/gl-rules/nope", json={}).status_code == 404
    assert (
        env["client"]("clerk")
        .patch(f"/api/gl-rules/{rid}", json={"enabled": True})
        .status_code
        == 403
    )
    updated = [
        e for e in env["events"]() if e["event_type"] == "gl_rule_updated"
    ]
    assert updated[0]["before"]["gl_account"] == "6100"


def test_delete(env):
    rid = rule(env, keyword="freight").json()["id"]
    admin = env["client"]("admin")
    assert admin.delete(f"/api/gl-rules/{rid}").json() == {"deleted": True}
    assert admin.delete(f"/api/gl-rules/{rid}").status_code == 404
    assert env["repo"].list_rules(TENANT) == []
    assert "gl_rule_deleted" in [e["event_type"] for e in env["events"]()]


def test_without_a_database_the_ui_is_told_why(tmp_path, monkeypatch):
    _files(tmp_path, monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(service, "_database_configured", lambda: False)
    r = TestClient(app, headers={"Authorization": "Bearer clerk"}).get(
        "/api/gl-rules"
    )
    assert r.status_code == 503
    assert "needs a database" in r.json()["detail"]


# -- coding a line ----------------------------------------------------------------


def test_a_person_codes_a_line(env):
    r = code(env, gl_account=" 6100 ")
    assert r.status_code == 200
    first = r.json()["lines"][0]
    assert first["line_number"] == 1
    assert first["gl_account"] == "6100"
    assert first["source"] == "manual"
    assert first["coded_by"] == "clerk@x.test"
    assert first["coded_at"]

    detail = env["client"]("viewer").get(f"/api/invoices/{CASE}").json()
    assert detail["gl_coding"]["lines"][0]["gl_account"] == "6100"
    assert [x["line_number"] for x in detail["lines"]] == [1, 2]
    assert detail["lines"][0]["gl_account"] == "6100"

    (event,) = [
        e for e in env["events"]() if e["event_type"] == "gl_line_coded"
    ]
    assert event["payload"]["gl_account"] == "6100"
    assert event["before"]["gl_account"] is None
    assert event["case_id"] == CASE


def test_clearing_a_line(env):
    code(env, gl_account="6100")
    r = code(env, gl_account=None)
    assert r.json()["lines"][0]["gl_account"] is None
    assert r.json()["lines"][0]["source"] == "manual"


def test_unknown_line_and_case(env):
    assert code(env, line=9, gl_account="6100").status_code == 404
    r = env["client"]("clerk").patch(
        "/api/invoices/nope/lines/1/gl", json={"gl_account": "6100"}
    )
    assert r.status_code == 404


def test_viewer_cannot_code_and_long_accounts_are_refused(env):
    assert code(env, "viewer", gl_account="6100").status_code == 403
    assert code(env, gl_account="9" * 65).status_code == 422


def test_a_decided_invoice_is_closed(env):
    (env["root"] / CASE / "workflow.json").write_text(
        json.dumps({"decision": "approve"})
    )
    r = code(env, gl_account="6100")
    assert r.status_code == 409
    assert "already approve" in r.json()["detail"]
    assert not (env["root"] / CASE / "gl_coding.json").exists()


def test_remember_is_admin_only(env):
    r = code(env, gl_account="6100", remember=True)
    assert r.status_code == 403
    assert "admin" in r.json()["detail"]
    assert env["repo"].list_rules(TENANT) == []


def test_remember_creates_then_updates_one_vendor_rule(env):
    assert (
        code(env, "admin", gl_account="6100", remember=True).status_code == 200
    )
    (only,) = env["repo"].list_rules(TENANT)
    assert only["vendor_name"] == VENDOR and only["keyword"] is None
    assert only["gl_account"] == "6100"
    # Remembering again re-points the same rule: a second vendor-only rule
    # would lose the tie to the older one and never code anything.
    code(env, "admin", line=2, gl_account="6200", remember=True)
    (only,) = env["repo"].list_rules(TENANT)
    assert only["gl_account"] == "6200"
    types = [e["event_type"] for e in env["events"]()]
    assert types.count("gl_rule_created") == 1
    assert "gl_rule_updated" in types


def test_remember_needs_an_account(env):
    r = code(env, "admin", gl_account=None, remember=True)
    assert r.status_code == 400
    assert env["repo"].list_rules(TENANT) == []


def test_coding_reroutes_approvals(env):
    approvals = env["approvals"]
    approvals.create_rule(
        TENANT,
        name="Facilities",
        kind="gl",
        match={"gl_prefix": "61"},
        steps=[{"kind": "user", "value": "a@x.test"}],
    )
    # case_005 is touchless; only an amount tier brings it to a person.
    approvals.create_rule(
        TENANT,
        name="Everything",
        kind="amount",
        match={"min_amount": 0},
        steps=[{"kind": "role", "value": "approver"}],
    )
    ctx = RunContext(tenant_id=TENANT, case_id=CASE)
    approval_flow.route(
        ctx,
        service.get_cases().get_artifacts(ctx),
        repo=approvals,
        now=datetime.now(timezone.utc),
    )
    assert not any(
        s["assignee_value"] == "a@x.test" for s in approvals.chain(TENANT, CASE)
    )
    code(env, gl_account="6100")
    assert any(
        s["assignee_value"] == "a@x.test" for s in approvals.chain(TENANT, CASE)
    )
    assert "approval_rerouted" in [e["event_type"] for e in env["events"]()]


def test_coding_works_without_a_database(tmp_path, monkeypatch):
    """The rules need storage; a person's coding of a line does not."""
    root, _ = _files(tmp_path, monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(service, "_database_configured", lambda: False)
    r = TestClient(app, headers={"Authorization": "Bearer clerk"}).patch(
        f"/api/invoices/{CASE}/lines/2/gl", json={"gl_account": "6100"}
    )
    assert r.status_code == 200
    stored = json.loads((root / CASE / "gl_coding.json").read_text())
    assert stored["lines"][1]["gl_account"] == "6100"


def test_the_trail_names_what_each_event_is_about():
    assert entity_type_for("gl_rule_created") == "gl_rule"
    assert entity_type_for("gl_line_coded", CASE) == "invoice"
