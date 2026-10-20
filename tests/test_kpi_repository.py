"""The KPI numbers: computed from typed columns, honest about what they skip."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fresh_schema import fresh_schema

from invoice_processing.db import repositories
from invoice_processing.db.models import (
    ApprovalStep,
    Document,
    Finding,
    Invoice,
    PipelineRun,
    Tenant,
)
from invoice_processing.db.repositories import (
    BUCKETS,
    KpiRepository,
    exception_bucket,
    percentile,
)
from invoice_processing.db.session import tenant_session

ACME = "tenant-acme"
GLOBEX = "tenant-globex"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


@pytest.fixture
def db(scratch_db):
    url = scratch_db("kpi.db")
    fresh_schema(url)
    for tenant_id, slug in ((ACME, "acme"), (GLOBEX, "globex")):
        with tenant_session(tenant_id, url) as session:
            session.add(Tenant(id=tenant_id, slug=slug, name=slug))
    return url


def seed(
    session,
    case_id,
    *,
    tenant_id=ACME,
    received=None,
    decided=None,
    human=None,
    decision="ACCEPT",
    routing="AUTO",
    po=None,
    held=False,
    chain=False,
    cost=None,
    kind=None,
    document=True,
):
    """One invoice with exactly the facts the KPI query reads."""
    received = received or days_ago(1)
    if document:
        session.add(
            Document(
                id=case_id,
                tenant_id=tenant_id,
                storage_key=f"k/{case_id}",
                sha256="0" * 64,
                created_at=received,
                kind=kind,
            )
        )
    invoice = Invoice(
        tenant_id=tenant_id,
        case_id=case_id,
        document_id=case_id if document else None,
        created_at=received,
        decided_at=decided,
        human_decision=human,
        decision=decision,
        routing=routing,
        po_number=po,
        fields={},
    )
    session.add(invoice)
    session.flush()
    if held:
        session.add(
            Finding(
                tenant_id=tenant_id,
                invoice_id=invoice.id,
                phase="phase2",
                step="2.3",
                passed=False,
            )
        )
    if chain:
        session.add(
            ApprovalStep(
                tenant_id=tenant_id,
                case_id=case_id,
                position=1,
                assignee_kind="user",
                assignee_value="a@acme.test",
                status="pending",
            )
        )
    if cost is not None:
        for i, value in enumerate(cost if isinstance(cost, list) else [cost]):
            session.add(
                PipelineRun(
                    tenant_id=tenant_id,
                    invoice_id=invoice.id,
                    cost_usd=value,
                    created_at=received + timedelta(minutes=i),
                )
            )
    return invoice


class TestPureParts:
    def test_percentile_is_nearest_rank(self):
        assert percentile([1, 2, 3, 4], 0.5) == 2
        assert percentile([1, 2, 3, 4], 0.9) == 4
        assert percentile([5], 0.5) == 5
        assert percentile([], 0.5) is None

    @pytest.mark.parametrize(
        ("facts", "bucket"),
        [
            (
                {
                    "decision": "REJECT",
                    "held": True,
                    "routing": "REVIEW",
                    "chain": True,
                    "human_decision": "approve",
                },
                "rejected_by_rules",
            ),
            (
                {
                    "decision": "ACCEPT",
                    "held": True,
                    "routing": "REVIEW",
                    "chain": True,
                    "human_decision": None,
                },
                "three_way_hold",
            ),
            (
                {
                    "decision": "ACCEPT",
                    "held": False,
                    "routing": "REVIEW",
                    "chain": True,
                    "human_decision": None,
                },
                "low_confidence",
            ),
            (
                {
                    "decision": "ACCEPT",
                    "held": False,
                    "routing": "AUTO",
                    "chain": True,
                    "human_decision": "approve",
                },
                "approval_required",
            ),
            (
                {
                    "decision": "ACCEPT",
                    "held": False,
                    "routing": "AUTO",
                    "chain": False,
                    "human_decision": "reject",
                },
                "manual_decision",
            ),
            (
                {
                    "decision": "ACCEPT",
                    "held": False,
                    "routing": "AUTO",
                    "chain": False,
                    "human_decision": None,
                },
                "other",
            ),
            (
                {
                    "decision": None,
                    "held": False,
                    "routing": None,
                    "chain": False,
                    "human_decision": None,
                },
                "other",
            ),
        ],
    )
    def test_bucket_order_is_first_match_wins(self, facts, bucket):
        assert exception_bucket(**facts) == bucket
        assert bucket in BUCKETS


class TestSummary:
    def test_counts_touchless_and_every_bucket(self, db):
        with tenant_session(ACME, db) as session:
            seed(session, "auto", decided=days_ago(0.9))
            seed(session, "rej", decision="REJECT", routing="REVIEW")
            seed(session, "held", routing="REVIEW", po="PO-1", held=True)
            seed(session, "low", routing="REVIEW")
            seed(session, "chain", chain=True, decided=days_ago(0.9))
            seed(session, "manual", human="approve", decided=days_ago(0.5))
            seed(session, "legacy")  # AUTO, accept, never decided: other
        body = KpiRepository(db).summary(ACME, days_ago(30), now=NOW)
        assert body["received"] == 7
        assert body["touchless"] == 1
        assert body["decided"] == 2  # auto + manual; the chain is pending
        assert body["touchless_rate"] == pytest.approx(1 / 7, abs=0.001)
        assert body["exception_rate"] == pytest.approx(6 / 7, abs=0.001)
        assert body["exceptions"] == {
            "rejected_by_rules": 1,
            "three_way_hold": 1,
            "low_confidence": 1,
            "approval_required": 1,
            "manual_decision": 1,
            "other": 1,
        }

    def test_a_pending_chain_is_open_even_with_a_touchless_decided_at(self, db):
        """The worker projects before it routes the chain, so a touchless
        decided_at can exist while an approver still has to act."""
        with tenant_session(ACME, db) as session:
            seed(session, "chain", chain=True, decided=days_ago(0.9))
        body = KpiRepository(db).summary(ACME, None, now=NOW)
        assert body["decided"] == 0
        assert body["touchless"] == 0
        assert body["open"]["count"] == 1

    def test_cycle_time_percentiles_and_bad_clocks(self, db):
        with tenant_session(ACME, db) as session:
            seed(
                session,
                "a",
                received=days_ago(10),
                decided=days_ago(9.5),
                human="approve",
            )
            seed(
                session,
                "b",
                received=days_ago(10),
                decided=days_ago(9),
                human="approve",
            )
            seed(
                session,
                "c",
                received=days_ago(10),
                decided=days_ago(8),
                human="approve",
            )
            # Decided before it arrived: a clock problem, never a fast decision.
            seed(
                session,
                "bad",
                received=days_ago(1),
                decided=days_ago(2),
                human="approve",
            )
        body = KpiRepository(db).summary(ACME, None, now=NOW)
        assert body["cycle_hours"] == {
            "median": 24.0,
            "p90": 48.0,
            "skipped": 1,
        }

    def test_open_age_is_the_oldest(self, db):
        with tenant_session(ACME, db) as session:
            seed(session, "young", received=days_ago(1), routing="REVIEW")
            seed(session, "old", received=days_ago(4), routing="REVIEW")
        body = KpiRepository(db).summary(ACME, None, now=NOW)
        assert body["open"] == {"count": 2, "oldest_hours": 96.0}

    def test_hold_rate_is_over_po_backed_invoices(self, db):
        with tenant_session(ACME, db) as session:
            seed(session, "po-held", po="PO-1", routing="REVIEW", held=True)
            seed(session, "po-ok", po="PO-2", decided=days_ago(0.5))
            seed(session, "non-po", decided=days_ago(0.5))
        body = KpiRepository(db).summary(ACME, None, now=NOW)
        assert body["three_way"] == {
            "po_invoices": 2,
            "held": 1,
            "hold_rate": 0.5,
        }

    def test_a_held_invoice_without_a_po_is_not_counted_as_held(self, db):
        """hold_rate is held/PO-backed; counting a PO-less hold in the
        numerator alone could push it past 1."""
        with tenant_session(ACME, db) as session:
            seed(session, "no-po-held", routing="REVIEW", held=True)
            seed(session, "po-ok", po="PO-2", decided=days_ago(0.5))
        body = KpiRepository(db).summary(ACME, None, now=NOW)
        assert body["three_way"] == {
            "po_invoices": 1,
            "held": 0,
            "hold_rate": 0.0,
        }

    def test_runs_written_in_the_same_instant_resolve_deterministically(
        self, db
    ):
        with tenant_session(ACME, db) as session:
            invoice = seed(session, "tie", decided=days_ago(0.5))
            at = days_ago(1)
            for run_id, value in (("a" * 36, 0.01), ("z" * 36, 0.05)):
                session.add(
                    PipelineRun(
                        id=run_id,
                        tenant_id=ACME,
                        invoice_id=invoice.id,
                        cost_usd=value,
                        created_at=at,
                    )
                )
        body = KpiRepository(db).summary(ACME, None, now=NOW)
        assert body["ai_cost"]["total_usd"] == 0.05

    def test_cost_uses_the_latest_run_when_duplicates_exist(self, db):
        with tenant_session(ACME, db) as session:
            seed(session, "dup", cost=[0.01, 0.01, 0.02], decided=days_ago(0.5))
            seed(session, "one", cost=0.04, decided=days_ago(0.5))
            seed(session, "none", decided=days_ago(0.5))
        body = KpiRepository(db).summary(ACME, None, now=NOW)
        assert body["ai_cost"] == {"per_invoice_usd": 0.03, "total_usd": 0.06}

    def test_window_scans_and_other_tenants_are_excluded(self, db):
        with tenant_session(ACME, db) as session:
            seed(session, "in", received=days_ago(5), decided=days_ago(4))
            seed(session, "out", received=days_ago(40), decided=days_ago(39))
            seed(session, "scan", kind="goods_receipt", decision="SUPPORTING")
            seed(session, "nodoc", document=False, decided=days_ago(0.5))
        with tenant_session(GLOBEX, db) as session:
            seed(session, "theirs", tenant_id=GLOBEX, decided=days_ago(0.5))
        body = KpiRepository(db).summary(ACME, days_ago(30), now=NOW)
        assert body["received"] == 2  # "in" and "nodoc"
        assert KpiRepository(db).summary(GLOBEX, None, now=NOW)["received"] == 1

    def test_an_empty_window_has_null_rates(self, db):
        with tenant_session(ACME, db) as session:
            seed(session, "old", received=days_ago(40))
        body = KpiRepository(db).summary(ACME, days_ago(7), now=NOW)
        assert body["received"] == 0
        assert body["touchless_rate"] is None
        assert body["exception_rate"] is None
        assert body["cycle_hours"] == {
            "median": None,
            "p90": None,
            "skipped": 0,
        }
        assert body["open"] == {"count": 0, "oldest_hours": None}
        assert body["three_way"] == {
            "po_invoices": 0,
            "held": 0,
            "hold_rate": None,
        }
        assert body["ai_cost"] == {"per_invoice_usd": None, "total_usd": 0.0}


class TestCases:
    def test_lists_the_case_ids_behind_a_reason_newest_first(self, db):
        with tenant_session(ACME, db) as session:
            seed(session, "older", received=days_ago(3), routing="REVIEW")
            seed(session, "newer", received=days_ago(2), routing="REVIEW")
            seed(session, "auto", decided=days_ago(0.5))
        body = KpiRepository(db).cases(ACME, None, "low_confidence")
        assert body == {"case_ids": ["newer", "older"], "truncated": False}

    def test_is_capped(self, db, monkeypatch):
        monkeypatch.setattr(repositories, "KPI_CASES_LIMIT", 2)
        with tenant_session(ACME, db) as session:
            for i in range(3):
                seed(
                    session, f"c{i}", received=days_ago(i + 1), routing="REVIEW"
                )
        body = KpiRepository(db).cases(ACME, None, "low_confidence")
        assert len(body["case_ids"]) == 2
        assert body["truncated"] is True
