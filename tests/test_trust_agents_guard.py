"""The guard on the six governed write paths (FR-28.2): engine off changes
nothing, an unregistered actor is untouched, and in enforcing a BLOCK is a
403 that writes nothing to the governed table."""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from fresh_schema import fresh_schema
from golden import GOLDEN_CASES
from sqlalchemy import select

from invoice_processing.adapters import (
    FsCaseRepository,
    JsonDocumentRepository,
    JsonlAuditSink,
    JsonlCorrectionStore,
)
from invoice_processing.api import auth, service
from invoice_processing.api.auth import Principal
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.repositories import (
    PgApprovalRepository,
    PgVendorRepository,
)
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.agents import guard
from invoice_processing.trust.agents.policy import Action
from invoice_processing.trust.agents.store import AgentStore
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.store import GraphStore

TENANT = "tenant-acme"
CASE = "case_005"  # QuickFix HVAC Repairs, 1069.32 AUD
ADMIN_A = RunContext(tenant_id=TENANT, run_id="", actor="admin-a@x.test")
ADMIN_B = RunContext(tenant_id=TENANT, run_id="", actor="admin-b@x.test")
INVOICE_ACTIONS = ["invoice.approve", "invoice.reject", "invoice.export"]
VENDOR = {"name": "QuickFix HVAC Repairs", "tax_id": "29AAAAA0000A1Z5"}
BANK = {"bank_account_number": "901031925572", "reason": "New letterhead"}


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


class Settings:
    """Tenant settings with just the agents mode."""

    def __init__(self):
        self.mode = "off"

    def get(self, tenant_id):
        return {"trust_engine_modes": {"agents": self.mode}}


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("agents_guard.db")
    fresh_schema(url)
    with tenant_session(TENANT, url) as s:
        s.add(
            models.Tenant(id=TENANT, name="Acme", slug="acme", status="active")
        )
        for email in ("a@x.test", "b@x.test"):
            s.add(models.User(tenant_id=TENANT, email=email, role="approver"))
    monkeypatch.setenv("DATABASE_URL", url)
    approvals = PgApprovalRepository(url)
    vendors = PgVendorRepository(url)
    graph = GraphStore(url)
    store = AgentStore(url, graph=graph)
    settings = Settings()
    root = tmp_path / "agent_output"
    shutil.copytree(GOLDEN_CASES, root)
    for case in root.iterdir():
        (case / "workflow.json").unlink(missing_ok=True)
    documents = JsonDocumentRepository(tmp_path / "documents.json")
    documents.create(
        TENANT, id=CASE, source="upload", source_ref="uploader@x.test"
    )
    audit = tmp_path / "audit.jsonl"
    for name, value in {
        "_approvals": approvals,
        "_vendors": vendors,
        "_graph_store": graph,
        "_agent_store": store,
        "_finding_store": FindingStore(url),
        "_tenant_settings": settings,
        "_cases": FsCaseRepository(root, tenant_id=TENANT),
        "_documents": documents,
        "_corrections": JsonlCorrectionStore(tmp_path / "corrections.jsonl"),
        "_audit": JsonlAuditSink(audit),
    }.items():
        monkeypatch.setattr(service, name, value)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)

    def client(token):
        return TestClient(app, headers={"Authorization": f"Bearer {token}"})

    def events():
        if not audit.exists():
            return []
        return [
            json.loads(x)["event_type"] for x in audit.read_text().splitlines()
        ]

    def rows(model):
        with tenant_session(TENANT, url) as s:
            return s.scalars(select(model)).all()

    return {
        "url": url,
        "client": client,
        "events": events,
        "rows": rows,
        "store": store,
        "settings": settings,
        "approvals": approvals,
        "vendors": vendors,
        "documents": documents,
        "root": root,
    }


def agent(
    env, token, *, owner="owner@x.test", actions=None, caps=None, mandate=True
):
    """Register the principal behind `token` as an agent, with an approved
    mandate unless `mandate=False`."""
    _, _, who = token.partition(":")
    a = env["store"].register(
        ADMIN_A,
        name=who,
        owner=owner,
        kind="user",
        principal_id=f"u-{token}",
        principal_email=f"{who}@x.test",
    )
    if mandate:
        m = env["store"].propose_mandate(
            ADMIN_A,
            a["id"],
            scope={
                "actions": actions or INVOICE_ACTIONS,
                "currency": "AUD",
                "counterparties": None,
                "caps": caps or {},
            },
            valid_from=datetime.now(UTC) - timedelta(days=1),
            valid_to=datetime.now(UTC) + timedelta(days=30),
        )
        env["store"].approve_mandate(ADMIN_B, m["id"], signature="sig")
    return a


