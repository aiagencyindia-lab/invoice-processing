"""The actions endpoint decides one step of a chain and finishes only on the
last. Without rules the endpoint behaves as before: one approver, done."""

import json
import shutil
import threading
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from fresh_schema import fresh_schema
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
from invoice_processing.db import session as db_session
from invoice_processing.db.repositories import (
    PgApprovalRepository,
    PgCaseRepository,
    PgPurchaseOrderRepository,
    PgUserDirectory,
)
from invoice_processing.db.session import (
    tenant_session,
)
from invoice_processing.ports import RunContext

TENANT = "tenant-acme"
CASE = "case_005"


def held(*codes):
    """A gate that holds on these findings (ids = codes, for reading)."""
    detail = {
        "held": True,
        "message": f"Payment is held: {'; '.join(codes)}.",
        "holds": [
            {"finding_id": c, "type": c, "engine": "payment", "summary": c}
            for c in codes
        ],
        "waiting": [],
        "transaction_node": "n-1",
    }
    return lambda t, c, **k: detail


class FakeVerifier:
    def verify(self, token):
        role, _, who = token.partition(":")
        if role not in ("viewer", "clerk", "approver", "controller", "admin"):
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
    fresh_schema(url)
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

    def add_users(*emails, role="approver"):
        with tenant_session(TENANT, url) as session:
            for email in emails:
                session.add(
                    models.User(tenant_id=TENANT, email=email, role=role)
                )

    add_users("a@x.test", "b@x.test", "pa@x.test", "fin1@x.test")

    return {
        "client": client,
        "add_users": add_users,
        "repo": repo,
        "url": url,
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
    assert "invoice_approved" not in [e["event_type"] for e in env["events"]()]
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


def test_unassigned_approver_is_refused(env):
    two_tier(env)
    r = act(env, "approver:c")
    assert r.status_code == 403
    assert r.json()["detail"] == "Step 1 is assigned to user:a@x.test."
    assert env["repo"].current(TENANT, CASE)["status"] == "pending"


def test_admin_needs_an_override_reason_on_an_unassigned_step(env):
    two_tier(env)
    assert act(env, "admin:boss").status_code == 403
    r = act(env, "admin:boss", override_reason="Urgent, a is away")
    assert r.status_code == 200 and r.json()["completed"] is False
    (event,) = [
        e for e in env["events"]() if e["event_type"] == "approval_overridden"
    ]
    assert event["payload"]["override_reason"] == "Urgent, a is away"
    assert event["payload"]["why"] == "override"
    assert env["repo"].chain(TENANT, CASE)[0]["decided_by"] == "boss@x.test"


def test_plain_decision_audit_has_no_override_reason(env):
    act(env, "approver:anyone")
    (event,) = [
        e for e in env["events"]() if e["event_type"] == "approval_step_decided"
    ]
    assert "override_reason" not in event["payload"]


def test_editor_finishing_after_another_approver_needs_no_extra_step(env):
    two_tier(env)
    env["client"]("approver:b").patch(
        f"/api/invoices/{CASE}/fields",
        json={"edits": [{"field": "vendor_name", "value": "Fixed Ltd"}]},
    )
    assert act(env, "approver:a").json()["completed"] is False
    r = act(env, "approver:b")
    assert r.json()["completed"] is True
    assert len(env["repo"].chain(TENANT, CASE)) == 2


def test_delegate_decision_records_acting_for(env):
    two_tier(env)
    env["repo"].create_delegation(
        TENANT,
        delegator="a@x.test",
        delegate="pa@x.test",
        starts_on=date.today() - timedelta(days=1),
        ends_on=date.today() + timedelta(days=1),
        created_by="admin@x.test",
    )
    assert act(env, "approver:pa").status_code == 200
    assert env["repo"].chain(TENANT, CASE)[0]["acting_for"] == "a@x.test"


def test_appended_editor_step_has_a_clock(env):
    env["client"]("approver:ed").patch(
        f"/api/invoices/{CASE}/fields",
        json={"edits": [{"field": "vendor_name", "value": "Fixed Ltd"}]},
    )
    act(env, "approver:ed")
    assert env["repo"].current(TENANT, CASE)["due_at"] is not None


# -- routes ------------------------------------------------------------------

STEP_A = {"kind": "user", "value": "a@x.test"}


def make_group(env, name="Finance", members=("fin1@x.test",)):
    r = env["client"]("admin:boss").post(
        "/api/approvals/groups", json={"name": name, "members": list(members)}
    )
    assert r.status_code == 201, r.text
    return r.json()


def make_rule(env, **over):
    body = {
        "name": "Big",
        "kind": "amount",
        "match": {"min_amount": 0},
        "steps": [STEP_A],
        **over,
    }
    return env["client"]("admin:boss").post("/api/approvals/rules", json=body)


ALL_ROUTES = [
    ("get", "/api/approvals/mine", None),
    ("get", f"/api/invoices/{CASE}/approvals", None),
    ("get", "/api/approvals/groups", None),
    ("post", "/api/approvals/groups", {"name": "G", "members": []}),
    ("patch", "/api/approvals/groups/x", {"name": "G"}),
    ("delete", "/api/approvals/groups/x", None),
    ("get", "/api/approvals/rules", None),
    (
        "post",
        "/api/approvals/rules",
        {
            "name": "R",
            "kind": "amount",
            "match": {"min_amount": 0},
            "steps": [{"kind": "role", "value": "approver"}],
        },
    ),
    ("patch", "/api/approvals/rules/x", {"name": "R"}),
    ("delete", "/api/approvals/rules/x", None),
    ("get", "/api/approvals/delegations", None),
    (
        "post",
        "/api/approvals/delegations",
        {
            "delegator_email": "a@x.test",
            "delegate_email": "b@x.test",
            "starts_on": "2026-01-01",
            "ends_on": "2026-01-02",
        },
    ),
    ("delete", "/api/approvals/delegations/x", None),
]


@pytest.mark.parametrize(("method", "path", "body"), ALL_ROUTES)
def test_routes_say_needs_db_without_a_database(
    env, monkeypatch, method, path, body
):
    monkeypatch.setattr(service, "get_approvals", lambda: None)
    r = env["client"]("admin:boss").request(method, path, json=body)
    assert r.status_code == 503
    assert "DATABASE_URL" in r.json()["detail"]


@pytest.mark.parametrize(
    ("method", "path", "body", "lowest"),
    [
        ("get", "/api/approvals/mine", None, "viewer"),
        ("get", f"/api/invoices/{CASE}/approvals", None, "viewer"),
        ("get", "/api/approvals/groups", None, "clerk"),
        (
            "post",
            "/api/approvals/groups",
            {"name": "G", "members": []},
            "admin",
        ),
        ("patch", "/api/approvals/groups/x", {"name": "G"}, "admin"),
        ("delete", "/api/approvals/groups/x", None, "admin"),
        ("get", "/api/approvals/rules", None, "clerk"),
        (
            "post",
            "/api/approvals/rules",
            {"name": "R", "kind": "amount", "match": {}, "steps": []},
            "admin",
        ),
        ("patch", "/api/approvals/rules/x", {"name": "R"}, "admin"),
        ("delete", "/api/approvals/rules/x", None, "admin"),
        ("get", "/api/approvals/delegations", None, "approver"),
        (
            "post",
            "/api/approvals/delegations",
            {
                "delegator_email": "approver@x.test",
                "delegate_email": "b@x.test",
                "starts_on": "2026-01-01",
                "ends_on": "2026-01-02",
            },
            "approver",
        ),
        ("delete", "/api/approvals/delegations/x", None, "approver"),
    ],
)
def test_role_gate(env, method, path, body, lowest):
    order = ["viewer", "clerk", "approver", "admin"]
    below = order[: order.index(lowest)]
    for role in below:
        r = env["client"](role).request(method, path, json=body)
        assert r.status_code == 403, (role, r.text)
    r = env["client"](lowest).request(method, path, json=body)
    assert r.status_code != 403


def test_group_crud_and_member_validation(env):
    g = make_group(env, members=["FIN1@x.test"])
    assert g["members"] == ["fin1@x.test"]
    bad = env["client"]("admin:boss").post(
        "/api/approvals/groups",
        json={"name": "Z", "members": ["ghost@x.test"]},
    )
    assert bad.status_code == 400 and "ghost@x.test" in bad.json()["detail"]
    upd = env["client"]("admin:boss").patch(
        f"/api/approvals/groups/{g['id']}", json={"members": ["a@x.test"]}
    )
    assert upd.json()["members"] == ["a@x.test"]
    bad = env["client"]("admin:boss").patch(
        f"/api/approvals/groups/{g['id']}", json={"members": ["ghost@x.test"]}
    )
    assert bad.status_code == 400
    listed = env["client"]("clerk").get("/api/approvals/groups").json()
    assert [x["name"] for x in listed["groups"]] == ["Finance"]
    assert (
        env["client"]("admin:boss")
        .delete(f"/api/approvals/groups/{g['id']}")
        .status_code
        == 200
    )
    assert (
        env["client"]("admin:boss")
        .delete(f"/api/approvals/groups/{g['id']}")
        .status_code
        == 404
    )
    types = [e["event_type"] for e in env["events"]()]
    assert {
        "approval_group_created",
        "approval_group_updated",
        "approval_group_deleted",
    } <= set(types)


def test_a_group_a_rule_names_cannot_be_deleted(env):
    g = make_group(env)
    r = make_rule(env, steps=[{"kind": "group", "value": "Finance"}])
    assert r.status_code == 201, r.text
    refused = env["client"]("admin:boss").delete(
        f"/api/approvals/groups/{g['id']}"
    )
    assert refused.status_code == 409
    assert "Big" in refused.json()["detail"]


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"kind": "bogus"}, "kind"),
        ({"steps": []}, "step"),
        ({"match": {"min_amount": -1}}, "min_amount"),
        ({"match": {"min_amount": "lots"}}, "min_amount"),
        ({"match": {"min_amount": 5, "currency": "AU"}}, "currency"),
        ({"kind": "vendor", "match": {}}, "vendor_name"),
        (
            {"kind": "entity", "match": {"vendor_name": "x"}},
            "organization_name",
        ),
        (
            {"steps": [{"kind": "user", "value": "ghost@x.test"}]},
            "ghost@x.test",
        ),
        ({"steps": [{"kind": "group", "value": "Nope"}]}, "Nope"),
        ({"steps": [{"kind": "role", "value": "emperor"}]}, "emperor"),
        ({"steps": [{"kind": "team", "value": "x"}]}, "team"),
    ],
)
def test_rule_validation(env, over, needle):
    r = make_rule(env, **over)
    assert r.status_code == 400, r.text
    assert needle in r.json()["detail"]


