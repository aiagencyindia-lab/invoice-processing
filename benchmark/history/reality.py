"""Benchmark gates for the reality checks (FR-23, AC-23 on the synthetic set).

`reality_coverage_<kind>`: the share of that kind's checked invoices whose
evidence decided the question (CORROBORATED or CONTRADICTED). Gates: goods
>= 0.70, services >= 0.50; a kind with nothing checked has nothing to
measure and passes (None), as eval scores a type nothing was planted for.
Reviewer agreement (AC-23 at a partner) stays a partner gate.

Partly true by construction: the family plants an uncontested invoice's line
from its own evidence, so this measures that the evidence reached the
invoice (GRN -> PO, vendor + month), not the engine's judgment; the
REAL-CONTRADICTED precision and recall gates measure that.
"""

from __future__ import annotations

from collections import Counter, defaultdict

FLOORS = {"goods": 0.70, "services": 0.50}
DECIDED = ("CORROBORATED", "CONTRADICTED")


def measure(tenant_id, graph, findings, manifest, url) -> dict:
    results: dict[str, Counter] = defaultdict(Counter)
    for n in graph.nodes(tenant_id, kind="reality_check"):
        results[n["attrs"].get("kind")][n["attrs"].get("result")] += 1
    out: dict = {"results": {k: dict(v) for k, v in sorted(results.items())}}
    gates = {}
    for kind, floor in FLOORS.items():
        counts = results.get(kind, Counter())
        total = sum(counts.values())
        share = (
            round(sum(counts[r] for r in DECIDED) / total, 4) if total else None
        )
        out[f"reality_coverage_{kind}"] = share
        gates[f"reality_coverage_{kind}"] = share is None or share >= floor
    out["gates"] = gates
    return out
