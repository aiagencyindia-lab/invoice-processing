"""Import a generated history, build the graph, and measure what got linked (AC-19)."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.findings.model import Citation, FindingSpec
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.graph.sync import sync_tenant
from invoice_processing.trust.history import mapping
from invoice_processing.trust.history.importer import HistoryImporter
from invoice_processing.trust.history.readers import read_table

from .generate import KINDS, generate

GATES = {"invoice_vendor_coverage": 0.95, "po_backed_coverage": 0.80}


def _ratio(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def run(url: str, data_dir: Path, tenant_id: str = "bench-history") -> dict:
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
        data = (data_dir / f"{kind}.csv").read_bytes()
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
        "contract_clause_coverage": None,
        "not_measurable_until": {"contract_clause_coverage": "wave1"},
        "counts": manifest["counts"],
        "unlinked": unlinked,
    }
    out["gates"] = {name: out[name] >= floor for name, floor in GATES.items()}
    out["passed"] = all(out["gates"].values())
    return out


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
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(args.data) if args.data else Path(tmp) / "data"
        if not args.data:
            generate(args.seed, data)
        url = args.db or f"sqlite:///{Path(tmp) / 'bench.db'}"
        result = run(url, data)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out_dir / f"{stamp}.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    shown = ("invoice_vendor_coverage", "po_backed_coverage", "gates", "passed")
    print(json.dumps({k: result[k] for k in shown}))
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