def test_rule_crud(env):
    r = make_rule(
        env,
        kind="vendor",
        match={"vendor_name": "Acme"},
        steps=[{"kind": "role", "value": "approver"}],
    )
    assert r.status_code == 201
    rule = r.json()
    upd = env["client"]("admin:boss").patch(
        f"/api/approvals/rules/{rule['id']}", json={"enabled": False}
    )
    assert upd.json()["enabled"] is False
    bad = env["client"]("admin:boss").patch(
        f"/api/approvals/rules/{rule['id']}", json={"steps": []}
    )
    assert bad.status_code == 400
    assert (
        len(env["client"]("clerk").get("/api/approvals/rules").json()["rules"])
        == 1
    )
    assert (
        env["client"]("admin:boss")
        .delete(f"/api/approvals/rules/{rule['id']}")
        .status_code
        == 200
    )
    assert (
        env["client"]("admin:boss")
        .patch("/api/approvals/rules/nope", json={"name": "q"})
        .status_code
        == 404
    )
    types = [e["event_type"] for e in env["events"]()]
    assert {
        "approval_rule_created",
        "approval_rule_updated",
        "approval_rule_deleted",
    } <= set(types)


def delegation(env, token, **over):
    today = date.today()
    body = {
        "delegator_email": "a@x.test",
        "delegate_email": "pa@x.test",
        "starts_on": str(today - timedelta(days=1)),
        "ends_on": str(today + timedelta(days=1)),
        **over,
    }
    return env["client"](token).post("/api/approvals/delegations", json=body)


