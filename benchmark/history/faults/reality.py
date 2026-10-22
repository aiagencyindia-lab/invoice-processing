"""Reality-check fault family (FR-23): short deliveries, over-billed hours,
seat overruns, price jumps and volume spikes, against evidence the base
history lacks.

The base vendors' PO-backed invoices become goods: one price and one
quantity per vendor, so the vendor's own history never varies by chance and
only a planted line stands out; each receipt is rewritten to that quantity
(a tenth become delivered shipments instead). Non-PO invoices that are the
vendor's only one in their calendar month become services (timesheets and
tickets in that month, `project` = the invoice number) or, one per vendor,
subscriptions (a seat count, `product` = the invoice number). A planted or
uncovered invoice's line is in its manifest entry; `seed_graph` plants every
line, since the history CSVs carry none.

REAL-SHUTDOWN is not gated: it needs `trust_shutdown_periods`, which the
eval's settings do not carry (as leakage's DUP-SPLIT). REAL-VOLUME-SPIKE is
gated, so ten spikes are planted for it to measure. The payment family bills
some contract-vendor invoices at five times their amount, and leakage plants
those lines at the contract price, so they are volume spikes too: expected
here, priced from the rows the way the engine prices them.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median

from . import leakage

GATES = {
    "REAL-CONTRADICTED": {"precision": 0.9, "recall": 0.8, "impact_error": 0.1},
    "REAL-PRICE-JUMP": {"precision": 0.85, "recall": 0.7, "impact_error": 0.1},
    "REAL-VOLUME-SPIKE": {
        "precision": 0.8,
        "recall": 0.7,
        "impact_error": 0.15,
    },
}
SHORT, JUMPS, SPIKES = 30, 20, 10
SERVICES, OVERBILLED, SERVICES_UNCOVERED = 100, 15, 25
SEATS, OVERRUNS, SEATS_UNCOVERED = 40, 10, 8
# Enough history for the plausibility checks (reality.min_history 5) with
# the vendor's own faults left out.
MIN_GOODS = 7
GOODS_DESC = "Industrial supplies"
SERVICE_DESC = "Consulting hours"
SEAT_DESC = "User licence seats"


def goods_price(i: int) -> Decimal:
    return Decimal(100 + 7 * i)


def goods_qty(i: int) -> int:
    return 20 + i % 30


def rate(i: int) -> Decimal:
    return Decimal(1500 + 10 * i)


def hours(i: int) -> int:
    return 40 + 8 * (i % 5)


def seat_price(i: int) -> Decimal:
    return Decimal(800 + i)


def seats(i: int) -> int:
    return 10 + i % 40


def _base(rows) -> dict[str, int]:
    """vendor_key -> index, for the generator's own vendors (V000...)."""
    out = {}
    for v in rows["vendor"]:
        ext = v["external_id"]
        if ext[:1] == "V" and ext[1:].isdigit():
            out[v["vendor_key"]] = int(ext[1:])
    return out


def _fault(code, inv, kind, impact, qty, price, unit) -> dict:
    return {
        "type": code,
        "doc_number": inv["doc_number"],
        "vendor_key": inv["vendor_key"],
        "expected_impact": None if impact is None else str(impact),
        "kind": kind,
        "invoiced_quantity": str(qty),
        "unit_price": str(price),
        "unit": unit,
    }


def _inflated_contract_lines(rows, faults) -> None:
    """Expected spikes on leakage's lines that payment inflated 5x."""
    rated = {f["doc_number"]: f for f in faults if f["type"] == "LEAK-RATE"}
    inflated = {f["doc_number"] for f in faults if f["type"] == "PAY-ANOMALY"}
    by_vendor: dict[str, list[tuple]] = defaultdict(list)
    for inv in rows["ap_invoice"]:
        name = inv["vendor_key"]
        if not name.startswith(leakage.PREFIX + " "):
            continue
        k = int(name.rsplit(" ", 1)[1])
        doc = inv["doc_number"]
        if doc in rated:  # leakage plants the fault's own line
            qty = Decimal(str(rated[doc]["quantity"]))
            price = Decimal(rated[doc]["unit_price"])
        else:
            qty, price = (
                Decimal(inv["amount"]) / leakage.rate(k),
                leakage.rate(k),
            )
        by_vendor[name].append((inv, qty, price))
    for lines in by_vendor.values():
        for inv, qty, price in lines:
            if inv["doc_number"] not in inflated:
                continue
            mid = median(q for other, q, _ in lines if other is not inv)
            if qty > mid:
                faults.append(
                    {
                        "type": "REAL-VOLUME-SPIKE",
                        "doc_number": inv["doc_number"],
                        "vendor_key": inv["vendor_key"],
                        "expected_impact": str(
                            ((qty - mid) * price).quantize(Decimal("0.01"))
                        ),
                    }
                )


