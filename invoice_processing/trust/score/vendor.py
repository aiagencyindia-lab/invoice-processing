"""A vendor's trust profile, computed on read from its invoices' score nodes,
its findings, the provider evidence they cite and the graph around it."""

from __future__ import annotations

from collections import defaultdict
from statistics import fmean

from sqlalchemy import select

from ...db.models import TrustFindingCitation
from ...db.session import tenant_session
from ..findings.model import DECIDED
from ..types import DIMENSIONS
from . import KIND, score_key

RECENT_SCORES = 50
UNDECIDED = frozenset({"detected", "triaged"})


def _cited(tenant_id: str, finding_ids: list[str], url) -> set[str]:
    if not finding_ids:
        return set()
    with tenant_session(tenant_id, url) as s:
        return set(
            s.scalars(
                select(TrustFindingCitation.node_id).where(
                    TrustFindingCitation.tenant_id == tenant_id,
                    TrustFindingCitation.finding_id.in_(finding_ids),
                )
            )
        )


def _latest_evidence(graph, tenant_id, capability, cited, match=None):
    """The newest provider evidence of `capability` this vendor's findings
    cite, or whose answer names the vendor (`match(data)`)."""
    best = None
    for n in graph.nodes(tenant_id, kind="provider_evidence"):
        a = n["attrs"]
        if a.get("capability") != capability:
            continue
        if n["id"] not in cited and not (match and match(a.get("data") or {})):
            continue
        if best is None or (a.get("observed_at") or "") > (
            best["attrs"].get("observed_at") or ""
        ):
            best = n
    return best


def _gst(graph, tenant_id, vendor, cited) -> dict | None:
    gstin = (vendor.get("natural_keys") or {}).get("gstin")
    ev = _latest_evidence(
        graph,
        tenant_id,
        "gstin",
        cited,
        match=lambda d: (
            bool(gstin) and str(d.get("gstin", "")).upper() == gstin
        ),
    )
    if ev is None:
        return None
    a = ev["attrs"]
    data = a.get("data") or {}
    return {
        "status": data.get("status")
        if a.get("status") == "ok"
        else "unavailable",
        "missed_periods": data.get("missed_periods"),
        "observed_at": a.get("observed_at"),
    }


def _bank_verified(graph, tenant_id, vendor, cited) -> bool | None:
    if vendor["attrs"].get("bank_verified_at"):
        return True
    ev = _latest_evidence(graph, tenant_id, "bank_verification", cited)
    if ev is None:
        return None
    a = ev["attrs"]
    return a.get("status") == "ok" and (a.get("data") or {}).get("result") == (
        "verified"
    )


def _sources(graph, tenant_id, vendor_id, edge: str, kind: str) -> list[dict]:
    """The open `kind` nodes with an `edge` edge into the vendor: one read
    of the kind, not one per edge.
    ponytail: reads every node of the kind; a natural-key or id filter in
    GraphStore if a tenant passes ~50k of them."""
    srcs = {e["src"] for e in graph.edges(tenant_id, dst=vendor_id, kind=edge)}
    if not srcs:
        return []
    return [n for n in graph.nodes(tenant_id, kind=kind) if n["id"] in srcs]


def _kyc(graph, tenant_id, vendor_id) -> list[dict]:
    out = []
    for n in _sources(
        graph, tenant_id, vendor_id, "evidences", "document_version"
    ):
        a = n["attrs"]
        out.append(
            {
                "node": n["id"],
                "document_id": n["ref_id"],
                "subkind": a.get("subkind"),
                "score": a.get("score"),
                "observed_at": n["valid_from"],
            }
        )
    return sorted(out, key=lambda r: r["observed_at"] or "", reverse=True)


def _network(graph, links, tenant_id, vendor) -> list[dict]:
    """Other vendors sharing a statutory id, bank account or domain (named
    by kind, never by value), and the entity links that join them."""
    mine = vendor.get("natural_keys") or {}
    shared: dict[str, set] = defaultdict(set)
    names = {}
    for other in graph.nodes(tenant_id, kind="vendor"):
        if other["id"] == vendor["id"]:
            continue
        names[other["id"]] = other["attrs"].get("name")
        theirs = other.get("natural_keys") or {}
        for k, v in mine.items():
            if k != "name" and v and theirs.get(k) == v:
                shared[other["id"]].add(k)
    for link in links.open_links(tenant_id) if links else []:
        if link["status"] == "rejected":  # a person ruled them apart
            continue
        ends = {link["node_a"], link["node_b"]}
        if vendor["id"] in ends and len(ends) == 2:
            (other,) = ends - {vendor["id"]}
            if other in names:
                shared[other].add(f"link:{link['status']}")
    return [
        {"vendor": {"node": n, "name": names.get(n)}, "shared": sorted(keys)}
        for n, keys in sorted(shared.items())
    ]


def profile(
    tenant_id: str, vendor_id: str, *, graph, findings, links=None, url=None
) -> dict | None:
    vendor = graph.node(tenant_id, vendor_id)
    if vendor is None or vendor["kind"] != "vendor":
        return None
    invoices = _sources(graph, tenant_id, vendor_id, "issued_by", "invoice")
    scores_by_key = {
        n["natural_key"]: n for n in graph.nodes(tenant_id, kind=KIND)
    }
    scored = [
        (inv, scores_by_key[score_key(inv["natural_key"])]["attrs"])
        for inv in invoices
        if score_key(inv["natural_key"]) in scores_by_key
    ]
    recent = sorted(scored, key=lambda p: p[1].get("computed_at") or "")[
        -RECENT_SCORES:
    ]
    dims = {}
    for d in DIMENSIONS:
        values = [
            a["dimensions"][d]["value"]
            for _, a in recent
            if (a.get("dimensions") or {}).get(d, {}).get("value") is not None
        ]
        dims[d] = round(fmean(values), 4) if values else None
    by_type: dict[str, dict] = defaultdict(lambda: {"open": 0, "decided": 0})
    rows = findings.list(tenant_id, vendor_node=vendor_id)
    for f in rows:
        if f["status"] in UNDECIDED:
            by_type[f["type"]]["open"] += 1
        elif f["status"] in DECIDED:
            by_type[f["type"]]["decided"] += 1
    cited = _cited(tenant_id, [f["id"] for f in rows], url)
    months: dict[str, list] = defaultdict(list)
    for inv, a in scored:
        month = (
            inv["attrs"].get("invoice_date") or a.get("computed_at") or ""
        )[:7]
        if month and a.get("composite") is not None:
            months[month].append(a["composite"])
    return {
        "vendor": {
            "node": vendor["id"],
            "name": vendor["attrs"].get("name"),
            "invoices": len(invoices),
            "scored": len(scored),
        },
        "dimensions": dims,
        "findings_by_type": dict(sorted(by_type.items())),
        "gst": _gst(graph, tenant_id, vendor, cited),
        "bank_verified": _bank_verified(graph, tenant_id, vendor, cited),
        "kyc": _kyc(graph, tenant_id, vendor_id),
        "network": _network(graph, links, tenant_id, vendor),
        "timeline": [
            {"month": m, "composite": round(fmean(v), 4)}
            for m, v in sorted(months.items())
        ],
    }
