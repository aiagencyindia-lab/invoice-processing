"""Contract clause extraction, measured end to end (AC-19 "contract-clause coverage").

`write` lays down a fixed corpus of contract PDFs (varied phrasing, decoy
sentences that carry numbers) and its answer key, written by hand rather than
by the parser. `coverage` runs each contract through the production path --
a contract document, its bytes in a blob store, `trust.evaluate` on that
document -- and scores the clause nodes the Leakage engine wrote against the
key: a planted clause counts when one extracted clause has its type and
numbers.

The model is the one part CI cannot run. `KeywordDecider` stands in for it: a
keyword classifier that sees only the prompt (the candidate sentences), never
the key, so the gate measures candidate selection, the decide() round trip and
regex parameter parsing, with a deliberately plain classifier. `--live-model`
on the eval CLI swaps in Gemini to measure the model itself.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from invoice_processing.adapters import LocalBlobStore
from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust import evaluate as trust_evaluate
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.modes import ENGINES
from invoice_processing.trust.providers import fakes
from invoice_processing.trust.providers.registry import Registry

TENANT = "bench-clauses"
ITEMS = (
    "Hex Bolts",
    "Steel Rods",
    "Copper Wire",
    "PVC Pipes",
    "Safety Gloves",
    "Paint Drums",
    "Bearings",
    "Gaskets",
    "Welding Rods",
    "Cable Trays",
)
DECOYS = (
    "This agreement is governed by the laws of India.",
    "Either party may terminate on 90 days written notice.",
    "Disputes go to arbitration in Mumbai under the 1996 Act.",
    "Clause 14 survives termination for 3 years.",
    "Notices are sent to the registered office at 221 Industrial Estate.",
)


def _corpus() -> list[dict]:
    """Ten contracts; every number below is the answer, typed by hand."""
    out = []
    for k, item in enumerate(ITEMS):
        price = Decimal(40 + 7 * k) + Decimal("0.50")
        tier_qty, tier_price = 500 * (k + 1), price - 5
        threshold, pct = 100_000 * (k + 2), 1 + k % 3
        start_year = 2024 + k % 2
        rate_line = (
            f"The rate for {item} is INR {price} per unit.",
            f"{item} shall be supplied at Rs. {price} per unit.",
            f"Unit price for {item}: INR {price} per unit.",
        )[k % 3]
        tier_line = (
            f"Above {tier_qty:,} units of {item} the price is Rs. {tier_price} per unit.",
            f"For orders of {tier_qty:,} units or more of {item}, the unit price is INR {tier_price}.",
        )[k % 2]
        rebate_line = (
            f"A rebate of {pct}% applies once annual spend exceeds INR {threshold:,}.",
            f"The supplier grants a {pct}% rebate on annual purchases above Rs. {threshold:,}.",
        )[k % 2]
        days, net = (10, 30) if k % 2 else (15, 45)
        discount_line = (
            f"Payment terms are 2/{days} net {net}.",
            f"A 2% discount applies if paid within {days} days, otherwise net {net} days.",
        )[k % 2]
        term_line = (
            f"This agreement commences on 1 April {start_year} and expires on "
            f"31 March {start_year + 2}."
        )
        clauses = [
            {
                "type": "rate",
                "params": {"price": str(price)},
                "text": rate_line,
            },
            {
                "type": "tier",
                "params": {"qty": str(tier_qty), "price": str(tier_price)},
                "text": tier_line,
            },
            {
                "type": "rebate",
                "params": {"threshold": str(threshold), "pct": str(pct)},
                "text": rebate_line,
            },
            {
                "type": "discount",
                "params": {"pct": "2", "days": str(days), "net": str(net)},
                "text": discount_line,
            },
            {
                "type": "term",
                "params": {
                    "start": f"{start_year}-04-01",
                    "end": f"{start_year + 2}-03-31",
                },
                "text": term_line,
            },
        ]
        if k % 2 == 0:
            esc = 3 + k % 4
            clauses.append(
                {
                    "type": "escalator",
                    "params": {"pct": str(esc)},
                    "text": f"Prices increase by {esc}% on each anniversary.",
                }
            )
        decoys = [DECOYS[(k + i) % len(DECOYS)] for i in range(3)]
        lines = [f"Master Supply Agreement {k + 1} for {item}."]
        for i, c in enumerate(clauses):
            lines.append(c["text"])
            if i < len(decoys):
                lines.append(decoys[i])
        out.append(
            {
                "file": f"contracts/msa-{k + 1}.pdf",
                "lines": lines,
                "clauses": [
                    {"type": c["type"], "params": c["params"]} for c in clauses
                ],
            }
        )
    return out


def make_pdf(lines: list[str]) -> bytes:
    """A one-page text-layer PDF, one Helvetica line per string, stdlib only."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    body = "".join(
        f"BT /F1 9 Tf 40 {780 - 14 * i} Td ({esc(line)}) Tj ET\n"
        for i, line in enumerate(lines)
    )
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [5 0 R] /Count 1 >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(body)} >>\nstream\n{body}endstream",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for n, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return out


