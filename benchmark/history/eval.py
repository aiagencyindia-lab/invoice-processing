"""Import a generated history, build the graph, and measure what got linked (AC-19)."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from invoice_processing.adapters import LocalBlobStore
from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust import evaluate as trust_evaluate
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.findings.model import Citation, FindingSpec
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.graph.sync import sync_tenant
from invoice_processing.trust.history import mapping
from invoice_processing.trust.history.importer import HistoryImporter
from invoice_processing.trust.history.readers import read_table
from invoice_processing.trust.modes import ENGINES
from invoice_processing.trust.providers import fakes
from invoice_processing.trust.providers.registry import Registry

from . import clauses
from . import faults as fault_families
from .generate import KINDS, generate

GATES = {
    "invoice_vendor_coverage": 0.95,
    "po_backed_coverage": 0.80,
    "contract_clause_coverage": 0.60,
}
_BASELINE = object()


def _explain_module():
    """benchmark/history/explain.py, when the explain package's branch has
    landed; absent until then, like a fault family that is not installed."""
    try:
        from . import explain  # noqa: PLC0415
    except ImportError:
        return None
    return explain


def _ratio(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def run(
    url: str,
    data_dir: Path,
    tenant_id: str = "bench-history",
    *,
    clause_decider=_BASELINE,
) -> dict:
    """`clause_decider=None` measures clause extraction with the production
    (Gemini) decider; the default is the keyword baseline."""
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(tenant_id, url) as s:
        s.add(
            models.Tenant(
                id=tenant_id, name="Bench", slug=tenant_id, status="active"
            )
        )
    ctx = RunContext(
        tenant_id=tenant_id, case_id="", run_id="bench", actor="bench"
    )
    importer = HistoryImporter(url)
    no_model = FakeDecider({}, fail=True)
    for kind in KINDS:
        path = data_dir / f"{kind}.csv"
        if not path.exists():
            continue
        data = path.read_bytes()
        headers, _ = read_table(data, f"{kind}.csv")
        importer.import_file(
            ctx,
            kind=kind,
            source_name="bench",
            filename=f"{kind}.csv",
            data=data,
            mapping=mapping.propose(kind, headers, decider=no_model),
        )
    graph, links = GraphStore(url), LinkStore(url)
    sync_tenant(ctx, graph, links, url=url, decider=no_model)

    manifest = json.loads((data_dir / "manifest.json").read_text())
    by_ext = {}
    with tenant_session(tenant_id, url) as s:
        for r in s.query(models.HistoryRecord).filter_by(tenant_id=tenant_id):
            by_ext[(r.kind, r.external_id)] = r.id
    node_of = graph.nodes_by_ref(
        tenant_id, "history_records", list(by_ext.values())
    )
    # A vendor node the sync synthesised from the invoice itself has no
    # ref_table; only a master-backed vendor counts as "linked" (AC-19).
    master_vendors = {
        n["id"] for n in graph.nodes(tenant_id, kind="vendor") if n["ref_table"]
    }
    issued = {
        e["src"]
        for e in graph.edges(tenant_id, kind="issued_by")
        if e["dst"] in master_vendors
    }
    references = {
        e["src"]: e["dst"] for e in graph.edges(tenant_id, kind="references")
    }
    fulfilled = {e["dst"] for e in graph.edges(tenant_id, kind="fulfils")}

    findings = FindingStore(url)
    invoices = po_backed = inv_linked = po_linked = unlinked = 0
    for link in manifest["expected_links"]:
        inv_node = node_of.get(by_ext.get(("ap_invoice", link["invoice"])))
        invoices += 1
        if inv_node in issued:
            inv_linked += 1
        elif inv_node:
            # The harness is the first engine: every miss is a cited finding,
            # which also drives FindingStore end to end on every run.
            unlinked += 1
            findings.upsert(
                ctx,
                FindingSpec(
                    type="BENCH-IMPORT",
                    transaction_node=inv_node,
                    dedupe_key=f"BENCH-IMPORT:{link['invoice']}",
                    impact=None,
                    currency=None,
                    confidence=1.0,
                    engine_version="bench-0",
                    rule_version="AC-19",
                    model_version="none",
                    summary="Invoice not linked to a master vendor",
                    citations=(Citation(inv_node, link["invoice"]),),
                ),
            )
        if link["po"]:
            po_backed += 1
            po_node = references.get(inv_node)
            if po_node and po_node in fulfilled:
                po_linked += 1
    out = {
        "invoice_vendor_coverage": _ratio(inv_linked, invoices),
        "po_backed_coverage": _ratio(po_linked, po_backed),
        "counts": manifest["counts"],
        "unlinked": unlinked,
    }
    with tempfile.TemporaryDirectory() as work:
        # Its own database: coverage() starts from an empty schema.
        extraction = clauses.coverage(
            f"sqlite:///{Path(work) / 'clauses.db'}",
            Path(work),
            decider=clauses.KeywordDecider()
            if clause_decider is _BASELINE
            else clause_decider,
        )
    out["contract_clause_coverage"] = extraction["coverage"]
    out["contract_clauses"] = extraction
    out["gates"] = {name: out[name] >= floor for name, floor in GATES.items()}

    modules = fault_families.modules()
    _run_engines(ctx, graph, links, url, data_dir, modules)
    out["types"] = _score(tenant_id, graph, findings, manifest, modules)
    for code, scores in out["types"].items():
        for metric, floor in _gates_of(modules)[code].items():
            ok = (
                scores[metric] <= floor
                if metric == "impact_error"
                else scores[metric] >= floor
            )
            out["gates"][f"{code}.{metric}"] = ok
    explain = _explain_module()
    if explain is not None:
        measured = explain.measure(tenant_id, graph, findings, manifest, url)
        out["gates"].update(measured.pop("gates", {}) or {})
        out["explain"] = measured
    out["passed"] = all(out["gates"].values())
    return out


class _Advisory:
    """Every engine on, nothing held: the benchmark measures, it never gates."""

    def get(self, tenant_id):
        return {"trust_engine_modes": dict.fromkeys(ENGINES, "advisory")}


def _gates_of(modules) -> dict[str, dict]:
    gates: dict[str, dict] = {}
    for module in modules:
        gates.update(getattr(module, "GATES", {}) or {})
    return gates


def _run_engines(ctx, graph, links, url, data_dir, modules) -> None:
    registry = Registry(graph)
    fakes.install(registry)
    scripts: dict[str, dict] = defaultdict(dict)
    for module in modules:
        seed = getattr(module, "seed_graph", None)
        if seed is not None:
            seed(ctx, graph, data_dir)
        for capability, script in (
            getattr(module, "provider_scripts", {}) or {}
        ).items():
            scripts[capability].update(script)
    for capability, script in scripts.items():
        registry.register(
            capability, "fake", lambda s=dict(script): fakes.FakeProvider(s)
        )
    trust_evaluate.evaluate(
        ctx,
        None,
        graph=graph,
        links=links,
        url=url,
        settings_store=_Advisory(),
        findings=FindingStore(url),
        providers=registry,
        decider=FakeDecider({}, fail=True),
        blobs=LocalBlobStore(data_dir / "blobs"),
    )


def _score(tenant_id, graph, findings, manifest, modules) -> dict:
    """Per gated finding type: precision, recall and ₹ error against the
    manifest, matched on the transaction's document number."""
    expected: dict[str, dict] = defaultdict(dict)
    for fault in manifest.get("faults", []):
        expected[fault["type"]][fault["doc_number"]] = fault.get(
            "expected_impact"
        )
    doc_of: dict[str, str | None] = {}

    def doc(node_id: str) -> str | None:
        if node_id not in doc_of:
            node = graph.node(tenant_id, node_id) or {"attrs": {}}
            a = node["attrs"]
            doc_of[node_id] = a.get("doc_number") or a.get("invoice_number")
        return doc_of[node_id]

    types = {}
    for code in _gates_of(modules):
        found = {
            doc(f["transaction_node"]): f["impact"]
            for f in findings.list(tenant_id, type=code)
        }
        want = expected.get(code, {})
        hit = want.keys() & found.keys()
        precision = (
            len(hit) / len(found) if found else (1.0 if not want else 0.0)
        )
        recall = len(hit) / len(want) if want else 1.0
        priced = [d for d in hit if want[d] not in (None, "")]
        num = sum(
            abs(Decimal(found[d] or 0) - Decimal(str(want[d]))) for d in priced
        )
        den = sum(Decimal(str(want[d])) for d in priced)
        types[code] = {
            "expected": len(want),
            "found": len(found),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "impact_error": round(float(num / den), 4) if den else 0.0,
        }
    return types


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="benchmark.history.eval")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--db",
        default=None,
        help="SQLAlchemy URL; default a temporary SQLite file",
    )
    parser.add_argument(
        "--data",
        default=None,
        help="generated data dir; default: generate into a temp dir",
    )
    parser.add_argument("--out", default="benchmark/runs/history")
    parser.add_argument(
        "--live-model",
        action="store_true",
        help="measure clause extraction with Gemini, not the keyword baseline",
    )
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(args.data) if args.data else Path(tmp) / "data"
        if not args.data:
            generate(args.seed, data)
        url = args.db or f"sqlite:///{Path(tmp) / 'bench.db'}"
        result = run(
            url, data, clause_decider=None if args.live_model else _BASELINE
        )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out_dir / f"{stamp}.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    shown = (
        "invoice_vendor_coverage",
        "po_backed_coverage",
        "contract_clause_coverage",
        "gates",
        "passed",
    )
    print(json.dumps({k: result[k] for k in shown}))
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
