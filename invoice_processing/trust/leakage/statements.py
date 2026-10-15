"""Supplier statements against our books: credits, double payments, disputes.

Sign convention: a statement line with a negative amount is a credit to us;
a positive one is an invoice the vendor says it issued.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from ...duplicates import normalize_number
from ..findings.model import FindingSpec
from .contracts import ENGINE_VERSION
from .view import View, cents, cite, money

CONFIDENCE = 0.8


def _spec(code, node, impact, summary, details, citations) -> FindingSpec:
    return FindingSpec(
        type=code,
        transaction_node=node["id"],
        dedupe_key=f"{code}:{node['natural_key']}",
        impact=None if impact is None else cents(impact),
        currency=(node["attrs"].get("currency") or "INR")
        if impact is not None
        else None,
        confidence=CONFIDENCE,
        engine_version=ENGINE_VERSION,
        rule_version=f"FR-20.{code.lower()}-1",
        model_version="none",
        summary=summary,
        details=details,
        citations=tuple(citations),
    )


def findings(ectx, view: View) -> list[FindingSpec]:
    invoices, known = defaultdict(set), defaultdict(set)
    for inv in view.invoices:
        number = normalize_number(inv["attrs"].get("invoice_number"))
        invoices[view.vendor(inv["id"])].add(number)
        known[view.vendor(inv["id"])].add(number)
    for p in view.of_kind["payment"]:
        known[view.vendor(p["id"])].add(
            normalize_number(p["attrs"].get("doc_number"))
        )
    out: list[FindingSpec] = []
    for s in view.of_kind["vendor_statement_line"]:
        ven, a = view.vendor(s["id"]), s["attrs"]
        number, amount = (
            normalize_number(a.get("doc_number")),
            money(a.get("amount")),
        )
        if ven is None or not number or amount is None:
            continue
        excerpt = f"{a.get('doc_number')} {a.get('amount')}"
        if amount < 0 and number not in known[ven]:
            out.append(
                _spec(
                    "STMT-UNAPPLIED-CREDIT",
                    s,
                    -amount,
                    "The vendor's statement shows a credit we never applied.",
                    {"doc_number": a.get("doc_number")},
                    [cite(s, excerpt)],
                )
            )
        elif amount > 0 and number not in invoices[ven]:
            out.append(
                _spec(
                    "STMT-DISPUTED",
                    s,
                    None,
                    "The vendor's statement lists an invoice we have no record of.",
                    {"doc_number": a.get("doc_number")},
                    [cite(s, excerpt)],
                )
            )
    for inv in view.invoices:
        amount = money(inv["attrs"].get("amount"))
        paid = [
            p for p in view.payments(inv) if money(p["attrs"].get("amount"))
        ]
        total = sum((money(p["attrs"]["amount"]) for p in paid), Decimal(0))
        if amount is None or amount <= 0 or not paid or total <= amount:
            continue
        out.append(
            _spec(
                "STMT-DOUBLE-PAID",
                inv,
                total - amount,
                "This invoice was paid more than its amount.",
                {"paid": str(total), "payments": [p["id"] for p in paid]},
                [cite(inv), *(cite(p) for p in paid)],
            )
        )
    return out
