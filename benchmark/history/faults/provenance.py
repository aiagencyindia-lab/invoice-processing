"""Provenance fault family: an approved invoice re-submitted with changed bytes.

`inject` records the tampered cases in the manifest; `seed_graph` plants them
(and decoys) as graph nodes, because document bytes and approval are not CSV
history. Each case is an approved original and a re-submission with the same
number and vendor but a different `document_sha256`. Decoys that must not
fire: the same bytes re-sent, the same number under another vendor, and a
changed copy of an invoice nobody approved.

Planted numbers are `PRV-*`, never a history number, and amounts differ
within a pair, so other engines' duplicate rules do not see the pairs.
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

TYPE = "PROV-TAMPER"
CASES = 6
GATES = {TYPE: {"precision": 1.0, "recall": 1.0, "impact_error": 0.0}}


def inject(rng, rows, faults, *, start, months) -> None:
    vendors = [v["vendor_key"] for v in rows["vendor"]]
    for k in range(CASES):
        faults.append(
            {
                "type": TYPE,
                "doc_number": f"PRV-{k:03d}",
                "vendor_key": rng.choice(vendors),
                "expected_impact": str(rng.randint(5_000, 500_000)),
            }
        )


def _sha(*parts) -> str:
    return hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()


def _plant(ctx, graph, vendor, number, role, *, sha, amount, approved, at):
    doc_id = f"prv-{number}-{role}"
    nid = graph.upsert_node(
        ctx,
        kind="invoice",
        natural_key=f"bench:provenance:{doc_id}",
        attrs={
            "invoice_number": number,
            "doc_number": number,
            "amount": str(amount),
            "currency": "INR",
            "document_id": doc_id,
            "document_sha256": sha,
            "decision": "approve" if approved else None,
        },
        at=at,
    )
    vid = graph.upsert_node(
        ctx,
        kind="document_version",
        natural_key=f"documents:{doc_id}:{sha}",
        ref_table="documents",
        ref_id=doc_id,
        attrs={"sha256": sha, "source": "email"},
        at=at,
    )
    for src, dst, kind in ((nid, vendor, "issued_by"), (vid, nid, "evidences")):
        graph.upsert_edge(
            ctx,
            src=src,
            dst=dst,
            kind=kind,
            source_system="bench",
            provenance="system_record",
            confidence=1.0,
            observed_at=at,
            at=at,
        )


def seed_graph(ctx, graph, data_dir: Path) -> None:
    at = datetime.now(timezone.utc)
    manifest = json.loads((data_dir / "manifest.json").read_text())
    vendor_of = {
        n["attrs"].get("name"): n["id"]
        for n in graph.nodes(ctx.tenant_id, kind="vendor")
        if n["ref_table"] == "history_records"
    }
    for fault in manifest["faults"]:
        if fault["type"] != TYPE:
            continue
        number, amount = fault["doc_number"], Decimal(fault["expected_impact"])
        vendor = vendor_of[fault["vendor_key"]]
        _plant(
            ctx, graph, vendor, number, "orig",
            sha=_sha(number, "orig"), amount=amount - 1, approved=True, at=at,
        )  # fmt: skip
        _plant(
            ctx, graph, vendor, number, "resub",
            sha=_sha(number, "resub"), amount=amount, approved=False, at=at,
        )  # fmt: skip

    with (data_dir / "vendor.csv").open(encoding="utf-8") as fh:
        names = sorted(r["vendor_key"] for r in csv.DictReader(fh))
    rng = random.Random(f"{manifest['seed']}:provenance-decoys")  # noqa: S311
    for k in range(6):
        number = f"PRV-D{k:02d}"
        a, b = rng.sample(names, 2)
        amount = Decimal(rng.randint(5_000, 500_000))
        same_bytes, other_vendor = k < 2, 2 <= k < 4
        _plant(
            ctx, graph, vendor_of[a], number, "orig",
            sha=_sha(number, "orig"), amount=amount - 1,
            approved=k < 4, at=at,
        )  # fmt: skip
        _plant(
            ctx, graph, vendor_of[b if other_vendor else a], number, "resub",
            sha=_sha(number, "orig" if same_bytes else "resub"),
            amount=amount, approved=False, at=at,
        )  # fmt: skip
