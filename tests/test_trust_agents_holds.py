"""A logged agent action that the guard would have blocked holds the
invoice's payment (Wave 5 B5): only from a log row, only in enforcing, only
on a known invoice, and only a person's decision releases it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from test_trust_agents_guard import CASE, TENANT, act, agent, export
from test_trust_agents_guard import env as guard_env  # noqa: F401 - fixture

from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust import evaluate as trust_evaluate
from invoice_processing.trust import gate
from invoice_processing.trust.graph.links import LinkStore

HELD = "Payment is held until a person decides this finding."
CTX = RunContext(tenant_id=TENANT, case_id="", run_id="r", actor="engine")


@pytest.fixture
def env(guard_env):  # noqa: F811
    url = guard_env["url"]
    with tenant_session(TENANT, url) as s:
        s.add(
            models.Document(
                tenant_id=TENANT, id="d-5", sha256="5" * 64, storage_key="k"
            )
        )
        s.flush()
        s.add(
            models.Invoice(
                tenant_id=TENANT,
                id="inv-5",
                document_id="d-5",
                case_id=CASE,
                total_inc_tax=Decimal("1069.32"),
                status="done",
            )
        )
    store = guard_env["store"]
    node = store.graph.upsert_node(
        CTX,
        kind="invoice",
        natural_key="invoices:inv-5",
        ref_table="invoices",
        ref_id="inv-5",
    )
    bot = agent(guard_env, "approver:bot", mandate=False)  # always BLOCK
    return {**guard_env, "node": node, "bot": bot}


def log(env, subject=f"invoices:{CASE}", source="log"):
    body = {
        "action": "invoice.approve",
        "subject_ref": subject,
        "source": source,
        "occurred_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    }
    r = env["client"]("approver:boss").post(
        f"/api/trust/agents/{env['bot']['id']}/actions/evaluate", json=body
    )
    assert r.status_code == 200 and r.json()["decision"] == "BLOCK", r.text
    return r.json()


def open_holds(env):
    with tenant_session(TENANT, env["url"]) as s:
        return s.scalars(
            select(models.TrustHold).where(models.TrustHold.lifted_at.is_(None))
        ).all()


def findings(env):
    return env["rows"](models.TrustFinding)


def test_a_log_block_in_enforcing_holds_the_invoice(env):
    env["settings"].mode = "enforcing"
    log(env)
    (hold,) = open_holds(env)
    (finding,) = findings(env)
    assert hold.finding_id == finding.id and hold.engine == "agents"
    assert finding.details["hold"] is True
    assert finding.summary.endswith(HELD)
    detail = gate.payment_hold_detail(TENANT, CASE)
    assert detail["held"] and detail["holds"][0]["finding_id"] == finding.id
    r = act(env, "approver:person")
    assert r.status_code == 409, r.text


def test_a_held_approved_invoice_does_not_export_until_rejected(env):
    assert act(env, "approver:person").status_code == 200
    env["settings"].mode = "enforcing"
    log(env)
    assert export(env, "approver:person").status_code == 409
    (finding,) = findings(env)
    r = env["client"]("approver:person").post(
        f"/v2/findings/{finding.id}/decision",
        json={"decision": "reject", "reason": "The log was wrong."},
    )
    assert r.status_code == 200, r.text
    assert open_holds(env) == []
    assert export(env, "approver:person").status_code == 200


@pytest.mark.parametrize("mode", ["advisory", "shadow"])
def test_below_enforcing_a_log_block_is_a_finding_and_no_hold(env, mode):
    env["settings"].mode = mode
    log(env)
    (finding,) = findings(env)
    assert finding.details["hold"] is False
    assert not finding.summary.endswith(HELD)
    assert open_holds(env) == []


def test_a_dryrun_records_no_finding_and_no_hold(env):
    env["settings"].mode = "enforcing"
    log(env, source="dryrun")
    assert findings(env) == [] and open_holds(env) == []


def test_a_log_block_on_a_vendor_holds_nothing(env):
    env["settings"].mode = "enforcing"
    log(env, subject="vendors:v-1")
    (finding,) = findings(env)
    assert finding.details["hold"] is False and open_holds(env) == []


def test_a_log_block_on_an_unknown_invoice_holds_nothing(env):
    env["settings"].mode = "enforcing"
    log(env, subject="invoices:case_nope")
    assert len(findings(env)) == 1 and open_holds(env) == []


def test_an_inline_refusal_never_holds(env):
    env["settings"].mode = "enforcing"
    assert act(env, "approver:bot").status_code == 403
    assert open_holds(env) == []


class Modes:
    def get(self, tenant_id):
        return {
            "trust_engine_modes": {"agents": "enforcing", "payment": "shadow"}
        }


def test_an_engine_run_on_the_invoice_leaves_the_hold(env):
    env["settings"].mode = "enforcing"
    log(env)
    report = trust_evaluate.evaluate(
        CTX,
        "d-5",
        graph=env["store"].graph,
        links=LinkStore(env["url"]),
        url=env["url"],
        settings_store=Modes(),
    )
    assert isinstance(report["engines"]["payment"], dict), report
    assert len(open_holds(env)) == 1
    assert gate.payment_hold_detail(TENANT, CASE)["held"]
