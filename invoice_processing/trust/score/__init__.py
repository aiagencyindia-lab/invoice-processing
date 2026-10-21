"""The Transaction Trust Score (FR-25): six dimensions per invoice, each one
able to drive a hold, a composite that is only advisory.

`evaluate()` calls `compute` after the engines ran: a per-document run scores
its invoice, a batch run every invoice, a contract run none. The score is a
graph node (`trust_score:{invoice natural key}`), so `upsert_node` versions it
and `?as_of` reads what was known then; the gate never reads it (B4).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from ...db.models import Invoice
from ...db.session import tenant_session
from ..modes import ENGINES
from . import dimensions as dm

SCORE_VERSION = "score-1"
KIND = "trust_score"


def score_key(invoice_natural_key: str) -> str:
    return f"{KIND}:{invoice_natural_key}"


def reality_key(invoice_natural_key: str) -> str:
    return f"reality:{invoice_natural_key}"


def score_node(
    graph, tenant_id: str, invoice: dict, as_of: datetime | None = None
) -> dict | None:
    key = score_key(invoice["natural_key"])
    if as_of is None:
        return graph.open_node(tenant_id, KIND, key)
    # ponytail: reads every score node valid then; a natural-key filter in
    # GraphStore if a tenant passes ~50k invoices.
    for n in graph.nodes(tenant_id, kind=KIND, as_of=as_of):
        if n["natural_key"] == key:
            return n
    return None


def _amount(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


class _World:
    """Everything the rules read for the invoices being scored: one query
    per kind for a batch, filtered queries for one invoice."""

    def __init__(self, ectx, invoices: list[dict]):
        g, t = ectx.graph, ectx.ctx.tenant_id
        self.graph, self.tenant = g, t
        one = invoices[0]["id"] if len(invoices) == 1 else None
        self.findings = defaultdict(list)
        for f in ectx.findings.list(t, transaction_node=one):
            self.findings[f["transaction_node"]].append(f)
        self.vendor = {
            e["src"]: e["dst"] for e in g.edges(t, src=one, kind="issued_by")
        }
        vendors = set(self.vendor.values())
        # Another invoice from the same vendor is the price baseline; its
        # purchase orders (also `issued_by`) are not.
        if one:
            invoice_ids = {x.node_id for x in ectx.transactions()} | {one}
        else:
            invoice_ids = {n["id"] for n in invoices}
        self.issued = defaultdict(set)
        for v in vendors if one else [None]:
            for e in g.edges(t, dst=v, kind="issued_by"):
                if e["src"] in invoice_ids:
                    self.issued[e["dst"]].add(e["src"])
        self.governed = {
            e["src"] for e in g.edges(t, src=one, kind="governed_by")
        }
        self.governs = set()
        for v in vendors if one else [None]:
            self.governs |= {
                e["dst"] for e in g.edges(t, dst=v, kind="governs")
            }
        self.approvals = defaultdict(list)
        edges = g.edges(t, dst=one, kind="approves")
        if one:
            nodes = {e["src"]: g.node(t, e["src"]) for e in edges}
        else:
            nodes = {n["id"]: n for n in g.nodes(t, kind="approval")}
        for e in edges:
            if nodes.get(e["src"]):
                self.approvals[e["dst"]].append(nodes[e["src"]]["attrs"])
        self.reality = self._by_key(ectx, "reality_check", invoices, one)
        self.documents = self._by_key(ectx, "document_version", invoices, one)
        self.scores = self._by_key(ectx, KIND, invoices, one)
        q = select(Invoice.id, Invoice.fields).where(Invoice.tenant_id == t)
        if one:
            q = q.where(Invoice.id == invoices[0]["ref_id"])
        # A batch reads the tenant's invoices whole: an IN list of every id
        # passes SQLite's bound-variable cap on a large tenant.
        with tenant_session(t, ectx.url) as s:
            rows = s.execute(q).all()
        self.workflow = {
            i: (fields or {}).get("workflow") or {} for i, fields in rows
        }

    def _by_key(self, ectx, kind, invoices, one) -> dict:
        g, t = self.graph, self.tenant
        if one:
            key = _key_for(kind, invoices[0])
            node = g.open_node(t, kind, key) if key else None
            return {key: node} if node else {}
        return {n["natural_key"]: n for n in g.nodes(t, kind=kind)}


def _key_for(kind: str, invoice: dict) -> str | None:
    if kind == KIND:
        return score_key(invoice["natural_key"])
    if kind == "reality_check":
        return reality_key(invoice["natural_key"])
    a = invoice["attrs"]
    if not a.get("document_id"):
        return None
    return f"documents:{a['document_id']}:{a.get('document_sha256')}"


def _inputs(world: _World, invoice: dict) -> dm.Inputs:
    iid = invoice["id"]
    live = invoice["ref_table"] == "invoices"
    workflow = world.workflow.get(invoice["ref_id"], {}) if live else {}
    reality = world.reality.get(_key_for("reality_check", invoice))
    document = world.documents.get(_key_for("document_version", invoice))
    vendor = world.vendor.get(iid)
    return dm.Inputs(
        findings=world.findings.get(iid, []),
        amount=_amount(invoice["attrs"].get("amount")),
        approvals=world.approvals.get(iid, []),
        decision=workflow.get("decision") or invoice["attrs"].get("decision"),
        overridden=bool(workflow.get("hold_overridden")),
        live=live,
        reality=reality["attrs"] if reality else None,
        document=document["attrs"] if document else None,
        has_contract=iid in world.governed
        or (vendor is not None and vendor in world.governs),
        has_history=bool(world.issued.get(vendor, set()) - {iid}),
    )


def _attrs(world, invoice, modes, inputs, now) -> dict:
    enforcing = {e for e, m in modes.items() if m == "enforcing"}
    running = {e for e, m in modes.items() if m != "off"}
    dims = dm.dimensions(inputs, enforcing=enforcing, running=running)
    engines, rules = {}, {}
    for f in inputs.findings:
        engine = dm.engine_of(f)
        if engine and f.get("engine_version"):
            engines[engine] = f["engine_version"]
        if f.get("rule_version"):
            rules[f["type"]] = f["rule_version"]
    reality = world.reality.get(_key_for("reality_check", invoice))
    document = world.documents.get(_key_for("document_version", invoice))
    return {
        "score_version": SCORE_VERSION,
        "computed_at": now.isoformat(),
        "composite": dm.composite(dims),
        "dimensions": dims,
        "versions": {
            "engines": dict(sorted(engines.items())),
            "provenance": document["id"] if document else None,
            "reality": reality["id"] if reality else None,
            "rules": dict(sorted(rules.items())),
        },
        "holds": dm.holds(inputs.findings, enforcing),
        "mode": modes,
    }


def _same(a: dict, b: dict) -> bool:
    return {**a, "computed_at": None} == {**b, "computed_at": None}


def compute(ectx, report: dict) -> None:
    """Write each scored invoice's `trust_score` node. Unchanged inputs
    write nothing new: the open node keeps its id and its `computed_at`."""
    graph, tenant = ectx.graph, ectx.ctx.tenant_id
    if ectx.invoice_node is not None:
        node = graph.node(tenant, ectx.invoice_node)
        invoices = [node] if node else []
    elif ectx.document is None:
        invoices = graph.nodes(tenant, kind="invoice")
    else:
        return  # ponytail: a contract run re-scores nothing until its invoices run
    if not invoices:
        return
    setting = ectx.settings.get("trust_engine_modes") or {}
    modes = {e: setting.get(e, "off") for e in ENGINES}
    world = _World(ectx, invoices)
    for invoice in invoices:
        attrs = _attrs(world, invoice, modes, _inputs(world, invoice), ectx.now)
        prior = world.scores.get(score_key(invoice["natural_key"]))
        if prior and _same(prior["attrs"], attrs):
            continue
        graph.upsert_node(
            ectx.ctx,
            kind=KIND,
            natural_key=score_key(invoice["natural_key"]),
            attrs=attrs,
            at=ectx.now,
        )


def with_modes(attrs: dict, findings: list[dict], modes: dict) -> dict:
    """The stored score with its holds re-read against today's modes: a mode
    change re-scores nothing, and an engine promoted since the last run
    holds now. Values are as of the last run."""
    enforcing = {e for e, m in modes.items() if m == "enforcing"}
    by_id = {f["id"]: f for f in findings if dm.is_open(f)}
    dims = {
        name: {
            **d,
            "hold": any(
                dm.is_hold(by_id[i]) and dm.engine_of(by_id[i]) in enforcing
                for i in d.get("drivers", ())
                if i in by_id
            ),
        }
        for name, d in (attrs.get("dimensions") or {}).items()
    }
    return {
        **attrs,
        "dimensions": dims,
        "holds": dm.holds(findings, enforcing),
        "mode": {e: modes.get(e, "off") for e in ENGINES},
    }