def inject(rng, rows, faults, *, start, months) -> None:
    _inflated_contract_lines(rows, faults)
    base = _base(rows)
    taken = {f["doc_number"] for f in faults}  # other families' cases
    grn_of = {g["po_number"]: g for g in rows["grn"]}
    per_month = Counter(
        (i["vendor_key"], i["record_date"][:7]) for i in rows["ap_invoice"]
    )

    # -- goods -------------------------------------------------------------
    by_vendor: dict[str, list[dict]] = defaultdict(list)
    for inv in rows["ap_invoice"]:
        vendor, po = inv["vendor_key"], inv.get("po_number")
        if vendor not in base or not po or po not in grn_of:
            continue
        grn_of[po]["quantity"] = str(goods_qty(base[vendor]))
        if inv["doc_number"] not in taken:
            by_vendor[vendor].append(inv)
    eligible = sorted(
        v for v, invs in by_vendor.items() if len(invs) >= MIN_GOODS
    )
    plan = (["short"] * SHORT) + (["jump"] * JUMPS) + (["spike"] * SPIKES)
    # One fault of a kind per vendor, at most two per vendor: the vendor's
    # median stays its constant, so the expected price is exact.
    used: dict[str, set] = defaultdict(set)
    planted: set[str] = set()
    for what in plan:
        choices = [
            v for v in eligible if what not in used[v] and len(used[v]) < 2
        ]
        vendor = rng.choice(choices)
        used[vendor].add(what)
        inv = rng.choice(
            [i for i in by_vendor[vendor] if i["doc_number"] not in planted]
        )
        planted.add(inv["doc_number"])
        i, grn = base[vendor], grn_of[inv["po_number"]]
        q, p = goods_qty(i), goods_price(i)
        if what == "short":
            short = math.ceil(q * rng.uniform(0.2, 0.5))
            grn["quantity"] = str(q - short)
            faults.append(
                _fault(
                    "REAL-CONTRADICTED", inv, "goods", short * p, q, p, "units"
                )
            )
        elif what == "jump":
            faults.append(
                _fault(
                    "REAL-PRICE-JUMP",
                    inv,
                    "goods",
                    2 * p * q,
                    q,
                    3 * p,
                    "units",
                )
            )
        else:
            big = q * 10
            grn["quantity"] = str(big)
            faults.append(
                _fault(
                    "REAL-VOLUME-SPIKE",
                    inv,
                    "goods",
                    (big - q) * p,
                    big,
                    p,
                    "units",
                )
            )
    shipped = 0
    for vendor in sorted(by_vendor):
        for inv in by_vendor[vendor]:
            if inv["doc_number"] in planted or rng.random() >= 0.1:
                continue
            grn = grn_of[inv["po_number"]]
            rows["grn"].remove(grn)
            shipped += 1
            rows["shipment"].append(
                {
                    "external_id": f"RS{shipped:05d}",
                    "doc_number": f"SHP-{shipped:05d}",
                    "po_number": inv["po_number"],
                    "vendor_key": vendor,
                    "record_date": grn["record_date"],
                    "quantity": grn["quantity"],
                    "status": "delivered",
                    "carrier": "Bluedart",
                }
            )

    # -- services and subscriptions ----------------------------------------
    free = [
        inv
        for inv in rows["ap_invoice"]
        if inv["vendor_key"] in base
        and not inv.get("po_number")
        and inv["doc_number"] not in taken
        and per_month[(inv["vendor_key"], inv["record_date"][:7])] == 1
    ]
    by_vendor = defaultdict(list)
    for inv in free:
        by_vendor[inv["vendor_key"]].append(inv)
    vendors = rng.sample(sorted(by_vendor), SEATS)
    subscribed = set()
    for k, vendor in enumerate(vendors):
        inv = rng.choice(by_vendor[vendor])
        subscribed.add(inv["doc_number"])
        i = base[vendor]
        s, p = seats(i), seat_price(i)
        if k < OVERRUNS:
            count = s - math.ceil(s * 0.4)
            faults.append(
                _fault(
                    "REAL-CONTRADICTED",
                    inv,
                    "subscription",
                    (s - count) * p,
                    s,
                    p,
                    "seat",
                )
            )
        elif k < OVERRUNS + SEATS_UNCOVERED:
            faults.append(
                _fault(
                    "REAL-NO-EVIDENCE", inv, "subscription", None, s, p, "seat"
                )
            )
            continue
        else:
            count = s
        rows["seat_count"].append(
            {
                "external_id": f"RC{k:03d}",
                "vendor_key": vendor,
                "record_date": inv["record_date"],
                "product": inv["doc_number"],
                "quantity": str(count),
            }
        )

    rest = [inv for inv in free if inv["doc_number"] not in subscribed]
    services = rng.sample(rest, SERVICES)
    worked = 0
    for k, inv in enumerate(services):
        i = base[inv["vendor_key"]]
        h, r = hours(i), rate(i)
        if k < OVERBILLED:
            logged = h // 2
            faults.append(
                _fault(
                    "REAL-CONTRADICTED",
                    inv,
                    "services",
                    (h - logged) * r,
                    h,
                    r,
                    "hour",
                )
            )
        elif k < OVERBILLED + SERVICES_UNCOVERED:
            faults.append(
                _fault("REAL-NO-EVIDENCE", inv, "services", None, h, r, "hour")
            )
            continue
        else:
            logged = h
        month = date.fromisoformat(inv["record_date"]).replace(day=1)
        split = (
            [8, logged - 8]
            if k % 3 == 0
            else [logged // 2, logged - logged // 2]
        )
        for n, part in enumerate(split):
            worked += 1
            day = month.replace(day=rng.randint(1, 28)).isoformat()
            if n == 0 and k % 3 == 0:
                rows["ticket"].append(
                    {
                        "external_id": f"RT{worked:05d}",
                        "vendor_key": inv["vendor_key"],
                        "doc_number": f"TCK-{worked:05d}",
                        "record_date": day,
                        "status": "closed",
                        "hours": str(part),
                        "project": inv["doc_number"],
                        "po_number": "",
                    }
                )
            else:
                rows["timesheet"].append(
                    {
                        "external_id": f"RW{worked:05d}",
                        "vendor_key": inv["vendor_key"],
                        "record_date": day,
                        "hours": str(part),
                        "rate": str(rate(i)),
                        "project": inv["doc_number"],
                        "po_number": "",
                        "approved_by": "pm@corp.test",
                    }
                )


def _rows(data_dir: Path, kind: str) -> list[dict]:
    path = Path(data_dir) / f"{kind}.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def seed_graph(ctx, graph, data_dir: Path) -> None:
    """One `invoice_line` per invoice the family planted: the manifest's
    when it has one, else derived from the evidence it was given."""
    at = datetime.now(UTC)
    manifest = json.loads((Path(data_dir) / "manifest.json").read_text())
    fixed = {
        f["doc_number"]: f
        for f in manifest["faults"]
        if f["type"].startswith("REAL-") and "kind" in f
    }
    base = _base({"vendor": _rows(data_dir, "vendor")})
    received = {
        r["po_number"]: r["quantity"]
        for kind in ("grn", "shipment")
        for r in _rows(data_dir, kind)
    }
    worked: dict[str, Decimal] = defaultdict(Decimal)
    for kind in ("timesheet", "ticket"):
        for r in _rows(data_dir, kind):
            worked[r["project"]] += Decimal(r["hours"])
    counted = {
        r["product"]: r["quantity"] for r in _rows(data_dir, "seat_count")
    }
    nodes = {
        n["attrs"].get("doc_number"): n["id"]
        for n in graph.nodes(ctx.tenant_id, kind="invoice")
    }
    for inv in _rows(data_dir, "ap_invoice"):
        doc, vendor = inv["doc_number"], inv["vendor_key"]
        if vendor not in base or doc not in nodes:
            continue
        i = base[vendor]
        if doc in fixed:
            f = fixed[doc]
            kind, qty, price = (
                f["kind"],
                f["invoiced_quantity"],
                f["unit_price"],
            )
        elif inv.get("po_number") in received:
            kind, qty, price = (
                "goods",
                received[inv["po_number"]],
                goods_price(i),
            )
        elif doc in worked:
            kind, qty, price = "services", worked[doc], rate(i)
        elif doc in counted:
            kind, qty, price = "subscription", counted[doc], seat_price(i)
        else:
            continue
        desc, unit = {
            "goods": (GOODS_DESC, "units"),
            "services": (SERVICE_DESC, "hour"),
            "subscription": (SEAT_DESC, "seat"),
        }[kind]
        line = graph.upsert_node(
            ctx,
            kind="invoice_line",
            natural_key=f"bench:reality:line:{doc}",
            attrs={
                "line_number": 1,
                "description": desc,
                "quantity": str(qty),
                "unit_price": str(price),
                "unit": unit,
                "amount": str(Decimal(str(qty)) * Decimal(str(price))),
            },
            at=at,
        )
        graph.upsert_edge(
            ctx,
            src=line,
            dst=nodes[doc],
            kind="part_of",
            source_system="bench",
            provenance="system_record",
            confidence=1.0,
            observed_at=at,
            at=at,
        )
