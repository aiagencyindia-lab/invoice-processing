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
from invoice_processing.trust.leakage import clauses

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


# -- Task 2: clause params by regex ------------------------------------------


def test_params_rate():
    assert clauses.params(
        "rate", "The rate for Steel Rod 12mm is INR 1,000.50 per unit."
    ) == {"price": "1000.50"}
    assert clauses.params("rate", "Price: ₹ 2,50,000 per unit") == {
        "price": "250000"
    }


def test_params_escalator():
    assert clauses.params(
        "escalator", "Prices escalate by 5% on each anniversary."
    ) == {"pct": "5"}


def test_params_tier():
    assert clauses.params(
        "tier", "Above 500 units in a contract year the price is Rs. 900."
    ) == {"qty": "500", "price": "900"}


def test_params_rebate():
    assert clauses.params(
        "rebate", "A rebate of 2.5% applies once spend exceeds INR 10,00,000."
    ) == {"threshold": "1000000", "pct": "2.5"}


def test_params_discount_terms():
    assert clauses.params("discount", "Payment terms 2/10 net 30.") == {
        "pct": "2",
        "days": "10",
        "net": "30",
    }
    assert clauses.params(
        "discount", "A 1.5% discount applies if paid within 15 days, net 45."
    ) == {"pct": "1.5", "days": "15", "net": "45"}


def test_params_term_dates():
    assert clauses.params(
        "term", "This agreement runs from 01/04/2025 to 31 March 2026."
    ) == {"start": "2025-04-01", "end": "2026-03-31"}
    assert clauses.params("term", "Effective 2025-01-01 until 2025-12-31.") == {
        "start": "2025-01-01",
        "end": "2025-12-31",
    }


def test_params_unparseable_is_none():
    assert clauses.params("rate", "The rate is agreed separately.") is None
    assert clauses.params("tier", "Tiered pricing applies at INR 5.") is None
    assert clauses.params("term", "Starts on 2025-01-01.") is None
    assert clauses.params("none", "INR 5") is None


def test_candidates_pick_money_percent_and_keywords():
    text = (
        "MASTER SUPPLY AGREEMENT\nThe parties agree as follows.\n"
        "The rate for Steel Rod is Rs. 1,000 per unit. "
        "Prices escalate by 5% yearly.\nGoverning law is Maharashtra."
    )
    assert clauses.candidates(text) == [
        "The rate for Steel Rod is Rs. 1,000 per unit.",
        "Prices escalate by 5% yearly.",
    ]


def test_bank_numbers_are_masked_in_excerpts():
    text = "Pay INR 500 to account 123456789012 at IFSC HDFC0001234."
    [cand] = clauses.candidates(text)
    assert "123456789012" not in cand and "…9012" in cand
