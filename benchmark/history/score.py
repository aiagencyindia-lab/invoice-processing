"""Benchmark gates for the Trust Score (FR-25).

`score_coverage`: the share of invoice nodes with a `trust_score` node; gate
`== 1.0`. `dimension_hold_consistency`: the share of open hold-type findings
on a scored invoice whose dimension reads at most 0.5 there; gate `== 1.0`
(a held invoice never shows that dimension healthy).
"""

from __future__ import annotations

from invoice_processing.trust.score import KIND, score_key
from invoice_processing.trust.score import dimensions as dm

HOLD_CEILING = 0.5


def measure(tenant_id, graph, findings, manifest, url) -> dict:
    invoices = graph.nodes(tenant_id, kind="invoice")
    scores = {
        n["natural_key"]: n["attrs"] for n in graph.nodes(tenant_id, kind=KIND)
    }
    score_of = {
        n["id"]: scores.get(score_key(n["natural_key"])) for n in invoices
    }
    scored = sum(1 for s in score_of.values() if s is not None)
    held = consistent = 0
    for f in findings.list(tenant_id):
        dim = dm.dimension_of(f)
        attrs = score_of.get(f["transaction_node"])
        if not (dm.is_open(f) and dm.is_hold(f) and dim and attrs):
            continue
        held += 1
        value = attrs["dimensions"][dim]["value"]
        consistent += value is not None and value <= HOLD_CEILING
    coverage = round(scored / len(invoices), 4) if invoices else 1.0
    consistency = round(consistent / held, 4) if held else 1.0
    return {
        "score_coverage": coverage,
        "scored": scored,
        "invoices": len(invoices),
        "dimension_hold_consistency": consistency,
        "held_findings": held,
        "gates": {
            "score_coverage": coverage == 1.0,
            "dimension_hold_consistency": consistency == 1.0,
        },
    }