def act(env, token, case=CASE, action="approve"):
    body = {"action": action}
    if action == "reject":
        body["reason"] = "Wrong amount"
    return env["client"](token).post(f"/api/invoices/{case}/actions", json=body)


def two_tier(env):
    env["approvals"].create_rule(
        TENANT,
        name="Big invoices",
        kind="amount",
        match={"min_amount": 0},
        steps=[
            {"kind": "user", "value": "a@x.test"},
            {"kind": "user", "value": "b@x.test"},
        ],
    )


def decided(env, case=CASE):
    path = env["root"] / case / "workflow.json"
    return (
        json.loads(path.read_text()).get("decision") if path.exists() else None
    )


def ledger(env):
    return env["rows"](models.AgentAction)


def blocked(response, code):
    assert response.status_code == 403, response.text
    body = response.json()["detail"]
    assert body["action_id"] and body["detail"]
    assert code in [r["type"] for r in body["reasons"]]
    return body


# -- engine off and unregistered actors ------------------------------------


def test_engine_off_never_asks_for_an_agent(env, monkeypatch):
    agent(env, "approver:bot", mandate=False)

    def boom(*a, **k):
        raise AssertionError("queried an agent with the engine off")

    monkeypatch.setattr(AgentStore, "by_principal", boom)
    monkeypatch.setattr(AgentStore, "by_owner", boom)
    assert act(env, "approver:bot").status_code == 200
    assert ledger(env) == []


def test_engine_off_never_builds_the_action(env):
    def build():
        raise AssertionError("built an action with the engine off")

    principal = Principal("u-1", "a@x.test", TENANT, "approver")
    assert guard.check(principal, build, settings_store=env["settings"]) is None


def test_an_unregistered_actor_is_untouched_in_enforcing(env):
    env["settings"].mode = "enforcing"
    agent(env, "approver:bot", mandate=False)  # someone else's agent
    assert act(env, "approver:stranger").status_code == 200
    assert decided(env) == "approve" and ledger(env) == []


def test_a_person_with_no_agents_is_untouched_on_a_chain(env):
    env["settings"].mode = "enforcing"
    two_tier(env)
    assert act(env, "approver:a").status_code == 200
    assert act(env, "approver:b").status_code == 200
    assert decided(env) == "approve" and ledger(env) == []


# -- act ---------------------------------------------------------------------


def test_enforcing_refuses_self_approval_and_writes_nothing(env):
    env["settings"].mode = "enforcing"
    agent(env, "approver:uploader")  # the document's uploader
    body = blocked(act(env, "approver:uploader"), "AGENT-SELF-APPROVAL")
    assert decided(env) is None
    assert env["approvals"].chain(TENANT, CASE) == []
    assert "invoice_approved" not in env["events"]()
    (row,) = ledger(env)
    assert row.id == body["action_id"] and row.decision == "BLOCK"
    assert row.amount == Decimal("1069.32") and row.currency == "AUD"
    (finding,) = env["rows"](models.TrustFinding)
    assert finding.type == "AGENT-SELF-APPROVAL"
    assert row.finding_id == finding.id


@pytest.mark.parametrize("mode", ["advisory", "shadow"])
def test_below_enforcing_a_block_is_recorded_and_the_write_happens(env, mode):
    env["settings"].mode = mode
    agent(env, "approver:bot", mandate=False)
    assert act(env, "approver:bot").status_code == 200
    assert decided(env) == "approve"
    (row,) = ledger(env)
    assert row.decision == "BLOCK" and row.mode == mode
    assert len(env["rows"](models.TrustFinding)) == 1


def test_an_agent_inside_its_mandate_is_allowed(env):
    env["settings"].mode = "enforcing"
    agent(env, "approver:bot", caps={"per_action": "5000"})
    assert act(env, "approver:bot").status_code == 200
    (row,) = ledger(env)
    assert row.decision == "ALLOW" and row.finding_id is None
    assert row.subject_ref == f"invoices:{CASE}"


def test_a_reject_needs_the_mandate_too(env):
    env["settings"].mode = "enforcing"
    agent(env, "approver:bot", actions=["invoice.approve"])
    blocked(act(env, "approver:bot", action="reject"), "AGENT-OUT-OF-MANDATE")
    assert decided(env) is None


def test_two_agents_of_one_owner_on_one_chain(env):
    env["settings"].mode = "enforcing"
    two_tier(env)
    agent(env, "approver:a")
    agent(env, "approver:b")
    assert act(env, "approver:a").status_code == 200
    blocked(act(env, "approver:b"), "AGENT-SAME-OWNER-CHAIN")
    assert decided(env) is None


