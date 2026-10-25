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
from datetime import UTC, date, datetime, timezone
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
KYC_SUSPECT = "PROV-KYC-SUSPECT"
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
    KYC_SUSPECT,
    "A vendor's onboarding document scores low on authenticity",
    engine="provenance",
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
# KYC documents and proofs are uploads; who uploaded them is the channel.
ROLE_CHANNEL = {"clerk": 0.6, "approver": 0.8, "controller": 0.8, "admin": 0.8}
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


def _channel(tenant, doc, invoice_node) -> float:
    source = doc.get("source")
    if source == "email":
        attrs = tenant.invoices[invoice_node]["attrs"]
        sender = (attrs.get("document_sender_domain") or "").strip().lower()
        return (
            1.0
            if sender and sender in tenant.vendor_domains(invoice_node)
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


def _invoice_versions(ectx, tenant) -> dict:
    """Every invoice document's version attrs, by canonical vendor:
    {vendor: [(document_id, attrs)]}. Found through the invoice's
    `document_id` attr: a sync that re-versions an invoice node closes its
    `evidences` edge until that document is evaluated again."""
    invoice_of_doc = {
        n["attrs"].get("document_id"): i for i, n in tenant.invoices.items()
    }
    out: dict = defaultdict(list)
    for v in ectx.graph.nodes(ectx.ctx.tenant_id, kind="document_version"):
        doc_id = v["natural_key"].split(":")[1]
        inv = invoice_of_doc.get(doc_id)
        if inv and tenant.vendor(inv):
            out[tenant.vendor(inv)].append((doc_id, v["attrs"]))
    return out


def _prior(tenant, versions, doc_id: str, invoice_node: str) -> list[dict]:
    """The vendor's other invoice documents' version attrs."""
    vendor = tenant.vendor(invoice_node)
    return [a for d, a in versions.get(vendor, ()) if d != doc_id]


def _edited(ectx, doc, vid, facts, invoice_node, attrs) -> FindingSpec | None:
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
        transaction_node=invoice_node,
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


def _key(doc: dict) -> str:
    return f"documents:{doc['id']}:{doc['sha256']}"


def plant_vendor_document(
    ctx,
    graph,
    *,
    document_id: str,
    sha256: str,
    vendor_id: str,
    subkind: str,
    filename: str,
    role: str,
    at: datetime | None = None,
) -> str:
    """The node a KYC upload writes before the document is scored: what it
    is, whose, and who uploaded it, with `evidences` to the vendor's node
    when the vendor has one. The run keeps all of it."""
    at = at or datetime.now(UTC)
    vid = graph.upsert_node(
        ctx,
        kind="document_version",
        natural_key=f"documents:{document_id}:{sha256}",
        ref_table="documents",
        ref_id=document_id,
        attrs={
            "kind": "kyc",
            "subkind": subkind,
            "vendor_id": vendor_id,
            "filename": filename,
            "sha256": sha256,
            "source": "upload",
            "uploaded_by_role": role,
        },
        at=at,
    )
    vendor = graph.nodes_by_ref(ctx.tenant_id, "vendors", [vendor_id]).get(
        vendor_id
    )
    if vendor:
        graph.upsert_edge(
            ctx,
            src=vid,
            dst=vendor,
            kind="evidences",
            source_system="upload",
            provenance="human_assertion",
            confidence=1.0,
            observed_at=at,
            at=at,
        )
    return vid


def _vendor_key(graph, tenant_id: str, node_id: str | None) -> str | None:
    """A vendor node's master id (what the upload route names), its natural
    key when the sync synthesised it from invoices."""
    node = graph.node(tenant_id, node_id) if node_id else None
    if node is None:
        return None
    return (
        node["ref_id"]
        if node["ref_table"] == "vendors"
        else node["natural_key"]
    )


_EDGE_FIELDS = ("kind", "source_system", "provenance", "confidence")


def _evidences(dst: str) -> dict:
    return {
        "src": None,
        "dst": dst,
        "kind": "evidences",
        "source_system": "provenance",
        "provenance": "system_record",
        "confidence": 1.0,
    }


def _vendor_document(ectx, tenant, doc):
    """(attrs to keep, channel, prior attrs, edges, vendor node) for a KYC
    document or a proof. The upload (or the reality branch, for a proof)
    planted its node; scoring re-versions it, which closes every edge into
    or out of it, so they are drawn again as they were (None marks the
    version node)."""
    t, g = ectx.ctx.tenant_id, ectx.graph
    planted = g.open_node(t, "document_version", _key(doc))
    keep = dict((planted or {}).get("attrs") or {})
    keep["kind"] = doc["kind"]
    edges = []
    if planted:
        for e in g.edges(t, src=planted["id"]) + g.edges(t, dst=planted["id"]):
            edges.append(
                {
                    **{k: e[k] for k in _EDGE_FIELDS},
                    "src": None if e["src"] == planted["id"] else e["src"],
                    "dst": None if e["dst"] == planted["id"] else e["dst"],
                }
            )
    evidences = [
        e["dst"] for e in edges if e["src"] is None and e["kind"] == "evidences"
    ]
    vendor = None
    if doc["kind"] == "kyc":
        vid = keep.get("vendor_id")
        vendor = g.nodes_by_ref(t, "vendors", [vid]).get(vid) if vid else None
        if vendor and vendor not in evidences:
            edges.append(_evidences(vendor))
    else:  # a proof: its vendor is its invoice's
        for dst in evidences:
            if dst in tenant.issuer:
                vendor = tenant.issuer[dst][0]
                keep["vendor_id"] = _vendor_key(g, t, vendor)
                break
    # ponytail: scans every version node, as `_prior` does.
    prior = [
        v["attrs"]
        for v in g.nodes(t, kind="document_version")
        if keep.get("vendor_id")
        and v["ref_id"] != doc["id"]
        and v["attrs"].get("fingerprint")
        and v["attrs"].get("kind") == doc["kind"]
        and v["attrs"].get("subkind") == keep.get("subkind")
        and v["attrs"].get("vendor_id") == keep["vendor_id"]
    ]
    channel = ROLE_CHANNEL.get(keep.get("uploaded_by_role"), CHANNEL["upload"])
    return keep, channel, prior, edges, vendor


def _ledger(
    ectx: EngineContext,
    tenant: _Tenant,
    doc: dict,
    invoice_node: str | None,
    data: bytes | None = None,
    versions: dict | None = None,
) -> tuple[str | None, list[FindingSpec]]:
    """Score one document and record its `document_version` node: (its id,
    the findings). An invoice document (`invoice_node`) is judged against
    the vendor's other invoices; a KYC document or proof against the
    vendor's documents of its kind. (None, []) when it is not a PDF."""
    facts = pdfmeta.facts(
        ectx.blobs.get(doc["storage_key"]) if data is None else data
    )
    if facts is None:
        logger.info("provenance_skipped", extra={"reason": "not_pdf"})
        return None, []
    if invoice_node is not None:
        attrs = tenant.invoices[invoice_node]["attrs"]
        keep = {"sender_domain": attrs.get("document_sender_domain")}
        channel = _channel(tenant, doc, invoice_node)
        if versions is None:
            versions = _invoice_versions(ectx, tenant)
        prior = _prior(tenant, versions, doc["id"], invoice_node)
        edges = [_evidences(invoice_node)]
    else:
        keep, channel, prior, edges, vendor = _vendor_document(
            ectx, tenant, doc
        )
    fp = pdfmeta.fingerprint(facts)
    matching = sum(1 for p in prior if p.get("fingerprint") == fp)
    min_history = ectx.threshold("provenance.template_min_history")
    components = {
        "channel": channel,
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
    vid = ectx.graph.upsert_node(
        ectx.ctx,
        kind="document_version",
        natural_key=_key(doc),
        ref_table="documents",
        ref_id=doc["id"],
        attrs={
            **keep,
            "sha256": doc["sha256"],
            "source": doc.get("source"),
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
    for e in edges:
        ectx.graph.upsert_edge(
            ectx.ctx,
            src=e["src"] or vid,
            dst=e["dst"] or vid,
            kind=e["kind"],
            source_system=e["source_system"],
            provenance=e["provenance"],
            confidence=e["confidence"],
            observed_at=ectx.now,
        )
    threshold = ectx.threshold("provenance.suspect_below")
    low = score < threshold
    summary = (
        f"Document authenticity score {score:.2f} is below {threshold:.2f}."
    )
    if invoice_node is None:
        if doc["kind"] != "kyc" or not low:
            # a proof's score reaches the reality engine through its node
            return vid, []
        return vid, [
            FindingSpec(
                type=KYC_SUSPECT,
                transaction_node=vendor or vid,
                dedupe_key=f"{KYC_SUSPECT}:{doc['id']}",
                impact=None,
                currency=None,
                confidence=round(1 - score, 4),
                engine_version=ENGINE_VERSION,
                rule_version=RULE_VERSION,
                model_version="none",
                summary=f"Vendor {keep.get('subkind') or 'onboarding'} "
                f"document: authenticity score {score:.2f} is below "
                f"{threshold:.2f}.",
                details={
                    "score": score,
                    "components": components,
                    "subkind": keep.get("subkind"),
                    "fingerprint": fp[:16],
                },
                citations=(Citation(vid, f"authenticity score {score:.2f}"),),
            )
        ]
    specs = [
        e for e in [_edited(ectx, doc, vid, facts, invoice_node, attrs)] if e
    ]
    if not low:
        return vid, specs
    return vid, [
        *specs,
        FindingSpec(
            type=SUSPECT,
            transaction_node=invoice_node,
            dedupe_key=f"{SUSPECT}:{doc['id']}",
            impact=None,
            currency=None,
            confidence=round(1 - score, 4),
            engine_version=ENGINE_VERSION,
            rule_version=RULE_VERSION,
            model_version="none",
            summary=summary,
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


def _batch_ledger(ectx, tenant) -> list[FindingSpec]:
    """A batch run scores every invoice document not yet at this engine
    version (new ones, and v1 nodes once), reading the bytes at their
    content address. Documents whose bytes are not stored are skipped."""
    specs, missing, failed, error = [], 0, 0, None
    # Loaded once: documents scored earlier in this run are not each other's
    # history until the next run.
    versions = _invoice_versions(ectx, tenant)
    current = {
        v["natural_key"]
        for v in ectx.graph.nodes(ectx.ctx.tenant_id, kind="document_version")
        if v["attrs"].get("engine_version") == ENGINE_VERSION
    }
    for node in sorted(tenant.invoices):
        a = tenant.invoices[node]["attrs"]
        doc_id, sha = a.get("document_id"), a.get("document_sha256")
        if not (doc_id and sha):
            continue
        doc = {
            "id": doc_id,
            "kind": None,
            "sha256": sha,
            # The ingest's content-addressed key (api.ingest.storage_key_for).
            "storage_key": f"documents/{sha}.pdf",
            "source": a.get("document_source"),
        }
        if _key(doc) in current:
            continue
        try:
            data = ectx.blobs.get(doc["storage_key"])
        except Exception as exc:  # local, GCS and test stores raise differently
            if isinstance(exc, (KeyError, FileNotFoundError)) or (
                type(exc).__name__ == "NotFound"
            ):
                missing += 1
            else:  # the store itself is failing: say so, never fail the run
                failed, error = failed + 1, type(exc).__name__
            continue
        specs += _ledger(ectx, tenant, doc, node, data, versions)[1]
    if missing:
        logger.info("provenance_batch_skipped", extra={"missing": missing})
    if failed:
        logger.warning(
            "provenance_batch_blob_failed",
            extra={"failed": failed, "error": error},
        )
    return specs


def score(ectx: EngineContext) -> str | None:
    """Score `ectx.document` now and return its `document_version` node id
    (None when it is not a readable PDF). Writes the node, never findings:
    those come from `trust.evaluate` under the tenant's mode."""
    return _ledger(ectx, _Tenant(ectx), ectx.document, ectx.invoice_node)[0]


def run(ectx: EngineContext) -> list[FindingSpec]:
    doc = ectx.document
    if doc is not None and ectx.invoice_node is None:
        if doc.get("kind") in ("kyc", "proof"):
            return _ledger(ectx, _Tenant(ectx), doc, None)[1]
        return []  # a contract or other non-invoice document
    tenant = _Tenant(ectx)
    if doc is None:
        specs = _batch_ledger(ectx, tenant)
        return specs + _tamper(ectx, tenant, sorted(tenant.invoices))
    _, specs = _ledger(ectx, tenant, doc, ectx.invoice_node)
    return specs + _tamper(ectx, tenant, [ectx.invoice_node])
