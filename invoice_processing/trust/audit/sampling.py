"""Which findings an engagement covers, and the sample a controller validates.

Scope is a filter over the tenant's findings (decision 5: no FK): the period
by the transaction's own date, vendors by the open `issued_by` edge, and
categories by keyword over the transaction's lines. An empty list is no
filter.
"""

from __future__ import annotations

import random
from datetime import date


def _date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _vendor_map(graph, tenant_id: str) -> dict[str, str]:
    return {
        e["src"]: e["dst"] for e in graph.edges(tenant_id, kind="issued_by")
    }


def _line_text(graph, tenant_id: str) -> dict[str, str]:
    """transaction node id -> the lower-cased text of its lines."""
    lines = {
        n["id"]: " ".join(
            str(n["attrs"].get(k) or "") for k in ("gl_account", "description")
        ).lower()
        for n in graph.nodes(tenant_id, kind="invoice_line")
    }
    out: dict[str, str] = {}
    for e in graph.edges(tenant_id, kind="part_of"):
        if e["src"] in lines:
            out[e["dst"]] = out.get(e["dst"], "") + " " + lines[e["src"]]
    return out


def scope_findings(
    audit: dict, findings: list[dict], graph, *, tenant_id: str
) -> list[dict]:
    period_from, period_to = (
        _date(audit.get("period_from")),
        _date(audit.get("period_to")),
    )
    vendors = set(audit.get("vendors") or ())
    keywords = [k.lower() for k in audit.get("categories") or () if k.strip()]
    vendor_of = _vendor_map(graph, tenant_id) if vendors else {}
    lines = _line_text(graph, tenant_id) if keywords else {}
    dates: dict[str, date | None] = {}
    if period_from or period_to:
        # ponytail: every node once; index by id in the store if a tenant's
        # graph outgrows memory.
        for n in graph.nodes(tenant_id):
            a = n["attrs"] or {}
            dates[n["id"]] = _date(a.get("invoice_date") or a.get("date"))
    out = []
    for f in findings:
        node = f["transaction_node"]
        if period_from or period_to:
            when = dates.get(node)
            if when is None:
                continue
            if period_from and when < period_from:
                continue
            if period_to and when > period_to:
                continue
        if vendors and vendor_of.get(node) not in vendors:
            continue
        if keywords and not any(k in lines.get(node, "") for k in keywords):
            continue
        out.append(f)
    return out


def draw(scope_ids: list[str], size: int, seed: str) -> list[str]:
    """The same ids and seed always draw the same sample, in any input order."""
    ids = sorted(set(scope_ids))
    if size > len(ids):
        raise ValueError(
            f"The scope has {len(ids)} findings, fewer than {size}."
        )
    return random.Random(seed).sample(ids, size)  # noqa: S311 - reproducibility, not secrecy


def precision(verdicts: dict) -> dict:
    validated = [v for v in verdicts.values() if v is not None]
    true = sum(1 for v in validated if v)
    return {
        "validated": len(validated),
        "true": true,
        "false": len(validated) - true,
        "precision": true / len(validated) if validated else None,
    }