def test_delegation_rules(env):
    assert delegation(env, "approver:other").status_code == 403
    ok = delegation(env, "approver:a")
    assert ok.status_code == 201, ok.text
    assert (
        delegation(env, "admin:boss", delegator_email="b@x.test").status_code
        == 201
    )
    selfd = delegation(env, "admin:boss", delegate_email="a@x.test")
    assert selfd.status_code == 400
    rng = delegation(
        env,
        "admin:boss",
        starts_on=str(date.today()),
        ends_on=str(date.today() - timedelta(days=3)),
    )
    assert rng.status_code == 400
    ghost = delegation(env, "admin:boss", delegate_email="ghost@x.test")
    assert ghost.status_code == 400 and "ghost@x.test" in ghost.json()["detail"]
    # An approver sees only their own; an admin sees all.
    mine = env["client"]("approver:a").get("/api/approvals/delegations").json()
    assert [d["delegator_email"] for d in mine["delegations"]] == ["a@x.test"]
    allofthem = env["client"]("admin:boss").get("/api/approvals/delegations")
    assert allofthem.json()["count"] == 2
    did = ok.json()["id"]
    assert (
        env["client"]("approver:b")
        .delete(f"/api/approvals/delegations/{did}")
        .status_code
        == 403
    )
    assert (
        env["client"]("approver:a")
        .delete(f"/api/approvals/delegations/{did}")
        .status_code
        == 200
    )
    assert (
        env["client"]("admin:boss")
        .delete(f"/api/approvals/delegations/{did}")
        .status_code
        == 404
    )
    types = [e["event_type"] for e in env["events"]()]
    assert {"delegation_created", "delegation_deleted"} <= set(types)


def route_case(env):
    ctx = RunContext(tenant_id=TENANT, case_id=CASE)
    artifacts = service.get_cases().get_artifacts(ctx)
    approval_flow.route(ctx, artifacts, repo=env["repo"], now=datetime.now(UTC))


def test_mine_for_a_group_member(env):
    make_group(env)
    make_rule(env, steps=[{"kind": "group", "value": "Finance"}])
    route_case(env)
    r = env["client"]("viewer:fin1").get("/api/approvals/mine")
    assert r.status_code == 200
    (item,) = r.json()["items"]
    assert r.json()["count"] == 1
    assert item["case_id"] == CASE and item["why"] == "member of Finance"
    assert item["position"] == 1 and item["overdue"] is False
    assert item["vendor"] and item["total"] is not None
    assert item["assignee"] == "group:Finance"
    assert (
        env["client"]("viewer:nobody")
        .get("/api/approvals/mine")
        .json()["items"]
        == []
    )


def test_mine_offers_only_what_assess_allows(env, monkeypatch):
    # The queue must apply the very test decide() applies, so a refusal added
    # to assess() reaches the queue without a second copy to keep in step.
    make_group(env)
    make_rule(env, steps=[{"kind": "group", "value": "Finance"}])
    route_case(env)
    monkeypatch.setattr(
        approval_flow, "assess", lambda *a, **k: (None, "refused for test")
    )
    r = env["client"]("viewer:fin1").get("/api/approvals/mine")
    assert r.json()["items"] == []


def test_mine_marks_overdue(env):
    make_rule(env)
    route_case(env)
    with tenant_session(TENANT, env["url"]) as session:
        step = session.query(models.ApprovalStep).one()
        step.due_at = step.due_at - timedelta(days=60)
    (item,) = (
        env["client"]("viewer:a").get("/api/approvals/mine").json()["items"]
    )
    assert item["overdue"] is True


def test_mine_for_a_delegate_only_within_range(env):
    make_rule(env)
    route_case(env)
    assert delegation(env, "approver:a").status_code == 201
    (item,) = (
        env["client"]("viewer:pa").get("/api/approvals/mine").json()["items"]
    )
    assert item["why"].startswith("delegate for")
    delegation(
        env,
        "approver:a",
        delegate_email="b@x.test",
        starts_on=str(date.today() + timedelta(days=5)),
        ends_on=str(date.today() + timedelta(days=9)),
    )
    assert (
        env["client"]("viewer:b").get("/api/approvals/mine").json()["items"]
        == []
    )


def test_invoice_chain_endpoint(env):
    two_tier(env)
    route_case(env)
    r = env["client"]("viewer").get(f"/api/invoices/{CASE}/approvals")
    body = r.json()
    assert [s["assignee_value"] for s in body["steps"]] == [
        "a@x.test",
        "b@x.test",
    ]
    assert body["current"] == 1
    assert (
        env["client"]("viewer").get("/api/invoices/nope/approvals").status_code
        == 404
    )
    act(env, "approver:a")
    act(env, "approver:b")
    done = env["client"]("viewer").get(f"/api/invoices/{CASE}/approvals").json()
    assert done["current"] is None


def test_editing_the_total_reroutes_to_a_higher_tier(env):
    env["repo"].create_rule(
        TENANT,
        name="Over 1m",
        kind="amount",
        match={"min_amount": 1_000_000},
        steps=[{"kind": "user", "value": "b@x.test"}],
    )
    route_case(env)
    assert env["repo"].chain(TENANT, CASE) == []
    r = env["client"]("clerk").patch(
        f"/api/invoices/{CASE}/fields",
        json={
            "edits": [{"field": "invoice_total_inc_tax", "value": 2_000_000}]
        },
    )
    assert r.json()["approvals_rerouted"] is True
    assert [s["assignee_value"] for s in env["repo"].chain(TENANT, CASE)] == [
        "b@x.test"
    ]
    assert "approval_rerouted" in [e["event_type"] for e in env["events"]()]
    other = env["client"]("clerk").patch(
        f"/api/invoices/{CASE}/fields",
        json={"edits": [{"field": "invoice_number", "value": "X-1"}]},
    )
    assert other.json()["approvals_rerouted"] is False


def test_manager_patch(env):
    admin = env["client"]("admin:boss")
    users = admin.get("/api/admin/users").json()["users"]
    a = next(u for u in users if u["email"] == "a@x.test")
    assert a["manager_email"] is None
    r = admin.patch(
        f"/api/admin/users/{a['id']}", json={"manager_email": "B@x.test"}
    )
    assert r.status_code == 200 and r.json()["manager_email"] == "b@x.test"
    assert env["repo"].manager_of(TENANT, "a@x.test") == "b@x.test"
    (event,) = [e for e in env["events"]() if e["event_type"] == "user_updated"]
    assert event["before"]["manager_email"] is None
    cleared = admin.patch(
        f"/api/admin/users/{a['id']}", json={"manager_email": None}
    )
    assert cleared.json()["manager_email"] is None
    selfm = admin.patch(
        f"/api/admin/users/{a['id']}", json={"manager_email": "a@x.test"}
    )
    assert selfm.status_code == 400
    ghost = admin.patch(
        f"/api/admin/users/{a['id']}", json={"manager_email": "g@x.test"}
    )
    assert ghost.status_code == 400
    assert (
        admin.patch(
            "/api/admin/users/nope", json={"manager_email": None}
        ).status_code
        == 404
    )
    assert (
        env["client"]("approver")
        .patch(f"/api/admin/users/{a['id']}", json={"manager_email": None})
        .status_code
        == 403
    )