def test_owner_then_their_agent_is_refused_for_the_agent(env):
    env["settings"].mode = "enforcing"
    two_tier(env)
    agent(env, "approver:b", owner="a@x.test")
    assert act(env, "approver:a").status_code == 200
    blocked(act(env, "approver:b"), "AGENT-SAME-OWNER-CHAIN")


def test_agent_then_its_owner_is_refused_for_the_owner(env):
    env["settings"].mode = "enforcing"
    two_tier(env)
    bot = agent(env, "approver:a", owner="b@x.test")
    assert act(env, "approver:a").status_code == 200
    body = blocked(act(env, "approver:b"), "AGENT-SAME-OWNER-CHAIN")
    assert "your agent a" in body["detail"]
    assert decided(env) is None
    refused = [r for r in ledger(env) if r.decision == "BLOCK"]
    assert [r.agent_id for r in refused] == [bot["id"]]


def test_the_owner_check_only_records_below_enforcing(env):
    env["settings"].mode = "advisory"
    two_tier(env)
    agent(env, "approver:a", owner="b@x.test")
    assert act(env, "approver:a").status_code == 200
    assert act(env, "approver:b").status_code == 200
    assert decided(env) == "approve"
    assert [r.decision for r in ledger(env)].count("BLOCK") == 1


# -- export ------------------------------------------------------------------


def export(env, token):
    return env["client"](token).post(
        f"/api/invoices/{CASE}/export", json={"target": "csv"}
    )


def test_enforcing_refuses_an_export_outside_the_mandate(env):
    assert act(env, "approver:person").status_code == 200  # engine off
    env["settings"].mode = "enforcing"
    agent(env, "approver:bot", actions=["invoice.approve"])
    blocked(export(env, "approver:bot"), "AGENT-OUT-OF-MANDATE")
    assert "invoice_exported" not in env["events"]()
    (row,) = ledger(env)
    assert row.action == "invoice.export"


def test_an_allowed_export_goes_out(env):
    assert act(env, "approver:person").status_code == 200
    env["settings"].mode = "enforcing"
    agent(env, "approver:bot")
    r = export(env, "approver:bot")
    assert r.status_code == 200 and r.json()["csv"]
    assert [x.decision for x in ledger(env)] == ["ALLOW"]


# -- vendors -----------------------------------------------------------------


def test_enforcing_refuses_a_vendor_load_and_creates_nothing(env):
    env["settings"].mode = "enforcing"
    agent(env, "admin:bot", actions=["vendor.patch"])
    r = env["client"]("admin:bot").put(
        "/api/vendors", json={"vendors": [VENDOR]}
    )
    blocked(r, "AGENT-OUT-OF-MANDATE")
    assert env["vendors"].list(TENANT) == []


def test_only_new_vendors_are_creates(env):
    env["client"]("admin").put("/api/vendors", json={"vendors": [VENDOR]})
    env["settings"].mode = "advisory"
    agent(env, "admin:bot", actions=["vendor.create"])
    other = {"name": "Brand New Pty", "tax_id": "29BBBBB0000B1Z5"}
    r = env["client"]("admin:bot").put(
        "/api/vendors", json={"vendors": [VENDOR, other]}
    )
    assert r.status_code == 200
    (row,) = ledger(env)
    assert row.action == "vendor.create" and row.vendor_key == "BRAND NEW"
    assert len(env["vendors"].list(TENANT)) == 2


def _vendor(env):
    env["client"]("admin").put("/api/vendors", json={"vendors": [VENDOR]})
    return env["vendors"].list(TENANT)[0]["id"]


def test_enforcing_refuses_a_vendor_patch(env):
    vendor_id = _vendor(env)
    env["settings"].mode = "enforcing"
    agent(env, "admin:bot", mandate=False)
    r = env["client"]("admin:bot").patch(
        f"/api/vendors/{vendor_id}", json={"email_domain": "evil.test"}
    )
    blocked(r, "AGENT-OUT-OF-MANDATE")
    assert env["vendors"].get(TENANT, vendor_id)["email_domain"] is None
    (row,) = ledger(env)
    assert row.action == "vendor.patch" and row.vendor_key


def test_enforcing_refuses_a_bank_change_proposal(env):
    vendor_id = _vendor(env)
    env["settings"].mode = "enforcing"
    agent(env, "clerk:bot", actions=["invoice.approve"])
    r = env["client"]("clerk:bot").post(
        f"/api/vendors/{vendor_id}/bank-changes", json=BANK
    )
    blocked(r, "AGENT-OUT-OF-MANDATE")
    assert env["rows"](models.VendorBankChange) == []


