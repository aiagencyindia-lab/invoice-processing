"""Build a cited explanation for one finding. No model anywhere here."""

from __future__ import annotations

from ..graph.locate import source_of, vendor_of
from .counterfactual import counterfactual
from .model import EXPLAIN_VERSION, Evidence, Explanation
from .templates import TEMPLATES, Context, generic

_LABELS = {
    "invoice": ("Invoice", "invoice_number"),
    "invoice_line": ("Invoice line", "description"),
    "payment": ("Payment", "doc_number"),
    "vendor_statement_line": ("Statement line", "doc_number"),
    "contract": ("Contract", "filename"),
    "contract_clause": ("Clause", "type"),
    "vendor": ("Vendor", "name"),
    "provider_evidence": ("Provider evidence", "capability"),
    "document_version": ("Document version", "case_id"),
    "bank_change": ("Bank-detail change", "changed_at"),
    "po": ("Purchase order", "doc_number"),
    "grn": ("Goods receipt", "doc_number"),
    "approval": ("Approval", "decided_by"),
}


def _label(node: dict | None) -> tuple[str, str]:
    if node is None:
        return "missing", "Node no longer in the graph"
    kind = node.get("kind") or "node"
    name, key = _LABELS.get(kind, (kind.replace("_", " ").capitalize(), None))
    attrs = node.get("attrs") or {}
    value = attrs.get(key) if key else None
    if kind == "contract_clause":
        page = attrs.get("page")
        value = f"{value or 'clause'}" + (f", page {page}" if page else "")
    return kind, f"{name} {value}" if value else name


def _confidence_note(finding: dict) -> str:
    c = finding.get("confidence")
    details = finding.get("details") or {}
    if c is None:
        note = "The engine gave no confidence for this finding."
    elif c >= 0.9:
        note = "High: a rule on recorded data."
    elif c >= 0.6:
        note = "Moderate: a rule on data that may be incomplete."
    else:
        note = "Low: a heuristic signal; confirm before acting."
    if details.get("needs_review"):
        note += " A cited clause was read by a model and is not yet reviewed."
    return note


def build(tenant_id: str, finding: dict, *, graph, links=None) -> Explanation:
    """`links` is accepted for parity with the other engines; the
    explanation reads only what the finding cited."""
    txn = finding["transaction_node"]
    cites = finding.get("citations") or []
    ids = list(dict.fromkeys([c["node_id"] for c in cites] + [txn]))
    nodes = {i: graph.node(tenant_id, i) for i in ids}
    approvers = tuple(
        who
        for e in graph.edges(tenant_id, dst=txn, kind="approves")
        if (
            who := (
                (graph.node(tenant_id, e["src"]) or {}).get("attrs") or {}
            ).get("decided_by")
        )
    )
    ctx = Context(finding, nodes, vendor_of(graph, tenant_id, txn), approvers)
    template = TEMPLATES.get(finding["type"], generic.generic)
    draft = {**generic.generic(ctx), **template(ctx)}
    evidence = []
    for node_id in ctx.cited:
        kind, label = _label(nodes.get(node_id))
        evidence.append(
            Evidence(
                node_id,
                kind,
                label,
                ctx.excerpt(node_id) or (None if nodes.get(node_id) else label),
                source_of(graph, tenant_id, nodes.get(node_id)),
            )
        )
    out = Explanation(
        finding_id=finding["id"],
        type=finding["type"],
        version=EXPLAIN_VERSION,
        what_happened=draft["what_happened"],
        why=tuple(draft["why"]),
        caused_by=tuple(draft["caused_by"]),
        evidence=tuple(evidence),
        confidence=finding.get("confidence"),
        confidence_note=draft.get("confidence_note")
        or _confidence_note(finding),
        next_action=draft["next_action"],
        would_change=tuple(draft.get("would_change") or ()),
        counterfactual=counterfactual(finding, nodes.get(txn)),
        versions={
            "explain_version": EXPLAIN_VERSION,
            "engine_version": finding.get("engine_version") or "",
            "rule_version": finding.get("rule_version") or "",
            "model_version": finding.get("model_version") or "",
        },
        transaction_node=txn,
    )
    out.check(ctx.cited)
    return out
