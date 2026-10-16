"""Benchmark gates for the Why engine (FR-24, AC-24).

`explanation_citation_coverage`: the share of findings in the eval database
whose explanation builds and passes `check()` with at least one piece of
evidence; gate `== 1.0`. `counterfactual_impact_error`: the money the
counterfactual says is at stake against the manifest's priced faults,
matched by type and document number exactly as `eval._score` does; gate
`<= 0.01`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal

from invoice_processing.trust.explain.engine import build

logger = logging.getLogger(__name__)
MAX_IMPACT_ERROR = 0.01


def measure(tenant_id, graph, findings, manifest, url) -> dict:
    expected: dict[str, dict] = defaultdict(dict)
    for fault in manifest.get("faults", []):
        if fault.get("expected_impact") not in (None, ""):
            expected[fault["type"]][fault["doc_number"]] = Decimal(
                str(fault["expected_impact"])
            )
    doc_of: dict[str, str | None] = {}

    def doc(node_id: str) -> str | None:
        if node_id not in doc_of:
            a = (graph.node(tenant_id, node_id) or {"attrs": {}})["attrs"]
            doc_of[node_id] = a.get("doc_number") or a.get("invoice_number")
        return doc_of[node_id]

    total = cited = 0
    num = den = Decimal(0)
    priced = 0
    for row in findings.list(tenant_id):
        f = findings.get(tenant_id, row["id"]) or row
        total += 1
        try:
            x = build(tenant_id, f, graph=graph)
        except Exception as exc:  # Uncited, or a template crash: both miss
            logger.warning(
                "explain_benchmark_miss",
                extra={"type": f["type"], "error": type(exc).__name__},
            )
            continue
        cited += 1
        want = expected.get(f["type"], {}).get(doc(f["transaction_node"]))
        delta = (x.counterfactual or None) and x.counterfactual.delta
        if want is None or delta is None:
            continue
        priced += 1
        num += abs(Decimal(delta) - want)
        den += want
    coverage = round(cited / total, 4) if total else 1.0
    error = round(float(num / den), 4) if den else 0.0
    return {
        "explanation_citation_coverage": coverage,
        "explained": cited,
        "findings": total,
        "counterfactual_impact_error": error,
        "priced": priced,
        "gates": {
            "explanation_citation_coverage": coverage == 1.0,
            "counterfactual_impact_error": error <= MAX_IMPACT_ERROR,
        },
    }
