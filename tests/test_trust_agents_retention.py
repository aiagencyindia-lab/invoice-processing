"""The worker's daily `agent_actions` retention sweep (Wave 5, program plan B7)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fresh_schema import fresh_schema

from invoice_processing import tenant_settings
from invoice_processing.api import service
from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.agents.store import AgentStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.worker import runner
from invoice_processing.worker.queue import LocalQueue
from invoice_processing.worker.runner import ClaimStore, Worker

A, B = "tenant-acme", "tenant-beta"
NOW = datetime.now(UTC)


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("agents_retention.db")
    fresh_schema(url)
    for t, slug in ((A, "acme"), (B, "beta")):
        with tenant_session(t, url) as s:
            s.add(models.Tenant(id=t, name=slug, slug=slug, status="active"))
    monkeypatch.setenv("DATABASE_URL", url)
    store = AgentStore(url, graph=GraphStore(url))
    monkeypatch.setattr(service, "_agent_store", store)
    overrides: dict[str, object] = {}

    def effective(tenant_id, store=None):
        got = overrides.get(tenant_id, {})
        if isinstance(got, Exception):
            raise got
        return {"trust_thresholds": got}

    monkeypatch.setattr(tenant_settings, "effective", effective)
    now = {"t": 1000.0}
    worker = Worker(
        queue=LocalQueue(tmp_path / "q"),
        claims=ClaimStore(tmp_path / "claims"),
        handler=lambda m: {},
        clock=lambda: now["t"],
    )
    agents = {}
    for t in (A, B):
        ctx = RunContext(tenant_id=t, case_id="", run_id="", actor="a@x.test")
        agent = store.register(
            ctx,
            name="bot",
            owner="o@x.test",
            principal_id=None,
            kind="external",
        )
        agents[t] = agent["id"]
        for days, action in (
            (500, "invoice.approve"),
            (500, "vendor.create"),
            (100, "invoice.approve"),
            (10, "invoice.approve"),
        ):
            with tenant_session(t, url) as s:
                store.record_action(
                    s,
                    ctx,
                    agent_id=agent["id"],
                    action=action,
                    decision="ALLOW",
                    source="inline",
                    occurred_at=NOW - timedelta(days=days),
                    currency="INR",
                    vendor_key="acme supplies",
                )
    return {
        "store": store,
        "worker": worker,
        "now": now,
        "overrides": overrides,
        "agents": agents,
    }


def ages(env, t):
    rows = env["store"].actions(t, agent_id=env["agents"][t])
    return sorted(((NOW - r["occurred_at"]).days, r["action"]) for r in rows)


KEPT = [
    (10, "invoice.approve"),
    (100, "invoice.approve"),
    (500, "vendor.create"),
]


def test_the_sweep_deletes_only_old_non_create_rows_per_tenant(env):
    env["worker"]._purge_agent_actions_if_due()
    assert ages(env, A) == KEPT and ages(env, B) == KEPT
    env["now"]["t"] += runner.RETENTION_SCAN_SECONDS + 1
    env["worker"]._purge_agent_actions_if_due()  # idempotent
    assert ages(env, A) == KEPT and ages(env, B) == KEPT


def test_one_tenants_failure_does_not_stop_the_others(env, caplog):
    env["overrides"][A] = RuntimeError("settings unreadable")
    env["worker"]._purge_agent_actions_if_due()
    assert len(ages(env, A)) == 4
    assert ages(env, B) == KEPT
    assert "retention sweep failed for a tenant" in caplog.text


def test_the_sweep_runs_once_per_interval(env, monkeypatch):
    calls = []
    monkeypatch.setattr(
        env["store"], "purge_before", lambda t, before: calls.append(t) or 0
    )
    worker = env["worker"]
    worker._purge_agent_actions_if_due()
    worker._purge_agent_actions_if_due()
    assert sorted(calls) == [A, B]
    env["now"]["t"] += runner.RETENTION_SCAN_SECONDS + 1
    worker._purge_agent_actions_if_due()
    assert len(calls) == 4


def test_a_failing_sweep_does_not_stop_the_worker(env, monkeypatch, caplog):
    def boom():
        raise RuntimeError("no database")

    monkeypatch.setattr(service, "get_agent_store", boom)
    env["worker"]._purge_agent_actions_if_due()  # logged, not raised
    assert "Agent-action retention sweep failed" in caplog.text


def test_retention_below_the_floor_is_raised():
    days = runner._retention_days
    assert (
        days({"agents.retention_days": 1, "agents.split_window_days": 7}) == 31
    )
    assert (
        days({"agents.retention_days": 1, "agents.split_window_days": 60}) == 61
    )
    assert (
        days({"agents.retention_days": 90, "agents.split_window_days": 7}) == 90
    )
    for bad in ("abc", -5, "nan", "inf", None, True):
        assert (
            days({"agents.retention_days": bad, "agents.split_window_days": 7})
            == 400
        )
    assert (
        days({"agents.retention_days": 1, "agents.split_window_days": "x"})
        == 31
    )


def test_a_low_tenant_retention_keeps_the_floor(env):
    env["overrides"][A] = {"agents.retention_days": 1}
    env["worker"]._purge_agent_actions_if_due()
    # The floor is 31 days: the 100-day row goes, the 10-day row stays.
    assert ages(env, A) == [(10, "invoice.approve"), (500, "vendor.create")]
    assert ages(env, B) == KEPT


def test_purged_rows_are_logged_without_the_tenant(env, caplog):
    caplog.set_level(logging.INFO, logger=runner.__name__)
    env["worker"]._purge_agent_actions_if_due()
    logged = [
        r for r in caplog.records if r.getMessage() == "agent_actions_purged"
    ]
    assert [r.rows for r in logged] == [1, 1]
    assert all(A not in str(vars(r)) and B not in str(vars(r)) for r in logged)
