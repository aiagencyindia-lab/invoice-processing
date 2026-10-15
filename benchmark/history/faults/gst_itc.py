"""GST/ITC fault family (FR-26): a cancelled registration, a non-filer, two
GSTINs behind one PAN, and a new vendor whose first invoice is oversized.

History CSVs carry no tax column, so `seed_graph` writes an 18% `tax_amount`
onto this family's invoice nodes (and re-points their edges), and scripts the
fake GSTIN registry from the vendor file — both read from `data_dir`, so a
pre-generated `--data` directory scores the same as a fresh one.
"""

from __future__ import annotations

import csv
import string
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from invoice_processing import tax_ids

PREFIX = "GSTF-"
TAX_RATE = Decimal("0.18")
FACTOR = {
    "CANCELLED": Decimal("1.0"),
    "NONFILER": Decimal("0.5"),
    "TWIN": Decimal("0.3"),
    "NEW": Decimal("0.2"),
}
_GATE = {"precision": 0.95, "recall": 0.9}
GATES = {
    "GST-INACTIVE": dict(_GATE),
    "GST-NONFILER": dict(_GATE),
    "GST-SHARED-IDENTITY": dict(_GATE),
    "GST-NEW-LARGE-FIRST": dict(_GATE),
    "GST-ITC-RISK": {**_GATE, "impact_error": 0.01},
}
# Filled by seed_graph before eval reads it (eval calls seed_graph first).
# ponytail: module state; a function hook in eval would be cleaner.
provider_scripts: dict[str, dict] = {"gstin": {}}


def _pan(rng) -> str:
    letters = "".join(rng.choices(string.ascii_uppercase, k=5))
    return f"{letters}{rng.randint(0, 9999):04d}{rng.choice(string.ascii_uppercase)}"


def _gstin(state: int, pan: str) -> str:
    head = f"{state:02d}{pan}1Z"
    for ch in string.digits + string.ascii_uppercase:
        if tax_ids.validate_gstin(head + ch)[0]:
            return head + ch
    raise ValueError(head)


def _tax(amount) -> Decimal:
    return (Decimal(str(amount)) * TAX_RATE).quantize(Decimal("0.01"))


def inject(rng, rows, faults, *, start: date, months: int) -> None:
    twin_pan = _pan(rng)
    vendors = [
        ("CANCELLED", "Gstf Cancelled Traders", _gstin(27, _pan(rng))),
        ("NONFILER", "Gstf Nonfiler Agencies", _gstin(24, _pan(rng))),
        ("TWIN-N", "Gstf Twin North", _gstin(27, twin_pan)),
        ("TWIN-S", "Gstf Twin South", _gstin(29, twin_pan)),
        ("NEW", "Gstf Newcomer Works", _gstin(33, _pan(rng))),
    ]
    seq = 0
    for role, name, gstin in vendors:
        twin = role.startswith("TWIN")
        rows["vendor"].append(
            {
                "external_id": f"{PREFIX}{role}",
                "vendor_key": name,
                "gstin": gstin,
                "email_domain": f"{role.lower()}.gstf.in",
                "pan": twin_pan if twin else "",
                # The history sync keeps no bank key on vendor nodes yet, so
                # the twins are caught on PAN; the account rides along.
                "bank_account": "50100012345678" if twin else "",
                "bank_code": "HDFC0000001" if twin else "",
            }
        )
        if role == "NEW":
            # First seen late, oversized first, then an ordinary one.
            plan = [(months - 6, Decimal(3_000_000)), (months - 4, None)]
        else:
            plan = [(m, None) for m in sorted(rng.sample(range(months), 3))]
        for k, (month, fixed) in enumerate(plan):
            seq += 1
            amount = fixed or Decimal(rng.randint(5_000, 500_000))
            doc = f"{PREFIX}{seq:04d}"
            rows["ap_invoice"].append(
                {
                    "external_id": doc,
                    "doc_number": doc,
                    "vendor_key": name,
                    "amount": str(amount),
                    "currency": "INR",
                    "record_date": (
                        start + timedelta(days=30 * month + rng.randint(0, 27))
                    ).isoformat(),
                    "po_number": "",
                }
            )
            kind = "TWIN" if twin else role
            signal = {
                "CANCELLED": "GST-INACTIVE",
                "NONFILER": "GST-NONFILER",
                "TWIN": "GST-SHARED-IDENTITY",
                "NEW": "GST-NEW-LARGE-FIRST",
            }[kind]
            if kind == "NEW" and k > 0:
                continue  # the ordinary follow-up invoice is clean
            for code, impact in (
                (signal, None),
                ("GST-ITC-RISK", str(_tax(amount) * FACTOR[kind])),
            ):
                faults.append(
                    {
                        "type": code,
                        "doc_number": doc,
                        "vendor_key": name,
                        "expected_impact": impact,
                    }
                )


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return [
            r for r in csv.DictReader(fh) if r["external_id"].startswith(PREFIX)
        ]


def seed_graph(ctx, graph, data_dir: Path) -> None:
    script = provider_scripts["gstin"]
    for v in _rows(Path(data_dir) / "vendor.csv"):
        role = v["external_id"].removeprefix(PREFIX)
        script[v["gstin"]] = {
            "status": "cancelled" if role == "CANCELLED" else "active",
            "missed_periods": 3 if role == "NONFILER" else 0,
            "registered_on": "2020-04-01",
        }
    tax = {
        r["doc_number"]: _tax(r["amount"])
        for r in _rows(Path(data_dir) / "ap_invoice.csv")
    }
    tid = ctx.tenant_id
    now = datetime.now(timezone.utc)
    for n in graph.nodes(tid, kind="invoice"):
        doc = n["attrs"].get("doc_number")
        if doc not in tax:
            continue
        edges = graph.edges(tid, src=n["id"]) + graph.edges(tid, dst=n["id"])
        new = graph.upsert_node(
            ctx,
            kind=n["kind"],
            natural_key=n["natural_key"],
            ref_table=n["ref_table"],
            ref_id=n["ref_id"],
            attrs={**n["attrs"], "tax_amount": str(tax[doc])},
            natural_keys=n["natural_keys"],
            at=now,
        )
        # A re-versioned node closes its edges; put them back on the new row.
        for e in edges:
            graph.upsert_edge(
                ctx,
                src=new if e["src"] == n["id"] else e["src"],
                dst=new if e["dst"] == n["id"] else e["dst"],
                kind=e["kind"],
                source_system=e["source_system"],
                provenance=e["provenance"],
                confidence=e["confidence"],
                observed_at=datetime.fromisoformat(e["observed_at"]),
                attrs=e["attrs"],
                at=now,
            )
