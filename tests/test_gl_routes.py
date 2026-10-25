"""GL rules are tenant policy an admin writes; a line's coding is a person's
call, closed once the invoice is decided, and every change is on the trail."""

import json
import shutil
import threading
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from fresh_schema import fresh_schema
from golden import GOLDEN_CASES

from invoice_processing import approval_flow, gl_coding
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
    PgCaseRepository,
    PgGlRuleRepository,
    entity_type_for,
)
from invoice_processing.db.session import tenant_session
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
    fresh_schema(url)
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
    assert r.json()["detail"] == (
        "This invoice was already decided; its GL coding is closed."
    )
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
        now=datetime.now(UTC),
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


def test_an_approver_who_recoded_a_line_cannot_approve_alone(env):
    """Coding a line is an edit: the coder may not be the only approver, even
    when their recode is what dropped the step that would have asked someone
    else."""
    approvals = env["approvals"]
    approvals.create_rule(
        TENANT,
        name="Facilities",
        kind="gl",
        match={"gl_prefix": "61"},
        steps=[{"kind": "user", "value": "a@x.test"}],
    )
    approvals.create_rule(
        TENANT,
        name="Everything",
        kind="amount",
        match={"min_amount": 0},
        steps=[{"kind": "role", "value": "approver"}],
    )
    code(env, gl_account="6100")
    assert any(
        s["assignee_value"] == "a@x.test" for s in approvals.chain(TENANT, CASE)
    )
    code(env, "approver:boss", gl_account="7000")
    assert not any(
        s["assignee_value"] == "a@x.test" and s["status"] == "pending"
        for s in approvals.chain(TENANT, CASE)
    )
    r = env["client"]("approver:boss").post(
        f"/api/invoices/{CASE}/actions", json={"action": "approve"}
    )
    assert r.status_code == 200
    assert r.json()["completed"] is False
    assert not (env["root"] / CASE / "workflow.json").exists()


def test_remember_refuses_an_oversized_vendor_before_writing(env):
    path = env["root"] / CASE / "02_extraction.json"
    data = json.loads(path.read_text())
    data["invoice"]["vendor_name"] = "V" * 256
    path.write_text(json.dumps(data))
    r = code(env, "admin", gl_account="6100", remember=True)
    assert r.status_code == 400
    assert not (env["root"] / CASE / "gl_coding.json").exists()
    assert env["repo"].list_rules(TENANT) == []


def test_a_malformed_stored_coding_does_not_break_coding_a_line(env):
    (env["root"] / CASE / "gl_coding.json").write_text(
        json.dumps(
            {
                "lines": [
                    {"line_number": "x", "gl_account": "1"},
                    {"line_number": [2], "gl_account": "2"},
                    {"line_number": True, "gl_account": "3"},
                    {"line_number": 2, "gl_account": "4"},
                ]
            }
        )
    )
    r = code(env, gl_account="6100")
    assert r.status_code == 200
    got = [(x["line_number"], x["gl_account"]) for x in r.json()["lines"]]
    assert got == [(1, "6100"), (2, "4")]


@pytest.mark.parametrize("field", ["line_items", "_evidence"])
def test_field_edits_cannot_reach_lines_or_internals(env, field):
    path = env["root"] / CASE / "02_extraction.json"
    before = path.read_text()
    r = env["client"]("clerk").patch(
        f"/api/invoices/{CASE}/fields",
        json={"edits": [{"field": field, "value": []}]},
    )
    assert r.status_code == 400
    assert path.read_text() == before


def _use_store(monkeypatch, store, scratch_db):
    """Serve the case from the database instead of files, when asked."""
    if store == "database":
        files = service.get_cases()
        cases = PgCaseRepository(scratch_db("gl.db"))
        ctx = RunContext(tenant_id=TENANT, case_id=CASE)
        for key, value in files.get_artifacts(ctx).items():
            cases.put_artifact(ctx, key, value)
        monkeypatch.setattr(service, "_cases", cases)


@pytest.mark.parametrize("store", ["files", "database"])
def test_two_people_coding_different_lines_at_once_both_land(
    env, monkeypatch, store, scratch_db
):
    """Each PATCH read the whole coding, changed its line, and wrote it all
    back, so the second writer erased the first writer's line."""
    _use_store(monkeypatch, store, scratch_db)

    # The first clerk's edit is mid-way (coding read, not yet written) when
    # the second clerk's edit for line 2 arrives.
    real, fired = gl_coding.valid_entries, threading.Event()

    def interleave(coding):
        if not fired.is_set():
            fired.set()
            other = threading.Thread(
                target=code,
                args=(env,),
                kwargs={"line": 2, "gl_account": "7200"},
            )
            other.start()
            # Fixed code holds a lock here, so the other edit waits for this
            # one rather than finishing inside it.
            other.join(timeout=1)
            interleave.other = other
        return real(coding)

    monkeypatch.setattr(gl_coding, "valid_entries", interleave)
    assert code(env, gl_account="6100").status_code == 200
    interleave.other.join(timeout=10)

    stored = service.get_cases().get_artifacts(
        RunContext(tenant_id=TENANT, case_id=CASE)
    )["gl_coding"]
    got = {x["line_number"]: x["gl_account"] for x in stored["lines"]}
    assert got == {1: "6100", 2: "7200"}


@pytest.mark.parametrize("store", ["files", "database"])
def test_two_people_editing_different_fields_at_once_both_land(
    env, monkeypatch, store, scratch_db
):
    """edit_fields had the same read-modify-write on the extraction."""
    _use_store(monkeypatch, store, scratch_db)

    def edit(field, value):
        return env["client"]("clerk").patch(
            f"/api/invoices/{CASE}/fields",
            json={"edits": [{"field": field, "value": value}]},
        )

    corrections = service.get_corrections()
    real, fired = corrections.record, threading.Event()

    def interleave(ctx, record):
        # Between the first edit's read and its write, a second one lands.
        if not fired.is_set():
            fired.set()
            assert edit("invoice_number", "INV-B").status_code == 200
        return real(ctx, record)

    monkeypatch.setattr(corrections, "record", interleave)
    assert edit("invoice_date", "2026-01-31").status_code == 200

    invoice = service.get_cases().get_artifacts(
        RunContext(tenant_id=TENANT, case_id=CASE)
    )["extraction"]["invoice"]
    assert invoice["invoice_date"] == "2026-01-31"
    assert invoice["invoice_number"] == "INV-B"
