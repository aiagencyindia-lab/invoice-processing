"""Engine readiness: evidence for promoting an engine, never a promotion (U2)."""

from datetime import UTC, datetime, timedelta

import pytest

from invoice_processing.trust import thresholds
from invoice_processing.trust.score import readiness as rd

NOW = datetime(2026, 10, 21, tzinfo=UTC)
T = thresholds.merged(None)


def rec(
    mode,
    *,
    days=30,
    findings=50,
    decided=20,
    precision=0.9,
    failures=0,
    insight=False,
):
    return rd.recommend(
        mode,
        days=days,
        findings=findings,
        decided=decided,
        precision=precision,
        failures=failures,
        t=T,
        insight_only=insight,
    )


@pytest.mark.parametrize(
    ("mode", "kw", "to", "words"),
    [
        ("off", {"days": 0, "findings": 0}, "shadow", None),
        ("shadow", {}, "advisory", None),
        ("shadow", {"days": 3}, None, "14 days"),
        ("shadow", {"findings": 2}, None, "20 findings"),
        ("advisory", {}, "enforcing", None),
        ("advisory", {"days": 13}, None, "14 days"),
        ("advisory", {"decided": 9}, None, "10 decided"),
        ("advisory", {"precision": 0.79}, None, "precision"),
        ("advisory", {"precision": None}, None, "precision"),
        ("advisory", {"failures": 1}, None, "failure"),
        ("advisory", {"insight": True}, None, "enforcing adds nothing"),
        ("enforcing", {}, None, "enforcing"),
    ],
)
def test_every_recommendation_branch(mode, kw, to, words):
    r = rec(mode, **kw)
    assert r["to"] == to
    assert r["reason"]
    if words:
        assert words in r["reason"]


def fnd(type_, *, engine="payment", mode="advisory", fid=None):
    return {
        "id": fid or f"f-{type_}-{mode}",
        "type": type_,
        "status": "detected",
        "details": {"engine": engine, "mode": mode},
    }


def evt(fid, to, actor="a@x", type_="PAY-UNVERIFIED"):
    return {"finding_id": fid, "type": type_, "to_status": to, "actor": actor}


def test_a_row_counts_the_engines_own_evidence():
    since = NOW - timedelta(days=20)
    findings = [
        fnd("PAY-UNVERIFIED", mode="shadow", fid="a"),
        fnd("PAY-UNVERIFIED", mode="advisory", fid="b"),
        fnd("PAY-UNVERIFIED", mode="enforcing", fid="c"),
        fnd("PAY-ANOMALY", fid="d"),
        fnd("LEAK-RATE", engine="leakage", fid="e"),
        {
            "id": "x",
            "type": "TRUST-ENGINE-FAILED",
            "status": "detected",
            "details": {"engine": "payment", "mode": "enforcing"},
        },
    ]
    events = [
        evt("a", "accepted"),
        evt("b", "accepted"),
        evt("b", "claimed"),
        evt("c", "rejected"),
        evt("d", "rejected", actor="system", type_="PAY-ANOMALY"),
        evt("e", "rejected", type_="LEAK-RATE"),
    ]
    holds = [
        {
            "engine": "payment",
            "placed_at": NOW - timedelta(days=1),
            "lifted_at": None,
        },
        {
            "engine": "payment",
            "placed_at": NOW - timedelta(days=40),
            "lifted_at": None,
        },
        {
            "engine": "payment",
            "placed_at": NOW - timedelta(days=2),
            "lifted_at": NOW,
        },
        {"engine": "gst_itc", "placed_at": NOW, "lifted_at": None},
    ]
    r = rd.row(
        "payment",
        mode="advisory",
        since=since,
        now=NOW,
        findings=findings,
        events=events,
        holds=holds,
        t=T,
    )
    assert r["mode"] == "advisory" and r["since"] == since.isoformat()
    assert r["days_in_mode"] == 20
    assert r["findings"] == 4
    assert r["would_have_held"] == 2
    assert r["held"] == 2 and r["open_holds"] == 2
    assert r["triage"] == {
        "decided": 3,
        "accepted": 2,
        "rejected": 1,
        "precision": round(2 / 3, 4),
    }
    assert r["failures"] == 1
    assert r["recommendation"]["to"] is None  # failures and too few decided


def test_a_row_with_no_since_is_day_zero():
    r = rd.row(
        "leakage",
        mode="off",
        since=None,
        now=NOW,
        findings=[],
        events=[],
        holds=[],
        t=T,
    )
    assert r["since"] is None and r["days_in_mode"] == 0
    assert r["triage"]["precision"] is None
    assert r["recommendation"]["to"] == "shadow"


def test_leakage_is_insight_only():
    assert rd.insight_only("leakage")
    assert not rd.insight_only("payment")
    assert not rd.insight_only("reality")


class CountingStore:
    def __init__(self):
        self.calls = []

    def list(self, tenant_id, **kw):
        self.calls.append(("list", kw))
        return []

    def events(self, tenant_id, **kw):
        self.calls.append(("events", kw))
        return []


class Settings:
    def __init__(self, settings):
        self.settings = settings

    def get(self, tenant_id):
        return self.settings


def test_the_report_reads_findings_and_events_once(monkeypatch):
    monkeypatch.setattr(rd, "hold_rows", lambda tenant_id, url=None: [])
    early = NOW - timedelta(days=30)
    store = CountingStore()
    rows = rd.report(
        "t",
        findings=store,
        settings_store=Settings(
            {
                "trust_engine_modes": {
                    "payment": "shadow",
                    "reality": "shadow",
                },
                "trust_engine_mode_since": {
                    "payment": early.isoformat(),
                    "reality": (NOW - timedelta(days=2)).isoformat(),
                },
            }
        ),
        now=NOW,
    )
    assert len(rows) == 5
    assert [c[0] for c in store.calls] == ["list", "events"]
    # An engine never moved through set_engine_mode reads all time.
    assert store.calls[0][1]["since"] is None