def test_an_agent_never_decides_an_agent_proposed_bank_change(env):
    vendor_id = _vendor(env)
    env["settings"].mode = "advisory"
    agent(
        env, "clerk:proposer", owner="p@x.test", actions=["bank_change.propose"]
    )
    proposed = env["client"]("clerk:proposer").post(
        f"/api/vendors/{vendor_id}/bank-changes", json=BANK
    )
    assert proposed.status_code == 201
    env["settings"].mode = "enforcing"
    agent(
        env,
        "approver:decider",
        owner="d@x.test",
        actions=["bank_change.decide"],
    )
    r = env["client"]("approver:decider").post(
        f"/api/vendors/bank-changes/{proposed.json()['id']}/decide",
        json={"action": "approve"},
    )
    blocked(r, "AGENT-UNVERIFIED-VENDOR")
    (change,) = env["rows"](models.VendorBankChange)
    assert change.status == "pending"


# -- failure (B8) ------------------------------------------------------------


def _break(monkeypatch):
    def down(*a, **k):
        raise RuntimeError("tenant-acme db down")

    monkeypatch.setattr(AgentStore, "current_mandate", down)


def test_a_guard_failure_is_a_503_in_enforcing(env, monkeypatch):
    env["settings"].mode = "enforcing"
    agent(env, "approver:bot")
    _break(monkeypatch)
    r = act(env, "approver:bot")
    assert r.status_code == 503 and "not taken" in r.json()["detail"]
    assert decided(env) is None


def test_a_guard_failure_proceeds_below_enforcing(env, monkeypatch, caplog):
    env["settings"].mode = "advisory"
    agent(env, "approver:bot")
    _break(monkeypatch)
    with caplog.at_level(logging.WARNING):
        assert act(env, "approver:bot").status_code == 200
    (rec,) = [r for r in caplog.records if r.msg == "agent_guard_failed"]
    assert rec.error == "RuntimeError"
    assert TENANT not in rec.getMessage() and not hasattr(rec, "agent_id")


def test_an_unreadable_mode_leaves_the_route_to_fail_as_it_did(env, caplog):
    """The settings and the governed write share one database: when the mode
    cannot be read, the route's own failure is the answer, unchanged."""

    class Down:
        def get(self, tenant_id):
            raise RuntimeError("down")

    def build():
        raise AssertionError("built an action without knowing the mode")

    principal = Principal("u-1", "a@x.test", TENANT, "approver")
    with caplog.at_level(logging.WARNING):
        assert guard.check(principal, build, settings_store=Down()) is None
    assert [r.msg for r in caplog.records] == ["agent_guard_failed"]


# -- caps are hard limits (B17) ----------------------------------------------


def test_two_concurrent_approvals_cannot_both_fit_one_cap(env, monkeypatch):
    if not env["url"].startswith("postgresql"):
        pytest.skip("SQLite serialises writers; the race needs Postgres")
    env["settings"].mode = "enforcing"
    bot = agent(
        env,
        "approver:bot",
        caps={"per_period": {"period": "month", "amount": "1000"}},
    )
    real = AgentStore.spent

    def slow(self, *a, **k):
        out = real(self, *a, **k)
        time.sleep(0.5)  # wide enough for the other approval to slip in
        return out

    monkeypatch.setattr(AgentStore, "spent", slow)
    principal = Principal("u-approver:bot", "bot@x.test", TENANT, "approver")
    results = []

    def approve(ref):
        action = Action(
            kind="invoice.approve",
            subject_table="invoices",
            subject_ref=ref,
            amount=Decimal("600"),
            currency="AUD",
        )
        try:
            guard.check(principal, action, settings_store=env["settings"])
            results.append("ok")
        except guard.HTTPException as exc:
            results.append(exc.status_code)

    threads = [
        threading.Thread(target=approve, args=(f"invoices:c{i}",))
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results, key=str) == [403, "ok"]
    decisions = sorted(
        r.decision for r in ledger(env) if r.agent_id == bot["id"]
    )
    assert decisions == ["ALLOW", "BLOCK"]


def test_advisory_patch_and_proposal_write_and_are_ledgered(env):
    vendor_id = _vendor(env)
    env["settings"].mode = "advisory"
    agent(env, "admin:bot", mandate=False)
    client = env["client"]("admin:bot")
    r = client.patch(
        f"/api/vendors/{vendor_id}", json={"email_domain": "q.test"}
    )
    assert r.status_code == 200
    r = client.post(f"/api/vendors/{vendor_id}/bank-changes", json=BANK)
    assert r.status_code == 201
    assert env["vendors"].get(TENANT, vendor_id)["email_domain"] == "q.test"
    assert len(env["rows"](models.VendorBankChange)) == 1
    assert sorted(r.action for r in ledger(env)) == [
        "bank_change.propose",
        "vendor.patch",
    ]
    assert len(env["rows"](models.TrustFinding)) == 2
