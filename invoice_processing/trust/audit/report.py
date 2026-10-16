"""The engagement report (FR-29.4): one pure assembler feeds the JSON export
and the printed page. Reads the graph for the top recoveries' evidence,
writes nothing."""

from __future__ import annotations

from decimal import Decimal

from ...api.trust_routes import source_of
from .. import thresholds
from ..findings import model as fm
from . import ledger

TOP = 20
OPEN = ("detected", "triaged", "needs_vendor_input")
ACCEPTED = ("accepted", "claimed", "recovered", "prevented", "written_off")
# What each engine would have stopped, and the mode that stops it. Static:
# the API process never imports the engines, so the registry may be empty.
PREVENTION = (
    (
        "leakage",
        "advisory",
        "leakage.",
        (
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
        ),
    ),
    (
        "payment",
        "enforcing",
        "payment.",
        (
            "PAY-UNVERIFIED",
            "PAY-NAME-MISMATCH",
            "PAY-BEC",
            "PAY-BEC-SIGNAL",
            "PAY-ANOMALY",
        ),
    ),
    ("provenance", "enforcing", "provenance.", ("PROV-TAMPER",)),
    (
        "gst_itc",
        "enforcing",
        "gst.",
        (
            "GST-INACTIVE",
            "GST-UNVERIFIED",
            "GST-NONFILER",
            "GST-NEW-LARGE-FIRST",
            "GST-SHARED-IDENTITY",
            "GST-ITC-RISK",
        ),
    ),
)
_ENGINE_OF = {t: engine for engine, _, _, types in PREVENTION for t in types}


def _impact(f: dict) -> Decimal:
    return Decimal(f["impact"] or 0)


def _engine(f: dict) -> str | None:
    details = f.get("details") or {}
    return (
        details.get("engine")
        or fm.TYPE_ENGINE.get(f["type"])
        or _ENGINE_OF.get(f["type"])
    )


def _is_hold(f: dict) -> bool:
    return bool(
        (f.get("details") or {}).get("hold", f["type"] in fm.HOLD_TYPES)
    )


def _groups(scope: list[dict], rows: list[dict], key) -> list[dict]:
    """count/impact per group, with the ledger's recovered and prevented."""
    out: dict = {}
    moved = {(r["finding_id"], r["to_status"]): r for r in rows}
    for f in scope:
        g = out.setdefault(
            key(f),
            {
                "count": 0,
                "impact": Decimal(0),
                "recovered": Decimal(0),
                "prevented": Decimal(0),
            },
        )
        g["count"] += 1
        g["impact"] += _impact(f)
        for status in ("recovered", "prevented"):
            r = moved.get((f["id"], status))
            if r and r["amount"] is not None:
                g[status] += Decimal(r["amount"])
    return [
        {
            **dict(k),
            "count": g["count"],
            **{
                s: ledger.money(g[s])
                for s in ("impact", "recovered", "prevented")
            },
        }
        for k, g in sorted(
            out.items(), key=lambda kv: (-kv[1]["impact"], str(kv[0]))
        )
    ]


def _label(node: dict) -> str:
    a = node.get("attrs") or {}
    return str(
        a.get("name")
        or a.get("invoice_number")
        or a.get("doc_number")
        or a.get("type")
        or node.get("kind")
    )


def _evidence(graph, tenant_id: str, citations) -> list[dict]:
    out = []
    for c in citations:
        node = graph.node(tenant_id, c["node_id"])
        out.append(
            {
                "node_id": c["node_id"],
                "kind": node["kind"] if node else "unknown",
                "label": _label(node) if node else "missing node",
                "excerpt": c.get("excerpt"),
                "source": source_of(graph, tenant_id, node),
            }
        )
    return out


def assemble(
    audit: dict,
    scope: list[dict],
    events: list[dict],
    *,
    graph,
    run: dict | None,
    now,
    tenant_id: str,
    modes: dict | None = None,
    findings=None,
) -> dict:
    """`scope` rows carry `vendor` ({node, name} or None) already resolved.
    A listed finding carries no citations; `findings` (a FindingStore) fetches
    them for the top recoveries only."""
    vendor_name = {f["id"]: (f.get("vendor") or {}).get("name") for f in scope}
    rows = ledger.rows(vendor_name, events)
    totals = ledger.totals(rows)
    fee = ledger.fee(
        audit.get("terms") or {},
        Decimal(totals["recovered"]),
        Decimal(totals["prevented"]),
    )
    currencies = {f["currency"] for f in scope if f.get("currency")}
    by_type = _groups(
        scope, rows, lambda f: (("type", f["type"]), ("engine", _engine(f)))
    )
    by_vendor = _groups(
        scope,
        rows,
        lambda f: (
            ("vendor", (f.get("vendor") or {}).get("name")),
            ("vendor_node", (f.get("vendor") or {}).get("node") or ""),
        ),
    )
    live = [f for f in scope if f["status"] not in ("rejected", "written_off")]
    live.sort(key=lambda f: (-_impact(f), f["id"]))
    if findings is not None:
        for f in live[:TOP]:
            if not f.get("citations"):
                full = findings.get(tenant_id, f["id"]) or {}
                f["citations"] = full.get("citations") or []
    top = [
        {
            "id": f["id"],
            "type": f["type"],
            "impact": f["impact"],
            "status": f["status"],
            "summary": f.get("summary"),
            "vendor": vendor_name[f["id"]],
            "citations": _evidence(graph, tenant_id, f.get("citations") or ()),
        }
        for f in live[:TOP]
    ]
    modes = modes or {}
    prevention = []
    for engine, suggested, prefix, types in PREVENTION:
        mine = [f for f in scope if f["type"] in types]
        prevention.append(
            {
                "engine": engine,
                "finding_types": list(types),
                "count": len(mine),
                "impact": ledger.money(
                    sum((_impact(f) for f in mine), Decimal(0))
                ),
                "mode_now": modes.get(engine, "off"),
                "mode_suggested": suggested,
                "thresholds_hint": [
                    n for n in thresholds.DEFAULTS if n.startswith(prefix)
                ],
            }
        )
    itc = [f for f in scope if f["type"] == "GST-ITC-RISK"]
    return {
        "report_version": 1,
        "generated_at": now.isoformat(),
        "audit": audit,
        "run": run,
        "summary": {
            "findings": len(scope),
            "detected_impact": ledger.money(
                sum((_impact(f) for f in scope), Decimal(0))
            ),
            "accepted_impact": ledger.money(
                sum(
                    (_impact(f) for f in scope if f["status"] in ACCEPTED),
                    Decimal(0),
                )
            ),
            "recovered": totals["recovered"],
            "prevented": totals["prevented"],
            "written_off": totals["written_off"],
            "at_risk_holds": ledger.money(
                sum(
                    (
                        _impact(f)
                        for f in scope
                        if _is_hold(f) and f["status"] in OPEN
                    ),
                    Decimal(0),
                )
            ),
            "currency": currencies.pop() if len(currencies) == 1 else None,
        },
        "by_type": by_type,
        "by_vendor": by_vendor,
        "top_recoveries": top,
        "prevention": prevention,
        "itc_exposure": {
            "count": len(itc),
            "impact": ledger.money(sum((_impact(f) for f in itc), Decimal(0))),
            "by_vendor": _groups(
                itc,
                rows,
                lambda f: (
                    ("vendor", (f.get("vendor") or {}).get("name")),
                    ("vendor_node", (f.get("vendor") or {}).get("node") or ""),
                ),
            ),
        },
        "ledger": {"rows": rows, "totals": totals, "fee": fee},
    }
