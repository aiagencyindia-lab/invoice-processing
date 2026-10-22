"""The auditor's evidence pack: one JSON document, sha-stamped, regenerated
on every request (program plan decision 6). Node attrs are already masked
by the graph sync; nothing here adds a raw value."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from ..graph.lineage import lineage
from .engine import build
from .model import EXPLAIN_VERSION

PACK_VERSION = 1
LINEAGE_VERSION = "lineage-1"


# Not hashed: the hash itself, and the clock. Two downloads of an unchanged
# finding carry the same sha (the pack is content-addressed, not a receipt).
UNHASHED = ("sha256", "generated_at")


def canonical_sha256(pack: dict) -> str:
    body = {k: v for k, v in pack.items() if k not in UNHASHED}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def evidence_pack(
    tenant_id: str,
    finding: dict,
    *,
    graph,
    findings=None,
    ratings=(),
    links=None,
    now: datetime | None = None,
) -> dict:
    if "citations" not in finding and findings is not None:
        finding = findings.get(tenant_id, finding["id"]) or finding
    now = now or datetime.now(UTC)
    explanation = build(tenant_id, finding, graph=graph, links=links)
    ids = list(
        dict.fromkeys(c["node_id"] for c in finding.get("citations", []))
    )
    nodes = [n for i in ids if (n := graph.node(tenant_id, i)) is not None]
    edges = [
        e for i in ids for e in graph.edges(tenant_id, src=i) if e["dst"] in ids
    ]
    try:
        chain = [
            link.__dict__
            for link in lineage(
                tenant_id, finding["transaction_node"], graph=graph, links=links
            )
        ]
    except KeyError:
        chain = []
    pack = {
        "pack_version": PACK_VERSION,
        "generated_at": now.isoformat(),
        "finding": finding,
        "explanation": explanation.as_dict(),
        "lineage": chain,
        "nodes": nodes,
        "edges": edges,
        "versions": {
            **explanation.versions,
            "explain_version": EXPLAIN_VERSION,
            "lineage_version": LINEAGE_VERSION,
        },
        "decision": {
            "status": finding.get("status"),
            "owner_id": finding.get("owner_id"),
            "decided_at": finding.get("decided_at"),
            "events": list(finding.get("events") or []),
        },
        "ratings": list(ratings),
    }
    pack["sha256"] = canonical_sha256(pack)
    return pack
