"""The tenant's trust posture: what was found, stopped, recovered and is
still open, with the money from the same ledger the audit report uses (so the
two always agree), plus hold, provenance and verification coverage."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from ..audit import ledger
from ..findings import model as finding_model
from . import KIND
from .readiness import hold_rows

OPEN = ("detected", "triaged", "needs_vendor_input")
TOP_VENDORS = 20
BUCKETS = ("0.0-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-1.0")
COVERED = ("identity", "reality", "authenticity")


def _impact(f: dict) -> Decimal:
    return Decimal(f["impact"]) if f.get("impact") else Decimal(0)


def _bucket(score: float) -> str:
    return BUCKETS[min(int(float(score) * 5), 4)]


def summary(
    tenant_id: str,
    *,
    graph,
    findings,
    url: str | None = None,
    since: datetime | None = None,
) -> dict:
    """`since` bounds what was found and what moved; open exposure, open
    holds, provenance and coverage are the state now.
    ponytail: money is summed across currencies; split by currency when a
    tenant pays in more than one."""
    rows = [
        f
        for f in findings.list(tenant_id, since=since)
        if f["type"] != finding_model.ENGINE_FAILED
    ]
    events = findings.events(tenant_id, since=since)
    totals = ledger.totals(
        ledger.rows({e["finding_id"] for e in events}, events)
    )
    vendor_of = {
        e["src"]: e["dst"] for e in graph.edges(tenant_id, kind="issued_by")
    }
    by_type: dict = defaultdict(lambda: {"count": 0, "impact": Decimal(0)})
    by_vendor: dict = defaultdict(lambda: {"count": 0, "impact": Decimal(0)})
    for f in rows:
        for group in (
            by_type[f["type"]],
            by_vendor[vendor_of.get(f["transaction_node"])],
        ):
            group["count"] += 1
            group["impact"] += _impact(f)
    by_vendor.pop(None, None)
    names = {
        n["id"]: n["attrs"].get("name")
        for n in graph.nodes(tenant_id, kind="vendor")
    }
    top = sorted(
        by_vendor.items(), key=lambda kv: (-kv[1]["impact"], -kv[1]["count"])
    )[:TOP_VENDORS]
    prevented = Decimal(totals["prevented"])
    recovered = Decimal(totals["recovered"])
    stopped = prevented + recovered
    open_rows = findings.list(tenant_id, statuses=OPEN)
    distribution = dict.fromkeys(BUCKETS, 0)
    for n in graph.nodes(tenant_id, kind="document_version"):
        score = n["attrs"].get("score")
        if score is not None and n["attrs"].get("kind") not in ("kyc", "proof"):
            distribution[_bucket(score)] += 1
    scores = [n["attrs"] for n in graph.nodes(tenant_id, kind=KIND)]
    coverage = {
        d: round(
            sum(
                1
                for a in scores
                if (a.get("dimensions") or {}).get(d, {}).get("value")
                is not None
            )
            / len(scores),
            4,
        )
        if scores
        else None
        for d in COVERED
    }
    holds = hold_rows(tenant_id, url)
    return {
        "since": since.isoformat() if since else None,
        "found": {
            "count": len(rows),
            "impact": ledger.money(sum((_impact(f) for f in rows), Decimal(0))),
        },
        "prevented": totals["prevented"],
        "recovered": totals["recovered"],
        "written_off": totals["written_off"],
        "open_exposure": ledger.money(
            sum((_impact(f) for f in open_rows), Decimal(0))
        ),
        "by_type": {
            t: {"count": g["count"], "impact": ledger.money(g["impact"])}
            for t, g in sorted(by_type.items())
        },
        "by_vendor": [
            {
                "node": n,
                "name": names.get(n),
                "count": g["count"],
                "impact": ledger.money(g["impact"]),
            }
            for n, g in top
        ],
        # Stopped before payment, of all the money stopped or clawed back.
        "pre_payment_share": round(float(prevented / stopped), 4)
        if stopped
        else None,
        "provenance_distribution": distribution,
        "verification_coverage": coverage,
        "holds": {
            "open": sum(1 for h in holds if h["lifted_at"] is None),
            "placed": sum(
                1 for h in holds if since is None or h["placed_at"] >= since
            ),
            "lifted": sum(
                1
                for h in holds
                if h["lifted_at"] is not None
                and (since is None or h["lifted_at"] >= since)
            ),
        },
    }
