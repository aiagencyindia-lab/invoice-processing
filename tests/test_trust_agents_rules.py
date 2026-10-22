"""The structural controls (FR-28.3): each rule alone, over a real ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fresh_schema import fresh_schema

from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.agents import rules
from invoice_processing.trust.agents.policy import Action, evaluate_action
from invoice_processing.trust.agents.store import AgentStore

T = "tenant-acme"
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
CTX = RunContext(tenant_id=T, case_id="", run_id="", actor="admin@acme.test")
BOT = f"bot@api-key.{T}"
TWIN = f"twin@api-key.{T}"
OWNER = "ops@acme.test"
SCOPE = {
    "actions": [
        "invoice.approve",
        "invoice.reject",
        "invoice.export",
        "bank_change.decide",
        "vendor.create",
    ],
    "currency": "INR",
    "counterparties": None,
    "caps": {},
}


@pytest.fixture
def env(scratch_db):
    url = scratch_db("agents_rules.db")
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    store = AgentStore(url)

    def register(name, owner=OWNER):
        return store.register(
            CTX,
            name=name,
            owner=owner,
            kind="api_key",
            principal_id=f"api-key:{name}",
            principal_email=f"{name}@api-key.{T}",
        )

    return {"url": url, "store": store, "bot": register("bot"), "reg": register}


def mandate(scope=None, **caps):
    return {
        "id": "m1",
        "version": 1,
        "scope": {**SCOPE, **(scope or {}), "caps": caps},
    }


def approve(**kw):
    return Action(
        **{
            "kind": "invoice.approve",
            "subject_table": "invoices",
            "subject_ref": "invoices:case_001",
            "vendor_key": "ACME SUPPLIES",
            "category": "service",
            "amount": Decimal("100.00"),
            "currency": "INR",
            **kw,
        }
    )


def ledger(env, agent=None, **cols):
    row = {
        "agent_id": (agent or env["bot"])["id"],
        "action": "invoice.approve",
        "subject_table": "invoices",
        "subject_ref": "invoices:old",
        "vendor_key": "ACME SUPPLIES",
        "category": "service",
        "amount": Decimal("100.00"),
        "currency": "INR",
        "decision": "ALLOW",
        "reasons": [],
        "explanation": "",
        "mode": "advisory",
        "source": "inline",
        "occurred_at": NOW - timedelta(hours=1),
        **cols,
    }
    with tenant_session(T, env["url"]) as s:
        env["store"].record_action(s, CTX, **row)


def run(rule, env, action, m=None, agent=None):
    return rule(
        action,
        agent or env["bot"],
        mandate() if m is None else m,
        env["store"],
        {},
        NOW,
        None,
    )


# -- currency (U10) --------------------------------------------------------


def test_an_approve_in_another_currency_is_out_of_mandate(env):
    r = run(rules.currency, env, approve(currency="USD"))
    assert r.type == "AGENT-OUT-OF-MANDATE" and r.severity == "BLOCK"
    assert "USD" in r.message and "INR" in r.message


def test_currency_compares_case_blind_and_only_for_approvals(env):
    assert run(rules.currency, env, approve(currency="inr")) is None
    export = approve(kind="invoice.export", currency="USD")
    assert run(rules.currency, env, export) is None


def test_an_approve_with_no_currency_is_out_of_mandate(env):
    assert run(rules.currency, env, approve(currency=None)) is not None


# -- mandate_active --------------------------------------------------------


def test_no_mandate_blocks(env):
    r = rules.mandate_active(
        approve(), env["bot"], None, env["store"], {}, NOW, None
    )
    assert r.type == "AGENT-OUT-OF-MANDATE"
    assert "no active mandate" in r.message


def test_a_revoked_agent_blocks_even_with_a_mandate(env):
    revoked = {**env["bot"], "status": "revoked"}
    r = run(rules.mandate_active, env, approve(), agent=revoked)
    assert r.type == "AGENT-OUT-OF-MANDATE" and "revoked" in r.message


def test_an_active_agent_with_a_mandate_passes(env):
    assert run(rules.mandate_active, env, approve()) is None


# -- in_scope --------------------------------------------------------------


def test_an_action_outside_the_scope_blocks(env):
    r = run(rules.in_scope, env, approve(kind="vendor.patch"))
    assert r.type == "AGENT-OUT-OF-MANDATE" and "vendor.patch" in r.message


def test_a_counterparty_outside_the_list_blocks(env):
    m = mandate({"counterparties": ["acme supplies"]})
    assert run(rules.in_scope, env, approve(), m) is None
    r = run(rules.in_scope, env, approve(vendor_key="ROGUE"), m)
    assert r.type == "AGENT-OUT-OF-MANDATE"


def test_an_action_without_a_vendor_skips_the_counterparty_test(env):
    m = mandate({"counterparties": ["acme supplies"]})
    assert run(rules.in_scope, env, approve(vendor_key=None), m) is None


def test_in_scope_says_nothing_without_a_mandate(env):
    r = rules.in_scope(approve(), env["bot"], None, env["store"], {}, NOW, None)
    assert r is None  # mandate_active already said it


# -- self_approval ---------------------------------------------------------


@pytest.mark.parametrize(
    "kind", ["invoice.approve", "invoice.reject", "bank_change.decide"]
)
def test_deciding_what_it_initiated_blocks(env, kind):
    r = run(
        rules.self_approval, env, approve(kind=kind, initiated_by=BOT.upper())
    )
    assert r.type == "AGENT-SELF-APPROVAL"


def test_someone_elses_upload_passes_and_export_is_not_a_decision(env):
    assert (
        run(rules.self_approval, env, approve(initiated_by=OWNER + "x")) is None
    )
    export = approve(kind="invoice.export", initiated_by=BOT)
    assert run(rules.self_approval, env, export) is None


# -- same_owner_chain (U9) -------------------------------------------------


def test_the_owner_already_deciding_blocks_the_agent(env):
    r = run(
        rules.same_owner_chain, env, approve(prior_deciders=("OPS@acme.test",))
    )
    assert r.type == "AGENT-SAME-OWNER-CHAIN"


def test_the_owner_initiating_blocks_the_agent(env):
    r = run(rules.same_owner_chain, env, approve(initiated_by=OWNER))
    assert r.type == "AGENT-SAME-OWNER-CHAIN"


def test_a_sibling_agent_with_the_same_owner_blocks(env):
    env["reg"]("twin")
    r = run(rules.same_owner_chain, env, approve(prior_deciders=(TWIN,)))
    assert r.type == "AGENT-SAME-OWNER-CHAIN"


def test_another_owners_agent_and_other_people_pass(env):
    env["reg"]("stranger", owner="else@acme.test")
    action = approve(
        prior_deciders=(f"stranger@api-key.{T}", "clerk@acme.test"),
        initiated_by="clerk@acme.test",
    )
    assert run(rules.same_owner_chain, env, action) is None


def test_its_own_earlier_step_is_not_a_sibling(env):
    # Deciding twice is the approval chain's own refusal, not this rule's.
    assert (
        run(rules.same_owner_chain, env, approve(prior_deciders=(BOT,))) is None
    )


# -- over_limit (B9, B17) --------------------------------------------------


def test_per_action_cap_is_inclusive(env):
    assert (
        run(rules.over_limit, env, approve(), mandate(per_action="100.00"))
        is None
    )
    over = approve(amount=Decimal("100.0001"))
    r = run(rules.over_limit, env, over, mandate(per_action="100.00"))
    assert r.type == "AGENT-OVER-LIMIT" and "per-action" in r.message


def test_per_period_counts_what_was_approved_this_month(env):
    ledger(env, amount=Decimal("900"))
    ledger(
        env,
        amount=Decimal("5000"),
        occurred_at=datetime(2026, 9, 30, tzinfo=UTC),
    )
    m = mandate(per_period={"period": "month", "amount": "1000"})
    assert run(rules.over_limit, env, approve(), m) is None
    r = run(rules.over_limit, env, approve(amount=Decimal("100.01")), m)
    assert r.type == "AGENT-OVER-LIMIT" and "month" in r.message


def test_a_day_period_starts_at_utc_midnight(env):
    ledger(
        env,
        amount=Decimal("900"),
        occurred_at=datetime(2026, 10, 21, 23, tzinfo=UTC),
    )
    m = mandate(per_period={"period": "day", "amount": "150"})
    assert run(rules.over_limit, env, approve(), m) is None


def test_blocked_dry_run_and_other_currency_rows_spent_nothing(env):
    ledger(env, amount=Decimal("900"), decision="BLOCK")
    ledger(env, amount=Decimal("900"), source="dryrun")
    ledger(env, amount=Decimal("900"), currency="USD")
    m = mandate(per_period={"period": "month", "amount": "100"})
    assert run(rules.over_limit, env, approve(), m) is None


def test_a_concurrent_row_recorded_after_now_still_counts(env):
    ledger(env, amount=Decimal("60"), occurred_at=NOW + timedelta(seconds=1))
    m = mandate(per_period={"period": "month", "amount": "100"})
    assert (
        run(rules.over_limit, env, approve(amount=Decimal("60")), m) is not None
    )


def test_per_vendor_and_per_category_caps(env):
    ledger(env, amount=Decimal("80"), vendor_key="other", category="goods")
    m = mandate(per_vendor="150")
    assert run(rules.over_limit, env, approve(), m) is None
    ledger(env, amount=Decimal("80"))
    assert "vendor" in run(rules.over_limit, env, approve(), m).message
    m = mandate(per_category={"service": "150", "goods": "1"})
    assert "service" in run(rules.over_limit, env, approve(), m).message


def test_an_unknown_amount_under_a_cap_blocks(env):
    r = run(
        rules.over_limit, env, approve(amount=None), mandate(per_action="1")
    )
    assert r.type == "AGENT-OVER-LIMIT"


def test_no_caps_and_non_approvals_are_never_over(env):
    assert run(rules.over_limit, env, approve(amount=Decimal("1e9"))) is None
    export = approve(kind="invoice.export", amount=Decimal("1e9"))
    assert run(rules.over_limit, env, export, mandate(per_action="1")) is None


# -- unverified_vendor (B11) -----------------------------------------------


def test_approving_an_agent_created_vendor_unverified_blocks(env):
    ledger(env, action="vendor.create", amount=None, subject_table="vendors")
    for kind in ("invoice.approve", "invoice.export"):
        r = run(
            rules.unverified_vendor,
            env,
            approve(kind=kind, vendor_verified=False),
        )
        assert r.type == "AGENT-UNVERIFIED-VENDOR"


def test_a_bank_an_agent_verified_is_not_verified(env):
    ledger(env, action="vendor.create", amount=None)
    action = approve(vendor_verified=True, vendor_verified_by=BOT)
    assert run(rules.unverified_vendor, env, action) is not None


def test_a_person_verified_bank_or_a_human_created_vendor_passes(env):
    assert (
        run(rules.unverified_vendor, env, approve(vendor_verified=False))
        is None
    )
    ledger(env, action="vendor.create", amount=None)
    ok = approve(vendor_verified=True, vendor_verified_by="cfo@acme.test")
    assert run(rules.unverified_vendor, env, ok) is None


def test_a_blocked_create_made_nothing(env):
    ledger(env, action="vendor.create", amount=None, decision="BLOCK")
    assert (
        run(rules.unverified_vendor, env, approve(vendor_verified=False))
        is None
    )


def test_an_agent_never_decides_an_agent_proposed_bank_change(env):
    other = env["reg"]("stranger", owner="else@acme.test")
    decide = approve(
        kind="bank_change.decide",
        subject_table="vendor_bank_changes",
        initiated_by=other["principal_email"],
        amount=None,
    )
    assert (
        run(rules.unverified_vendor, env, decide).type
        == "AGENT-UNVERIFIED-VENDOR"
    )
    human = approve(kind="bank_change.decide", initiated_by="clerk@acme.test")
    assert run(rules.unverified_vendor, env, human) is None


# -- composition -----------------------------------------------------------


def test_the_rules_run_in_severity_order_and_a_clean_action_is_allowed(env):
    assert [r.__name__ for r in rules.RULES] == [
        "currency",
        "mandate_active",
        "in_scope",
        "self_approval",
        "same_owner_chain",
        "over_limit",
        "unverified_vendor",
    ]
    out = evaluate_action(
        CTX,
        approve(),
        agent=env["bot"],
        mandate=mandate(per_action="1000"),
        store=env["store"],
        settings={},
        now=NOW,
    )
    assert out.decision == "ALLOW"


def test_policy_imports_first_without_a_cycle():
    """Importing policy before rules must work: policy imports rules before
    it defines Reason."""
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    code = (
        "import invoice_processing.trust.agents.policy as p; "
        "assert len(p.rules.RULES) == 7"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, check=False
    )
    assert done.returncode == 0, done.stderr.decode()[-500:]


# -- the fix pass (fable review) --------------------------------------------


def test_counterparties_are_compared_normalised(env):
    m = mandate({"counterparties": ["Acme Supplies"]})
    assert run(rules.in_scope, env, approve(), m) is None


def test_a_padded_currency_matches(env):
    assert run(rules.currency, env, approve(currency=" inr ")) is None


def test_a_negative_amount_cannot_lower_spend(env):
    r = run(
        rules.over_limit,
        env,
        approve(amount=Decimal("-900")),
        mandate(per_action="1000"),
    )
    assert r.type == "AGENT-OVER-LIMIT" and "negative" in r.message


def test_vendor_and_category_caps_need_a_known_key(env):
    r = run(
        rules.over_limit,
        env,
        approve(vendor_key=None),
        mandate(per_vendor="500"),
    )
    assert r is not None and "vendor" in r.message
    m = mandate(per_category={"service": "500"})
    r = run(rules.over_limit, env, approve(category=None), m)
    assert r is not None and "category" in r.message


def test_a_naive_log_time_is_read_as_utc(env, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    import time  # noqa: PLC0415

    time.tzset()
    try:
        # 03:00 on the 21st read as Kolkata time is the 20th in UTC; read
        # as UTC (as the ledger does) it is the 21st, with the row below.
        ledger(
            env,
            amount=Decimal("100"),
            occurred_at=datetime(2026, 10, 21, 1, tzinfo=UTC),
        )
        m = mandate(per_period={"period": "day", "amount": "150"})
        late = approve(occurred_at=datetime(2026, 10, 21, 3))
        assert run(rules.over_limit, env, late, m) is not None
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
