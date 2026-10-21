"""Provenance v1 (FR-22 part): where a document came from, and whether an
approved invoice came back changed.

Per invoice document: read the PDF's facts, score its authenticity
deterministically, and record a `document_version` node (the provenance
ledger) that `evidences` the invoice. For every invoice in scope: a
`PROV-TAMPER` hold when the same vendor's invoice with the same number was
approved with different document bytes. A batch run (no document) checks
tamper only; the ledger needs a document's bytes. No model, no providers.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ...duplicates import normalize_number
from ..engine import EngineContext
from ..findings.model import Citation, FindingSpec, register_type
from . import forensics, pdfmeta

ENGINE_VERSION = "provenance-2"
RULE_VERSION = "FR-22-v2"
SUSPECT = "PROV-SUSPECT"
TAMPER = "PROV-TAMPER"
EDITED = "PROV-EDITED"
register_type(
    SUSPECT,
    "Low authenticity score: channel, template, text layer or metadata",
    engine="provenance",
)
register_type(
    TAMPER,
    "An approved invoice re-submitted with different document bytes",
    engine="provenance",
    hold=True,
)
register_type(
    EDITED,
    "The invoice document was edited and re-saved after it was issued",
    engine="provenance",
)

logger = logging.getLogger(__name__)

# Email from the vendor's own domain scores 1.0; email from anywhere else
# counts as "other".
CHANNEL = {"api": 0.8, "upload": 0.6}
OTHER = 0.3
WEIGHTS = {
    "channel": 0.3,
    "template": 0.2,
    "text": 0.1,
    "meta": 0.1,
    "producer": 0.15,
    "edits": 0.1,
    "fonts": 0.05,
}


class _Tenant:
    """One read of the invoices, vendors and links a run needs."""

    def __init__(self, ectx: EngineContext):
        t = ectx.ctx.tenant_id
        g = ectx.graph
        self.invoices = {n["id"]: n for n in g.nodes(t, kind="invoice")}
        self.vendor_nodes = {n["id"]: n for n in g.nodes(t, kind="vendor")}
        canonical = ectx.links.canonical_map(t)
        # invoice -> (the vendor node it names, that vendor's canonical id)
        self.issuer: dict[str, tuple[str, str]] = {}
        for e in g.edges(t, kind="issued_by"):
            if e["src"] in self.invoices:
                self.issuer[e["src"]] = (
                    e["dst"],
                    canonical.get(e["dst"], e["dst"]),
                )
        status = {
            n["id"]: n["attrs"].get("status")
            for n in g.nodes(t, kind="approval")
        }
        self.approved = {
            i
            for i, n in self.invoices.items()
            if n["attrs"].get("decision") == "approve"
        } | {
            e["dst"]
            for e in g.edges(t, kind="approves")
            if status.get(e["src"]) == "approved"
        }

    def vendor(self, invoice: str) -> str | None:
        return (self.issuer.get(invoice) or (None, None))[1]

    def vendor_domains(self, invoice: str) -> set[str]:
        domains = set()
        for node in self.issuer.get(invoice) or ():
            d = (self.vendor_nodes.get(node) or {"attrs": {}})["attrs"].get(
                "email_domain"
            )
            if d:
                domains.add(str(d).strip().lower())
        return domains


def _channel(ectx, tenant, attrs) -> float:
    source = (ectx.document or {}).get("source")
    if source == "email":
        sender = (attrs.get("document_sender_domain") or "").strip().lower()
        return (
            1.0
            if sender and sender in tenant.vendor_domains(ectx.invoice_node)
            else OTHER
        )
    return CHANNEL.get(source, OTHER)


def _meta(facts: dict, now: datetime) -> float:
    if not facts["producer"] or not facts["creation_date"]:
        return 0.0
    created = datetime.fromisoformat(facts["creation_date"])
    # No ModDate: the file records no modification, so it is its creation.
    modified = datetime.fromisoformat(
        facts["mod_date"] or facts["creation_date"]
    )
    return 1.0 if created <= modified <= now else 0.0


def _prior(ectx, tenant, doc_id: str) -> list[dict]:
    """The vendor's other documents' version attrs. Found through the
    invoice's `document_id` attr: a sync that re-versions an invoice node
    closes its `evidences` edge until that document is evaluated again."""
    vendor = tenant.vendor(ectx.invoice_node)
    invoice_of_doc = {
        n["attrs"].get("document_id"): i for i, n in tenant.invoices.items()
    }
    # ponytail: scans every version node per document; index by vendor when a
    # tenant passes ~50k documents.
    out = []
    for v in ectx.graph.nodes(ectx.ctx.tenant_id, kind="document_version"):
        other = v["natural_key"].split(":")[1]
        inv = invoice_of_doc.get(other)
        if other != doc_id and inv and tenant.vendor(inv) == vendor:
            out.append(v["attrs"])
    return out


def _edited(ectx, doc, vid, facts, attrs) -> FindingSpec | None:
    """Saved on top of itself at least `provenance.edits_suspect` times,
    unless the last save is dated on or before the invoice's own date (a
    generator's or signer's saves on the day of issue)."""
    saves = facts["incremental_updates"]
    if saves < int(ectx.threshold("provenance.edits_suspect")):
        return None
    issued, modified = attrs.get("invoice_date"), facts["mod_date"]
    if issued and modified:
        try:
            on_issue = datetime.fromisoformat(modified).date() <= (
                date.fromisoformat(str(issued)[:10])
            )
        except ValueError:
            on_issue = False
        if on_issue:
            return None
    return FindingSpec(
        type=EDITED,
        transaction_node=ectx.invoice_node,
        dedupe_key=f"{EDITED}:{doc['id']}",
        impact=None,
        currency=None,
        confidence=0.9,
        engine_version=ENGINE_VERSION,
        rule_version=RULE_VERSION,
        model_version="none",
        summary=(
            f"The invoice document was saved on top of itself {saves} times"
            + (" after its issue date." if issued and modified else ".")
        ),
        details={
            "incremental_updates": saves,
            "mod_date": modified,
            "invoice_date": issued,
            "producer": facts["producer"],
        },
        citations=(Citation(vid, f"{saves} incremental saves"),),
    )


def _ledger(ectx: EngineContext, tenant: _Tenant) -> list[FindingSpec]:
    doc = ectx.document
    facts = pdfmeta.facts(ectx.blobs.get(doc["storage_key"]))
    if facts is None:
        logger.info("provenance_skipped", extra={"reason": "not_pdf"})
        return []
    attrs = tenant.invoices[ectx.invoice_node]["attrs"]
    fp = pdfmeta.fingerprint(facts)
    prior = _prior(ectx, tenant, doc["id"])
    matching = sum(1 for p in prior if p.get("fingerprint") == fp)
    min_history = ectx.threshold("provenance.template_min_history")
    components = {
        "channel": _channel(ectx, tenant, attrs),
        "template": 1.0
        if len(prior) < min_history
        else round(matching / len(prior), 4),
        "text": 1.0 if facts["has_text"] else 0.0,
        "meta": _meta(facts, ectx.now),
        "producer": forensics.producer(
            facts, [p.get("producer") for p in prior]
        ),
        "edits": forensics.edits(
            facts, int(ectx.threshold("provenance.edits_suspect"))
        ),
        "fonts": forensics.fonts(facts),
    }
    score = round(sum(WEIGHTS[k] * v for k, v in components.items()), 4)
    key = f"documents:{doc['id']}:{doc['sha256']}"
    vid = ectx.graph.upsert_node(
        ectx.ctx,
        kind="document_version",
        natural_key=key,
        ref_table="documents",
        ref_id=doc["id"],
        attrs={
            "sha256": doc["sha256"],
            "source": doc.get("source"),
            "sender_domain": attrs.get("document_sender_domain"),
            **{
                k: facts[k]
                for k in (
                    "producer",
                    "creator",
                    "creation_date",
                    "mod_date",
                    "page_count",
                    "has_text",
                    "incremental_updates",
                    "font_names",
                )
            },
            "engine_version": ENGINE_VERSION,
            "fingerprint": fp,
            "channel": doc.get("source"),
            "template": {"prior": len(prior), "matching": matching},
            "score": score,
            "components": components,
            "actor": ectx.ctx.actor,
        },
    )
    ectx.graph.upsert_edge(
        ectx.ctx,
        src=vid,
        dst=ectx.invoice_node,
        kind="evidences",
        source_system="provenance",
        provenance="system_record",
        confidence=1.0,
        observed_at=ectx.now,
    )
    specs = [e for e in [_edited(ectx, doc, vid, facts, attrs)] if e]
    threshold = ectx.threshold("provenance.suspect_below")
    if score >= threshold:
        return specs
    return [
        *specs,
        FindingSpec(
            type=SUSPECT,
            transaction_node=ectx.invoice_node,
            dedupe_key=f"{SUSPECT}:{doc['id']}",
            impact=None,
            currency=None,
            confidence=round(1 - score, 4),
            engine_version=ENGINE_VERSION,
            rule_version=RULE_VERSION,
            model_version="none",
            summary=f"Document authenticity score {score:.2f} is below {threshold:.2f}.",
            details={
                "score": score,
                "components": components,
                "fingerprint": fp[:16],
            },
            citations=(Citation(vid, f"authenticity score {score:.2f}"),),
        ),
    ]


def _version_of(ectx, attrs) -> str | None:
    if not attrs.get("document_id"):
        return None
    v = ectx.graph.open_node(
        ectx.ctx.tenant_id,
        "document_version",
        f"documents:{attrs['document_id']}:{attrs['document_sha256']}",
    )
    return v["id"] if v else None


def _amount(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


def _tamper(ectx, tenant, targets) -> list[FindingSpec]:
    by_key: dict[tuple, list[str]] = defaultdict(list)
    for i, n in tenant.invoices.items():
        num = normalize_number(n["attrs"].get("invoice_number"))
        if num and n["attrs"].get("document_sha256") and tenant.vendor(i):
            by_key[(tenant.vendor(i), num)].append(i)
    specs = []
    for x in targets:
        a = tenant.invoices[x]["attrs"]
        num = normalize_number(a.get("invoice_number"))
        sha = a.get("document_sha256")
        twins = [
            y
            for y in by_key.get((tenant.vendor(x), num), ())
            if y != x
            and y in tenant.approved
            and tenant.invoices[y]["attrs"]["document_sha256"] != sha
        ]
        if not (num and sha and twins):
            continue
        cites = [Citation(x, f"submitted, sha {sha[:12]}")]
        for y in twins:
            ya = tenant.invoices[y]["attrs"]
            cites.append(
                Citation(y, f"approved, sha {ya['document_sha256'][:12]}")
            )
        for node in [x, *twins]:
            vid = _version_of(ectx, tenant.invoices[node]["attrs"])
            if vid:
                cites.append(Citation(vid, "document version"))
        specs.append(
            FindingSpec(
                type=TAMPER,
                transaction_node=x,
                dedupe_key=f"{TAMPER}:{tenant.invoices[x]['natural_key']}",
                impact=_amount(a.get("amount")),
                currency=a.get("currency"),
                confidence=1.0,
                engine_version=ENGINE_VERSION,
                rule_version=RULE_VERSION,
                model_version="none",
                summary=(
                    f"Two approved copies of invoice {a.get('invoice_number')} "
                    "from this vendor have different document bytes."
                    if x in tenant.approved
                    else f"Invoice {a.get('invoice_number')} was approved "
                    "earlier with a different document; this copy may have "
                    "been altered."
                ),
                details={"approved_twins": len(twins)},
                citations=tuple(cites),
            )
        )
    return specs


def run(ectx: EngineContext) -> list[FindingSpec]:
    if ectx.document is not None and ectx.invoice_node is None:
        return []  # a contract or other non-invoice document
    tenant = _Tenant(ectx)
    if ectx.document is None:
        return _tamper(ectx, tenant, sorted(tenant.invoices))
    specs = _ledger(ectx, tenant)
    return specs + _tamper(ectx, tenant, [ectx.invoice_node])
