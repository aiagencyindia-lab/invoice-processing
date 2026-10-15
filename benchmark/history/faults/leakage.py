"""Leakage fault family: rate overcharge, near-duplicate, unapplied credit,
missed discount.

Contract vendors are appended (never the base vendors changed), so other
families' rows are untouched. A contract is graph state, not a CSV row:
`seed_graph` plants each contract vendor's contract, its reviewed clauses and
the invoice lines the history CSVs cannot carry. Contract terms follow from
the vendor's number; a faulty line's price is in its manifest fault.

DUP-SPLIT is not gated: it needs the tenant's `high_value_threshold`, which
the eval's settings leave at 0 (off).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

GATE = {"precision": 0.9, "recall": 0.8, "impact_error": 0.02}
GATES = {
    code: dict(GATE)
    for code in (
        "LEAK-RATE",
        "LEAK-DISCOUNT",
        "DUP-NEAR",
        "STMT-UNAPPLIED-CREDIT",
    )
}

VENDORS = 6
PREFIX = "Leakline Supplies"
ITEM = "Steel Rod 12mm"
DISCOUNT = {"pct": "2", "days": "10", "net": "30"}
TERM = {"start": "2024-12-01", "end": "2027-12-31"}
OVERCHARGE = Decimal("1.08")


def rate(k: int) -> Decimal:
    return Decimal(1000 + 100 * k)


def _iso(d: date) -> str:
    return d.isoformat()


def inject(rng, rows, faults, *, start, months) -> None:
    base_invoices = list(rows["ap_invoice"])
    pay = credit = 0
    for k in range(VENDORS):
        name = f"{PREFIX} {k}"
        rows["vendor"].append(
            {
                "external_id": f"LKV{k}",
                "vendor_key": name,
                "email_domain": f"leakline{k}.in",
            }
        )
        for m in range(months):
            d = start + timedelta(days=30 * m + rng.randint(0, 20))
            qty = rng.randint(5, 60)
            price = rate(k)
            number = f"LK{k}-{m + 1:03d}"
            over = rng.random() < 0.15
            if over:
                price = rate(k) * OVERCHARGE
                faults.append(
                    {
                        "type": "LEAK-RATE",
                        "doc_number": number,
                        "vendor_key": name,
                        "expected_impact": str((price - rate(k)) * qty),
                        "quantity": qty,
                        "unit_price": str(price),
                    }
                )
            amount = price * qty
            rows["ap_invoice"].append(
                {
                    "external_id": f"LKI{k}-{m}",
                    "doc_number": number,
                    "vendor_key": name,
                    "amount": str(amount),
                    "currency": "INR",
                    "record_date": _iso(d),
                    "po_number": "",
                }
            )
            rows["vendor_statement_line"].append(
                {
                    "external_id": f"LKS{k}-{m}",
                    "vendor_key": name,
                    "doc_number": number,
                    "amount": str(amount),
                    "currency": "INR",
                    "record_date": _iso(d),
                }
            )
            draw = rng.random()
            if draw < 0.15:  # paid early, in full: the discount is lost
                paid, when = amount, d + timedelta(days=rng.randint(1, 9))
                faults.append(
                    {
                        "type": "LEAK-DISCOUNT",
                        "doc_number": number,
                        "vendor_key": name,
                        "expected_impact": str(amount * 2 / 100),
                    }
                )
            elif draw < 0.45:  # paid early, discount taken
                paid = amount * Decimal("0.98")
                when = d + timedelta(days=rng.randint(1, 9))
            else:
                paid, when = amount, d + timedelta(days=rng.randint(15, 40))
            pay += 1
            rows["payment"].append(
                {
                    "external_id": f"LKP{pay}",
                    "doc_number": number,
                    "vendor_key": name,
                    "amount": str(paid),
                    "currency": "INR",
                    "record_date": _iso(when),
                    "bank_reference": f"LKUTR{pay:06d}",
                }
            )
        for c in range(3):
            credit += 1
            number = f"LCN{k}-{c + 1}"
            amount = Decimal(rng.randint(500, 20_000))
            when = start + timedelta(days=rng.randint(30, 30 * months - 30))
            rows["vendor_statement_line"].append(
                {
                    "external_id": f"LKC{credit}",
                    "vendor_key": name,
                    "doc_number": number,
                    "amount": str(-amount),
                    "currency": "INR",
                    "record_date": _iso(when),
                }
            )
            if rng.random() < 0.4:
                faults.append(
                    {
                        "type": "STMT-UNAPPLIED-CREDIT",
                        "doc_number": number,
                        "vendor_key": name,
                        "expected_impact": str(amount),
                    }
                )
            else:
                pay += 1
                rows["payment"].append(
                    {
                        "external_id": f"LKP{pay}",
                        "doc_number": number,
                        "vendor_key": name,
                        "amount": str(amount),
                        "currency": "INR",
                        "record_date": _iso(when + timedelta(days=5)),
                        "bank_reference": f"LKUTR{pay:06d}",
                    }
                )
    for n, inv in enumerate(rng.sample(base_invoices, 10)):
        dup = dict(inv)
        dup["external_id"] = f"LKD{n}"
        dup["doc_number"] = inv["doc_number"] + "A"
        dup["record_date"] = _iso(
            date.fromisoformat(inv["record_date"])
            + timedelta(days=rng.randint(1, 5))
        )
        rows["ap_invoice"].append(dup)
        faults.append(
            {
                "type": "DUP-NEAR",
                "doc_number": dup["doc_number"],
                "vendor_key": dup["vendor_key"],
                "expected_impact": dup["amount"],
            }
        )


def _edge(ctx, graph, src, dst, kind, provenance, at) -> None:
    graph.upsert_edge(
        ctx,
        src=src,
        dst=dst,
        kind=kind,
        source_system="bench",
        provenance=provenance,
        confidence=1.0,
        observed_at=at,
        at=at,
    )


def _clause(ctx, graph, contract, kind, params, excerpt, at) -> None:
    node = graph.upsert_node(
        ctx,
        kind="contract_clause",
        natural_key=f"clause:{contract}:1:{kind}",
        attrs={
            "type": kind,
            "params": params,
            "page": 1,
            "excerpt": excerpt,
            "confidence": 0.95,
            "reviewed": True,
        },
        at=at,
    )
    _edge(ctx, graph, node, contract, "part_of", "model_inference", at)


def seed_graph(ctx, graph, data_dir: Path) -> None:
    at = datetime.now(timezone.utc)
    manifest = json.loads((Path(data_dir) / "manifest.json").read_text())
    priced = {
        f["doc_number"]: (Decimal(f["quantity"]), Decimal(f["unit_price"]))
        for f in manifest["faults"]
        if f["type"] == "LEAK-RATE"
    }
    tenant = ctx.tenant_id
    vendor_of = {
        e["src"]: e["dst"] for e in graph.edges(tenant, kind="issued_by")
    }
    invoices = graph.nodes(tenant, kind="invoice")
    for v in graph.nodes(tenant, kind="vendor"):
        name = v["attrs"].get("name") or ""
        if not name.startswith(PREFIX + " ") or not v["ref_table"]:
            continue
        k = int(name.rsplit(" ", 1)[1])
        contract = graph.upsert_node(
            ctx,
            kind="contract",
            natural_key=f"bench:contract:{k}",
            attrs={"filename": f"msa-{k}.pdf", "vendor_id": None},
            at=at,
        )
        _edge(ctx, graph, contract, v["id"], "governs", "human_assertion", at)
        _clause(
            ctx,
            graph,
            contract,
            "rate",
            {"price": str(rate(k))},
            f"The rate for {ITEM} is INR {rate(k)} per unit.",
            at,
        )
        _clause(
            ctx,
            graph,
            contract,
            "discount",
            DISCOUNT,
            "Payment terms 2/10 net 30.",
            at,
        )
        _clause(
            ctx,
            graph,
            contract,
            "term",
            TERM,
            f"This agreement runs from {TERM['start']} to {TERM['end']}.",
            at,
        )
        for inv in invoices:
            if vendor_of.get(inv["id"]) != v["id"]:
                continue
            doc = inv["attrs"]["doc_number"]
            amount = Decimal(inv["attrs"]["amount"])
            qty, price = priced.get(doc, (amount / rate(k), rate(k)))
            line = graph.upsert_node(
                ctx,
                kind="invoice_line",
                natural_key=f"bench:line:{doc}",
                attrs={
                    "line_number": 1,
                    "description": ITEM,
                    "quantity": str(qty),
                    "unit_price": str(price),
                    "amount": str(amount),
                },
                at=at,
            )
            _edge(ctx, graph, line, inv["id"], "part_of", "system_record", at)