def write(out_dir: Path) -> list[dict]:
    """The contract PDFs and `contract_clauses.json`, the key."""
    out_dir = Path(out_dir)
    key = []
    for c in _corpus():
        path = out_dir / c["file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(make_pdf(c["lines"]))
        key.append({"file": c["file"], "clauses": c["clauses"]})
    (out_dir / "contract_clauses.json").write_text(
        json.dumps(key, indent=2), encoding="utf-8"
    )
    return key


# -- the stand-in model -------------------------------------------------------

_RULES = (
    ("rebate", re.compile(r"\brebate", re.I)),
    ("escalator", re.compile(r"\b(?:increase|escalat)\w*\b.*%", re.I)),
    ("discount", re.compile(r"\bnet\s+\d+|\bdiscount\b.*\bdays\b", re.I)),
    ("tier", re.compile(r"\b(?:above|or more|over)\b.*\bunits?\b", re.I)),
    ("rate", re.compile(r"\bper unit\b|\bunit price\b|\brate\b", re.I)),
    ("term", re.compile(r"\b(?:commences?|expires?|effective)\b", re.I)),
)


class KeywordDecider:
    """Classifies each candidate in a decide() prompt by keyword alone."""

    def __call__(self, prompt: str) -> tuple[str, str]:
        record = prompt.split("Record:\n", 1)[1].split("\nQuestions:\n", 1)[0]
        candidates = json.loads(record).get("candidates") or {}
        answers = []
        for cid, text in candidates.items():
            kind = next((k for k, rx in _RULES if rx.search(text)), "none")
            answers.append({"id": cid, "value": kind, "probability": 0.8})
            answers.append(
                {"id": f"{cid}_score", "value": 0.8, "probability": 0.8}
            )
        return json.dumps({"answers": answers}), "keyword-baseline"


# -- scoring ------------------------------------------------------------------


class _LeakageOnly:
    def get(self, tenant_id):
        return {
            "trust_engine_modes": {
                **dict.fromkeys(ENGINES, "off"),
                "leakage": "advisory",
            }
        }


def _norm(params: dict) -> dict:
    out = {}
    for k, v in (params or {}).items():
        try:
            out[k] = Decimal(str(v)).normalize()
        except (InvalidOperation, ValueError):
            out[k] = str(v)
    return out


def coverage(url: str, work_dir: Path, *, decider=None) -> dict:
    """Extract every corpus contract through `trust.evaluate`; score the key.
    `decider=None` uses the production (budgeted Gemini) decider."""
    work_dir = Path(work_dir)
    key = write(work_dir)
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(TENANT, url) as s:
        s.add(
            models.Tenant(id=TENANT, name="Bench", slug=TENANT, status="active")
        )
    ctx = RunContext(
        tenant_id=TENANT, case_id="", run_id="bench", actor="bench"
    )
    graph, links = GraphStore(url), LinkStore(url)
    blobs = LocalBlobStore(work_dir)
    registry = Registry(graph)
    fakes.install(registry)
    planted = matched = extracted = correct = 0
    for contract in key:
        data = (work_dir / contract["file"]).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        with tenant_session(TENANT, url) as s:
            doc = models.Document(
                tenant_id=TENANT,
                storage_key=contract["file"],
                sha256=sha,
                filename=Path(contract["file"]).name,
                mime_type="application/pdf",
                byte_size=len(data),
                source="upload",
                kind="contract",
            )
            s.add(doc)
            s.flush()
            doc_id = doc.id
        node = graph.upsert_node(
            ctx,
            kind="contract",
            natural_key=f"documents:{doc_id}",
            ref_table="documents",
            ref_id=doc_id,
            attrs={"filename": Path(contract["file"]).name, "sha256": sha},
        )
        trust_evaluate.evaluate(
            ctx,
            doc_id,
            graph=graph,
            links=links,
            url=url,
            settings_store=_LeakageOnly(),
            findings=FindingStore(url),
            providers=registry,
            decider=decider,
            blobs=blobs,
        )
        clause_ids = {
            e["src"] for e in graph.edges(TENANT, dst=node, kind="part_of")
        }
        found = [
            (n["attrs"].get("type"), _norm(n["attrs"].get("params")))
            for n in graph.nodes(TENANT, kind="contract_clause")
            if n["id"] in clause_ids
        ]
        want = [(c["type"], _norm(c["params"])) for c in contract["clauses"]]
        planted += len(want)
        matched += sum(1 for w in want if w in found)
        extracted += len(found)
        correct += sum(1 for f in found if f in want)
    return {
        "planted": planted,
        "matched": matched,
        "coverage": round(matched / planted, 4) if planted else 0.0,
        "precision": round(correct / extracted, 4) if extracted else 0.0,
    }
