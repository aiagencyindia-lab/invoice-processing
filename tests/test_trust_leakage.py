"""Leakage engine (FR-20): contract compliance, duplicates, statements, radar."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust import leakage
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.engine import EngineContext
from invoice_processing.trust.findings import model as finding_model
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore

T = "tenant-leak"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="worker")
NOW = datetime(2025, 9, 1, tzinfo=timezone.utc)
AT = datetime(2025, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def env(scratch_db):
    url = scratch_db("leakage.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Leak", slug="leak", status="active"))
    return url, GraphStore(url), LinkStore(url)


def make_ectx(
    env,
    *,
    document=None,
    invoice_node=None,
    decider=None,
    blobs=None,
    settings=None,
    now=NOW,
):
    url, graph, links = env
    return EngineContext(
        ctx=CTX,
        mode="advisory",
        document=document,
        invoice_node=invoice_node,
        graph=graph,
        links=links,
        findings=None,
        providers=None,
        settings=settings or {},
        decider=decider or FakeDecider({}, fail=True),
        blobs=blobs,
        url=url,
        now=now,
    )


# -- Task 1: skeleton --------------------------------------------------------


def test_types_are_registered_to_leakage_and_none_hold():
    codes = {
        "LEAK-RATE",
        "LEAK-ESCALATOR",
        "LEAK-TIER",
        "LEAK-REBATE",
        "LEAK-DISCOUNT",
        "LEAK-TERM",
        "DUP-NEAR",
        "DUP-SPLIT",
        "DUP-CROSS",
        "STMT-UNAPPLIED-CREDIT",
        "STMT-DOUBLE-PAID",
        "STMT-DISPUTED",
        "RADAR-RENEWAL",
        "RADAR-ESCALATOR",
    }
    assert leakage.ENGINE_VERSION == "leakage-1"
    for code in codes:
        assert finding_model.TYPE_ENGINE[code] == "leakage"
        assert code not in finding_model.HOLD_TYPES


def test_empty_graph_finds_nothing(env):
    assert leakage.run(make_ectx(env)) == []