# -- fix round 1 -------------------------------------------------------------


def edit_total(env, value=2_000_000):
    return env["client"]("clerk").patch(
        f"/api/invoices/{CASE}/fields",
        json={"edits": [{"field": "invoice_total_inc_tax", "value": value}]},
    )


def test_a_decided_invoice_refuses_routing_edits(env):
    assert act(env, "approver:anyone").status_code == 200
    r = edit_total(env)
    assert r.status_code == 409
    assert "already decided" in r.json()["detail"]
    types = [e["event_type"] for e in env["events"]()]
    assert "fields_corrected" not in types
    assert "approval_rerouted" not in types
    assert not (env["root"].parent / "corrections.jsonl").exists()


def test_a_decided_invoice_still_takes_non_routing_edits(env):
    assert act(env, "approver:anyone").status_code == 200
    r = env["client"]("clerk").patch(
        f"/api/invoices/{CASE}/fields",
        json={"edits": [{"field": "payment_terms", "value": "Net 45"}]},
    )
    assert r.status_code == 200 and r.json()["count"] == 1


def test_a_failed_reroute_is_reported_and_audited(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db went away")

    monkeypatch.setattr(approval_flow, "route", boom)
    r = edit_total(env)
    assert r.status_code == 200
    assert r.json()["approvals_rerouted"] == "failed"
    assert r.json()["count"] == 1
    (ev,) = [
        e
        for e in env["events"]()
        if e["event_type"] == "approval_reroute_failed"
    ]
    assert "db went away" in ev["payload"]["error"]


def test_reroute_audit_follows_fields_corrected(env):
    make_rule(env, match={"min_amount": 1_000_000})
    edit_total(env)
    types = [e["event_type"] for e in env["events"]()]
    assert types.index("fields_corrected") < types.index("approval_rerouted")


def test_disabling_a_rule_does_not_revalidate_its_steps(env):
    rule = make_rule(env).json()
    with tenant_session(TENANT, env["url"]) as session:
        session.query(models.User).filter_by(email="a@x.test").delete()
    r = env["client"]("admin:boss").patch(
        f"/api/approvals/rules/{rule['id']}", json={"enabled": False}
    )
    assert r.status_code == 200 and r.json()["enabled"] is False


def test_empty_manager_patch_is_refused(env):
    users = env["client"]("admin:boss").get("/api/admin/users").json()["users"]
    r = env["client"]("admin:boss").patch(
        f"/api/admin/users/{users[0]['id']}", json={}
    )
    assert r.status_code == 400 and "Nothing to change" in r.json()["detail"]


def test_blank_names_and_infinite_amounts_are_refused(env):
    assert make_rule(env, name="  ").status_code == 400
    r = env["client"]("admin:boss").post(
        "/api/approvals/rules",
        content=(
            '{"name":"R","kind":"amount","match":{"min_amount":1e999},'
            '"steps":[{"kind":"role","value":"approver"}]}'
        ),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_groups_need_members_and_rules_need_staffed_groups(env):
    admin = env["client"]("admin:boss")
    assert (
        admin.post(
            "/api/approvals/groups", json={"name": "Empty", "members": []}
        ).status_code
        == 400
    )
    g = make_group(env)
    assert (
        admin.patch(
            f"/api/approvals/groups/{g['id']}", json={"members": []}
        ).status_code
        == 400
    )
    # A group emptied behind the rule's back (e.g. its members were removed).
    env["repo"].update_group(TENANT, g["id"], members=[])
    r = make_rule(env, steps=[{"kind": "group", "value": "Finance"}])
    assert r.status_code == 400 and "no members" in r.json()["detail"]


def test_an_identical_reroute_reports_no_change(env):
    make_rule(env, match={"min_amount": 0})
    route_case(env)
    env["client"]("clerk").patch(
        f"/api/invoices/{CASE}/fields",
        json={"edits": [{"field": "currency", "value": "AUD"}]},
    )
    # Same rules, same chain: pending rows are re-created with new ids.
    r = env["client"]("clerk").patch(
        f"/api/invoices/{CASE}/fields",
        json={"edits": [{"field": "currency", "value": "NZD"}]},
    )
    assert r.json()["approvals_rerouted"] is False
    assert "approval_rerouted" not in [e["event_type"] for e in env["events"]()]


# -- final review --------------------------------------------------------------


@pytest.mark.parametrize("value", ["12,500.00", "lots", None, "nan", True])
def test_a_non_numeric_total_is_refused(env, value):
    r = edit_total(env, value)
    assert r.status_code == 400, r.text
    assert "invoice_total_inc_tax" in r.json()["detail"]


def test_a_numeric_string_total_is_accepted(env):
    assert edit_total(env, "2500.50").status_code == 200


def test_unreadable_routing_table_is_503_not_unrouted(env, monkeypatch):
    def down(*a, **k):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(service, "_approvals", None)
    monkeypatch.setattr(db_session, "get_engine", down)
    # With the whole database down, sign-in itself now refuses (503) before
    # routing is reached. Keep this test about routing: sign-in on claims.
    monkeypatch.setattr(auth, "_sso_services", lambda: None)
    r = act(env, "approver:anyone")
    assert r.status_code == 503
    assert r.json()["detail"] == "Approval routing is unavailable."
    assert not (env["root"] / CASE / "workflow.json").exists()
    mine = env["client"]("approver:anyone").get("/api/approvals/mine")
    assert mine.status_code == 503
    # An edit keeps its correction and says the re-route failed.
    edit = edit_total(env)
    assert edit.status_code == 200
    assert edit.json()["approvals_rerouted"] == "failed"


def test_people_who_approve_must_hold_an_approving_role(env):
    env["add_users"]("cl@x.test", role="clerk")
    r = make_rule(env, steps=[{"kind": "user", "value": "cl@x.test"}])
    assert r.status_code == 400
    assert "cl@x.test" in r.json()["detail"]
    assert "clerk" in r.json()["detail"]
    g = env["client"]("admin:boss").post(
        "/api/approvals/groups", json={"name": "G", "members": ["cl@x.test"]}
    )
    assert g.status_code == 400 and "clerk" in g.json()["detail"]
    d = delegation(env, "approver:a", delegate_email="cl@x.test")
    assert d.status_code == 400 and "clerk" in d.json()["detail"]
    admin = env["client"]("admin:boss")
    users = admin.get("/api/admin/users").json()["users"]
    a = next(u for u in users if u["email"] == "a@x.test")
    m = admin.patch(
        f"/api/admin/users/{a['id']}", json={"manager_email": "cl@x.test"}
    )
    assert m.status_code == 400
    assert "cl@x.test" in m.json()["detail"] and "clerk" in m.json()["detail"]


def test_mine_leaves_out_what_the_caller_may_not_approve(env):
    make_rule(
        env,
        steps=[
            {"kind": "role", "value": "approver"},
            {"kind": "role", "value": "admin"},
        ],
    )
    route_case(env)
    # The uploader holds the role but may not approve their own invoice.
    mine = env["client"]("approver:uploader").get("/api/approvals/mine")
    assert mine.json()["items"] == []
    assert (
        env["client"]("approver:other")
        .get("/api/approvals/mine")
        .json()["count"]
        == 1
    )
    assert act(env, "approver:a").status_code == 200
    # Step 2 is the admins'; a, who decided step 1, is not offered it.
    assert (
        env["client"]("admin:a").get("/api/approvals/mine").json()["items"]
        == []
    )
    assert (
        env["client"]("admin:z").get("/api/approvals/mine").json()["count"] == 1
    )


# -- gap fixes -----------------------------------------------------------------


def lose_the_workflow_write(env):
    # The step is rejected but the crash took the workflow artifact with it.
    (env["root"] / CASE / "workflow.json").unlink()


def test_a_rejected_chain_without_a_workflow_stays_closed(env):
    two_tier(env)
    assert act(env, "approver:a").status_code == 200
    assert (
        act(env, "approver:b", action="reject", reason="no").status_code == 200
    )
    lose_the_workflow_write(env)
    before = env["repo"].chain(TENANT, CASE)
    r = edit_total(env)
    assert r.status_code == 200
    assert r.json()["approvals_rerouted"] is False
    assert "approval_rerouted" not in [e["event_type"] for e in env["events"]()]
    assert env["repo"].chain(TENANT, CASE) == before
    assert act(env, "approver:pa").status_code == 409


def escalate_a_to_b(env):
    """Route a one-step chain for a, let it go overdue, escalate to b."""
    make_rule(env)
    route_case(env)
    with tenant_session(TENANT, env["url"]) as session:
        session.query(models.User).filter_by(
            email="a@x.test"
        ).one().manager_email = "b@x.test"
    later = datetime.now(UTC) + timedelta(days=60)
    assert approval_flow.escalate_overdue(env["repo"], None, now=later) == 1
    (step,) = env["repo"].chain(TENANT, CASE)
    assert step["escalated_to"] == "b@x.test"
    return step


def test_escalation_does_not_decide_or_reassign(env):
    step = escalate_a_to_b(env)
    assert step["status"] == "pending"
    assert not (env["root"] / CASE / "workflow.json").exists()
    r = act(env, "approver:a")
    assert r.status_code == 200 and r.json()["completed"] is True
    (step,) = env["repo"].chain(TENANT, CASE)
    assert step["status"] == "approved" and step["decided_by"] == "a@x.test"


def test_the_escalation_target_may_approve_instead(env):
    escalate_a_to_b(env)
    (item,) = (
        env["client"]("approver:b").get("/api/approvals/mine").json()["items"]
    )
    assert item["why"] == "escalated from a@x.test"
    r = act(env, "approver:b")
    assert r.status_code == 200 and r.json()["completed"] is True
    (step,) = env["repo"].chain(TENANT, CASE)
    assert step["status"] == "approved" and step["decided_by"] == "b@x.test"


def chain_for(env, token):
    r = env["client"](token).get(f"/api/invoices/{CASE}/approvals")
    assert r.status_code == 200
    body = r.json()
    return body["can_decide"], body["reason"]


def test_chain_says_whether_the_caller_can_decide(env):
    two_tier(env)
    route_case(env)
    assert chain_for(env, "approver:a") == (True, None)
    assert chain_for(env, "approver:b") == (
        False,
        "Step 1 is assigned to user:a@x.test.",
    )
    # An admin may override, but the rule itself still says no.
    assert chain_for(env, "admin:boss") == (
        False,
        "Step 1 is assigned to user:a@x.test.",
    )
    assert act(env, "approver:a").status_code == 200
    can, reason = chain_for(env, "approver:a")
    assert can is False and "earlier step" in reason
    assert chain_for(env, "approver:b") == (True, None)


def test_chain_names_the_uploader_rule(env):
    make_rule(env, steps=[{"kind": "role", "value": "approver"}])
    route_case(env)
    can, reason = chain_for(env, "approver:uploader")
    assert can is False and "uploaded" in reason
    assert chain_for(env, "approver:other") == (True, None)


def test_chain_without_a_current_step_cannot_be_decided(env):
    assert chain_for(env, "approver:a") == (False, None)


def test_the_refusal_act_gives_is_the_reason_the_chain_gives(env):
    two_tier(env)
    route_case(env)
    _, reason = chain_for(env, "approver:b")
    r = act(env, "approver:b")
    assert r.status_code == 403 and r.json()["detail"] == reason


def test_a_seat_holder_without_the_approver_role_cannot_decide(env):
    # Named on the step, then demoted: act would 403 on the role, so the
    # chain must not offer the decision either. The users row decides the
    # role now, so the demotion is the row's, and the token agrees with it.
    two_tier(env)
    route_case(env)
    users = PgUserDirectory(env["url"])
    users.set_role(TENANT, users.find(TENANT, "a@x.test")["id"], "clerk")
    assert chain_for(env, "clerk:a") == (
        False,
        "'approver' or higher is required.",
    )
    assert act(env, "clerk:a").status_code == 403


def put_gl(accounts):
    ctx = RunContext(tenant_id=TENANT, case_id=CASE)
    lines = [
        {"line_number": i + 1, "gl_account": a, "source": "rule"}
        for i, a in enumerate(accounts)
    ]
    service.get_cases().put_artifact(ctx, "gl_coding", {"lines": lines})


def test_route_sends_a_gl_coded_invoice_to_its_gl_rule(env):
    put_gl(["6100-IT", None])
    gl = make_rule(env, kind="gl", match={"gl_prefix": "6100"}, steps=[STEP_A])
    assert gl.status_code == 201, gl.text
    make_rule(env, steps=[{"kind": "user", "value": "b@x.test"}])
    route_case(env)
    chain = env["repo"].chain(TENANT, CASE)
    assert [s["assignee_value"] for s in chain] == ["a@x.test", "b@x.test"]
    assert chain[0]["source_rule_id"] == gl.json()["id"]


def test_act_on_an_unrouted_invoice_routes_by_gl(env):
    # decide() builds the chain itself when routing yielded none.
    put_gl(["6100-IT"])
    make_rule(env, kind="gl", match={"gl_prefix": "61"}, steps=[STEP_A])
    assert act(env, "approver:a").status_code == 200
    chain = env["repo"].chain(TENANT, CASE)
    assert [(s["assignee_value"], s["status"]) for s in chain] == [
        ("a@x.test", "approved")
    ]


@pytest.mark.parametrize(
    "artifacts",
    [
        {},
        {"gl_coding": None},
        {"gl_coding": {"lines": "x"}},
        {"gl_coding": {"lines": [None, {"gl_account": 5}, {}]}},
    ],
)
def test_malformed_gl_coding_routes_with_no_accounts(artifacts):
    assert approval_flow.routing_invoice(artifacts)["_gl_accounts"] == []


def test_routing_invoice_lists_coded_accounts():
    invoice = approval_flow.routing_invoice(
        {
            "extraction": {"invoice": {"vendor_name": "V"}},
            "gl_coding": {
                "lines": [{"gl_account": "4000"}, {"gl_account": None}]
            },
        }
    )
    assert invoice == {"vendor_name": "V", "_gl_accounts": ["4000"]}


@pytest.mark.parametrize("prefix", [None, "", "  ", 6, "x" * 65])
def test_gl_rule_needs_a_short_prefix(env, prefix):
    r = make_rule(env, kind="gl", match={"gl_prefix": prefix})
    assert r.status_code == 400, r.text
    assert "gl_prefix" in r.json()["detail"]


def test_unknown_kind_lists_gl(env):
    r = make_rule(env, kind="bogus")
    assert "gl" in r.json()["detail"].split("Use ")[1]


# Approval-time re-check: two invoices cannot both consume the same received
# units. The match is re-run at the final step against the live book.
PO_ORDER = {
    "po_number": "PO-9001",
    "vendor_name": "QuickFix HVAC Repairs",
    "total_amount": 2000,
    "currency": "AUD",
    "lines": [
        {
            "line_number": 1,
            "item_code": "B-6205",
            "description": "Ball bearing 6205",
            "qty_ordered": 20,
            "unit_price": 100,
        }
    ],
}
LINE_23 = {"line_number": 1, "po_line_number": 1, "quantity": 6, "signals": []}


@pytest.fixture
def po_env(env, monkeypatch):
    orders = PgPurchaseOrderRepository(env["url"])
    monkeypatch.setattr(service, "_purchase_orders", orders)
    orders.upsert_orders(
        TENANT,
        [PO_ORDER],
        uploaded_by="a@x.test",
        now=datetime(2026, 10, 1, tzinfo=UTC),
    )
    orders.record_receipts(
        TENANT,
        [
            {
                "grn_number": "GRN-1",
                "po_number": "PO-9001",
                "received_on": "2026-10-01",
                "lines": [{"line_number": 1, "qty_received": 10}],
            }
        ],
        recorded_by="a@x.test",
    )
    case = env["root"] / CASE
    ext_path = case / "02_extraction.json"
    ext = json.loads(ext_path.read_text())
    ext["invoice"]["po_number"] = "PO-9001"
    ext["invoice"]["line_items"] = [
        {
            "item_code": "B-6205",
            "description": "Ball bearing 6205",
            "quantity": 6,
            "unit_price": 100,
        }
    ]
    ext_path.write_text(json.dumps(ext))
    p2_path = case / "04_phase2_validation.json"
    p2 = json.loads(p2_path.read_text())
    p2["validations"].append(
        {"step": "2.3", "passed": True, "signals": [], "lines": [LINE_23]}
    )
    p2_path.write_text(json.dumps(p2))
    return env


def consume_six(env, quantity=6):
    """Another invoice on the same PO line, approved since this was matched."""
    with tenant_session(TENANT, env["url"]) as s:
        s.add(
            models.Invoice(
                tenant_id=TENANT,
                case_id="case_other",
                vendor_name="QuickFix HVAC Repairs",
                invoice_number="INV-77",
                invoice_date="2026-10-01",
                currency="AUD",
                total_ex_tax=600,
                tax_amount=0,
                total_inc_tax=600,
                po_number="PO-9001",
                vendor_tax_id="x",
                due_date="2026-11-01",
                min_confidence=1,
                human_decision="approve",
                fields={
                    "extraction": {"invoice": {"po_number": "PO-9001"}},
                    "workflow": {
                        "decision": "approve",
                        "decided_at": "2026-10-02T00:00:00+00:00",
                    },
                    "phase2": {
                        "validations": [
                            {
                                "step": "2.3",
                                "lines": [{**LINE_23, "quantity": quantity}],
                            }
                        ]
                    },
                },
            )
        )


def test_approve_within_received_succeeds(po_env):
    r = act(po_env, "approver:anyone")
    assert r.status_code == 200 and r.json()["completed"] is True


def test_approve_after_the_units_are_consumed_is_409(po_env):
    consume_six(po_env)
    r = act(po_env, "approver:anyone")
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "Line 1 would bill 12 of 10 received; the purchase order changed "
        "since this was checked."
    )
    assert not (po_env["root"] / CASE / "workflow.json").exists()
    assert po_env["repo"].current(TENANT, CASE)["status"] == "pending"


def test_reject_is_not_rechecked(po_env):
    consume_six(po_env)
    r = act(po_env, "approver:anyone", action="reject", reason="Wrong qty")
    assert r.status_code == 200
    assert workflow(po_env)["decision"] == "reject"


def test_admin_override_approves_over_billed_and_is_audited(po_env):
    consume_six(po_env)
    r = act(po_env, "admin:boss", override_reason="Second delivery is here")
    assert r.status_code == 200 and r.json()["completed"] is True
    (event,) = [
        e
        for e in po_env["events"]()
        if e["event_type"] == "approval_overridden"
    ]
    assert event["payload"]["override_reason"] == "Second delivery is here"


def test_unreadable_book_at_approval_is_503(po_env, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(service._purchase_orders, "book", broken)
    r = act(po_env, "approver:anyone")
    assert r.status_code == 503
    assert r.json()["detail"] == approval_flow.BOOK_DOWN
    assert not (po_env["root"] / CASE / "workflow.json").exists()


def test_only_the_final_step_is_rechecked(po_env):
    two_tier(po_env)
    consume_six(po_env)
    first = act(po_env, "approver:a")
    assert first.status_code == 200 and first.json()["completed"] is False
    assert act(po_env, "approver:b").status_code == 409


def test_over_ordered_names_the_ordered_quantity(po_env):
    consume_six(po_env, quantity=15)
    r = act(po_env, "approver:anyone")
    assert r.status_code == 409
    assert r.json()["detail"].startswith("Line 1 would bill 21 of 20 ordered;")


def test_a_hold_already_accepted_is_not_raised_again(po_env):
    p2_path = po_env["root"] / CASE / "04_phase2_validation.json"
    p2 = json.loads(p2_path.read_text())
    p2["validations"][-1] = {
        "step": "2.3",
        "passed": False,
        "signals": ["short_received"],
        "lines": [{**LINE_23, "signals": ["short_received"]}],
    }
    p2_path.write_text(json.dumps(p2))
    consume_six(po_env)
    assert act(po_env, "approver:anyone").status_code == 200


def test_two_final_approvals_cannot_both_consume_the_same_units(
    po_env, monkeypatch
):
    """Each invoice fits the 10 received alone; together they bill 12."""
    cases = PgCaseRepository(po_env["url"])
    monkeypatch.setattr(service, "_cases", cases)
    fs = FsCaseRepository(po_env["root"], tenant_id=TENANT)
    base = fs.get_artifacts(RunContext(tenant_id=TENANT, case_id=CASE))
    for case in ("case_a", "case_b"):
        ctx = RunContext(tenant_id=TENANT, case_id=case)
        for key, value in base.items():
            if key != "workflow":
                cases.put_artifact(ctx, key, value)
        cases.project_case(ctx, cases.get_artifacts(ctx))

    # The first approval pauses inside its re-check. Without the lock the
    # second re-checks meanwhile, before the first is billed, and passes.
    real = approval_flow.over_billed
    a_inside, b_inside, go = (threading.Event() for _ in range(3))

    def paused(ctx, artifacts):
        out = real(ctx, artifacts)
        if ctx.case_id == "case_a":
            a_inside.set()
            go.wait(5)
        else:
            b_inside.set()
        return out

    monkeypatch.setattr(approval_flow, "over_billed", paused)
    results = {}

    def approve(case):
        results[case] = act(po_env, "approver:anyone", case=case)

    a = threading.Thread(target=approve, args=("case_a",))
    b = threading.Thread(target=approve, args=("case_b",))
    a.start()
    assert a_inside.wait(5)
    b.start()
    b_inside.wait(0.5)  # set at once without the lock; never while held
    go.set()
    a.join(10)
    b.join(10)
    assert results["case_a"].status_code == 200
    assert results["case_b"].status_code == 409
    assert "would bill 12 of 10 received" in results["case_b"].json()["detail"]


UNCHECKED_23 = {
    "step": "2.3",
    "passed": False,
    "signals": ["unchecked"],
    "evidence": "Purchase-order lines could not be checked: db down",
    "lines": [],
}


def mark_unchecked(env, case=CASE):
    p2_path = env["root"] / case / "04_phase2_validation.json"
    p2 = json.loads(p2_path.read_text())
    p2["validations"][-1] = dict(UNCHECKED_23)
    p2_path.write_text(json.dumps(p2))


def stored_23(cases, case):
    ctx = RunContext(tenant_id=TENANT, case_id=case)
    phase2 = cases.get_artifacts(ctx)["phase2"]
    return next(v for v in phase2["validations"] if v["step"] == "2.3")


def on_db_cases(env, monkeypatch, *names):
    """The fixture's case copied into the database store under `names`."""
    cases = PgCaseRepository(env["url"])
    monkeypatch.setattr(service, "_cases", cases)
    fs = FsCaseRepository(env["root"], tenant_id=TENANT)
    base = fs.get_artifacts(RunContext(tenant_id=TENANT, case_id=CASE))
    for case in names:
        ctx = RunContext(tenant_id=TENANT, case_id=case)
        for key, value in base.items():
            if key != "workflow":
                cases.put_artifact(ctx, key, value)
        cases.project_case(ctx, cases.get_artifacts(ctx))
    return cases


def test_billed_counts_an_approval_whose_projection_failed(po_env, monkeypatch):
    cases = on_db_cases(po_env, monkeypatch, "case_a", "case_b")

    real = cases.project_case

    def broken(ctx, artifacts):
        raise RuntimeError("projection down")

    monkeypatch.setattr(cases, "project_case", broken)
    assert act(po_env, "approver:anyone", case="case_a").status_code == 200
    monkeypatch.setattr(cases, "project_case", real)
    r = act(po_env, "approver:anyone", case="case_b")
    assert r.status_code == 409
    assert "would bill 12 of 10 received" in r.json()["detail"]


def test_approving_an_unchecked_hold_rechecks_the_live_book(po_env):
    mark_unchecked(po_env)
    consume_six(po_env)
    r = act(po_env, "approver:anyone")
    assert r.status_code == 409
    assert r.json()["detail"].startswith("Line 1 would bill 12 of 10 received")
    assert not (po_env["root"] / CASE / "workflow.json").exists()


def test_an_admin_may_override_an_unchecked_hold(po_env):
    mark_unchecked(po_env)
    consume_six(po_env)
    r = act(po_env, "admin:boss", override_reason="Rest is on the dock")
    assert r.status_code == 200
    assert "approval_overridden" in [
        e["event_type"] for e in po_env["events"]()
    ]


def test_an_unchecked_hold_with_the_book_down_is_503(po_env, monkeypatch):
    mark_unchecked(po_env)

    def broken(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(service._purchase_orders, "book", broken)
    r = act(po_env, "approver:anyone")
    assert r.status_code == 503
    assert r.json()["detail"] == approval_flow.BOOK_DOWN


def test_an_approved_unchecked_hold_is_stored_with_lines_and_billed(
    po_env, monkeypatch
):
    mark_unchecked(po_env)
    cases = on_db_cases(po_env, monkeypatch, "case_a", "case_b")
    assert act(po_env, "approver:anyone", case="case_a").status_code == 200
    entry = stored_23(cases, "case_a")
    assert entry["passed"] is True
    assert [
        (ln["po_line_number"], ln["quantity"]) for ln in entry["lines"]
    ] == [(1, 6.0)]
    # case_b was unchecked too; case_a's 6 now count, so 12 of 10 received.
    r = act(po_env, "approver:anyone", case="case_b")
    assert r.status_code == 409
    assert "would bill 12 of 10 received" in r.json()["detail"]


def test_an_unchecked_hold_refuses_on_any_live_hold_not_only_quantity(po_env):
    mark_unchecked(po_env)
    ext_path = po_env["root"] / CASE / "02_extraction.json"
    ext = json.loads(ext_path.read_text())
    ext["invoice"]["line_items"][0]["unit_price"] = 500
    ext_path.write_text(json.dumps(ext))
    r = act(po_env, "approver:anyone")
    assert r.status_code == 409
    assert r.json()["detail"].startswith(
        "Purchase-order lines were not checked when this invoice was processed"
    )


def test_final_approval_is_refused_while_payment_is_held(env, monkeypatch):
    from invoice_processing.trust import gate  # noqa: PLC0415

    monkeypatch.setattr(gate, "payment_hold_detail", held("PAY-X"))
    two_tier(env)
    first = act(env, "approver:a")
    assert first.status_code == 200 and first.json()["completed"] is False
    second = act(env, "approver:b")
    assert second.status_code == 409 and "PAY-X" in second.json()["detail"]
    assert not (env["root"] / CASE / "workflow.json").exists()


def test_admin_override_with_reason_passes_a_payment_hold(env, monkeypatch):
    from invoice_processing.trust import gate  # noqa: PLC0415

    monkeypatch.setattr(gate, "payment_hold_detail", held("PAY-X"))
    r = act(env, "admin:boss", override_reason="Called the vendor on file")
    assert r.status_code == 200 and r.json()["completed"] is True


def test_a_controller_decides_an_approver_step(env):
    """No rule: the fallback chain asks for the approver role, and a
    controller ranks with approver."""
    env["add_users"]("cora@x.test", role="controller")
    assert act(env, "controller:cora").status_code == 200, act
    assert workflow(env)["decision"] == "approve"


def test_a_controller_can_be_a_user_step_a_delegate_and_a_manager(env):
    env["add_users"]("cora@x.test", role="controller")
    assert (
        make_rule(
            env, steps=[{"kind": "user", "value": "cora@x.test"}]
        ).status_code
        == 201
    )
    r = delegation(env, "approver:a", delegate_email="cora@x.test")
    assert r.status_code == 201, r.text
    admin = env["client"]("admin:boss")
    users = admin.get("/api/admin/users").json()["users"]
    a = next(u for u in users if u["email"] == "a@x.test")
    r = admin.patch(
        f"/api/admin/users/{a['id']}", json={"manager_email": "cora@x.test"}
    )
    assert r.status_code == 200, r.text


CONTROLLER_STEP = {"kind": "role", "value": "controller"}


def set_user(env, email, **patch):
    admin = env["client"]("admin:boss")
    users = admin.get("/api/admin/users").json()["users"]
    user = next(u for u in users if u["email"] == email)
    return admin.patch(f"/api/admin/users/{user['id']}", json=patch)


def test_a_controller_role_step_needs_an_active_controller(env):
    r = make_rule(env, steps=[CONTROLLER_STEP])
    assert r.status_code == 400 and "controller" in r.text
    env["add_users"]("cora@x.test", role="controller")
    assert set_user(env, "cora@x.test", is_active=False).status_code == 200
    assert make_rule(env, steps=[CONTROLLER_STEP]).status_code == 400
    assert set_user(env, "cora@x.test", is_active=True).status_code == 200
    assert make_rule(env, steps=[CONTROLLER_STEP]).status_code == 201


def test_only_a_controller_decides_a_controller_step(env):
    """Not an admin, not an approver: the ladder does not reach this seat."""
    env["add_users"]("cora@x.test", role="controller")
    assert make_rule(env, steps=[CONTROLLER_STEP]).status_code == 201
    for token in ("admin:boss", "approver:a"):
        r = act(env, token)
        assert r.status_code in (403, 409), (token, r.text)
    r = act(env, "controller:cora")
    assert r.status_code == 200, r.text
    assert workflow(env)["decision"] == "approve"


def test_the_last_controller_cannot_be_removed_while_a_rule_needs_one(env):
    env["add_users"]("cora@x.test", role="controller")
    assert make_rule(env, steps=[CONTROLLER_STEP]).status_code == 201
    r = set_user(env, "cora@x.test", role="approver")
    assert r.status_code == 409 and "controller" in r.json()["detail"]
    assert set_user(env, "cora@x.test", is_active=False).status_code == 409
    env["add_users"]("carl@x.test", role="controller")
    assert set_user(env, "cora@x.test", role="approver").status_code == 200


def test_the_last_controller_cannot_be_removed_while_a_step_waits(env):
    """The rule is gone, but an invoice already waits on a controller."""
    env["add_users"]("cora@x.test", role="controller")
    rule = make_rule(env, steps=[CONTROLLER_STEP]).json()
    act(env, "approver:a")  # routes the invoice: the step now exists
    assert env["repo"].current(TENANT, CASE)["assignee_value"] == "controller"
    env["repo"].delete_rule(TENANT, rule["id"])
    assert set_user(env, "cora@x.test", role="approver").status_code == 409


def test_the_last_controller_can_go_when_nothing_needs_one(env):
    env["add_users"]("cora@x.test", role="controller")
    assert set_user(env, "cora@x.test", role="approver").status_code == 200
