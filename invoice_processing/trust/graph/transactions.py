"""One shape for every transaction, live or historical (spec §4.2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class Transaction:
    node_id: str
    kind: str
    vendor_key: str | None
    doc_number: str | None
    amount: Decimal | None
    currency: str | None
    date: date | None
    source: Literal["live", "history"]
    ref: tuple[str, str]


def transactions(
    tenant_id: str, *, graph, as_of: datetime | None = None
) -> list[Transaction]:
    vendor_of = {
        e["src"]: e["dst"]
        for e in graph.edges(tenant_id, kind="issued_by", as_of=as_of)
    }
    out = []
    for n in graph.nodes(tenant_id, kind="invoice", as_of=as_of):
        a = n["attrs"]
        out.append(
            Transaction(
                node_id=n["id"],
                kind="invoice",
                vendor_key=vendor_of.get(n["id"]),
                doc_number=a.get("invoice_number") or a.get("doc_number"),
                amount=Decimal(a["amount"]) if a.get("amount") else None,
                currency=a.get("currency"),
                date=date.fromisoformat(a["invoice_date"])
                if a.get("invoice_date")
                else None,
                source="live" if n["ref_table"] == "invoices" else "history",
                ref=(n["ref_table"] or "", n["ref_id"] or ""),
            )
        )
    return out
