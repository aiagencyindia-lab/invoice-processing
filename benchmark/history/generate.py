"""A synthetic company's 18 months of AP history, as the files a customer
would export.

Deterministic for a seed. Headers are the import field names so the mapping
proposer links them without a model. Wave 0 injects no faults: the manifest's
`faults` list exists for the engine waves to fill.
"""

from __future__ import annotations

import csv
import json
import random
import string
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

KINDS = ("vendor", "po", "grn", "ap_invoice", "payment")
HEADERS = {
    "vendor": ["external_id", "vendor_key", "gstin", "email_domain"],
    "po": [
        "external_id",
        "doc_number",
        "vendor_key",
        "amount",
        "currency",
        "record_date",
    ],
    "grn": [
        "external_id",
        "doc_number",
        "po_number",
        "record_date",
        "quantity",
    ],
    "ap_invoice": [
        "external_id",
        "doc_number",
        "vendor_key",
        "amount",
        "currency",
        "record_date",
        "po_number",
    ],
    "payment": [
        "external_id",
        "doc_number",
        "vendor_key",
        "amount",
        "currency",
        "record_date",
        "bank_reference",
    ],
}
_FIRST = ("Acme", "Globex", "Initech", "Umbrella", "Stark", "Wayne")
_TRADE = ("Steel", "Tools", "Logistics", "Services", "Foods")


@dataclass(frozen=True)
class Manifest:
    seed: int
    counts: dict
    expected_links: list
    faults: list

    def as_dict(self) -> dict:
        return asdict(self)


def _gstin(rng: random.Random) -> str:
    state = f"{rng.randint(1, 37):02d}"
    pan = (
        "".join(rng.choices(string.ascii_uppercase, k=5))
        + f"{rng.randint(0, 9999):04d}"
        + rng.choice(string.ascii_uppercase)
    )
    return f"{state}{pan}1Z{rng.choice(string.ascii_uppercase + string.digits)}"


def generate(
    seed: int, out: Path, *, months: int = 18, vendors: int = 60
) -> Manifest:
    rng = random.Random(seed)  # noqa: S311 - synthetic data, not secrets
    out.mkdir(parents=True, exist_ok=True)
    start = date(2025, 1, 1)
    rows: dict[str, list[dict]] = {k: [] for k in KINDS}
    links = []

    names = [
        f"{rng.choice(_FIRST)} {rng.choice(_TRADE)} {i}" for i in range(vendors)
    ]
    for i, name in enumerate(names):
        rows["vendor"].append(
            {
                "external_id": f"V{i:03d}",
                "vendor_key": name,
                "gstin": _gstin(rng),
                "email_domain": f"{name.split()[0].lower()}{i}.in",
            }
        )

    po_seq = inv_seq = grn_seq = pay_seq = 0
    for month in range(months):
        for i, name in enumerate(names):
            if rng.random() < 0.35:
                continue  # not every vendor invoices every month
            day = start + timedelta(days=30 * month + rng.randint(0, 27))
            amount = Decimal(rng.randint(5_000, 500_000))
            inv_seq += 1
            inv = {
                "external_id": f"I{inv_seq:05d}",
                "doc_number": f"INV-{i}-{inv_seq}",
                "vendor_key": name,
                "amount": str(amount),
                "currency": "INR",
                "record_date": day.isoformat(),
                "po_number": "",
            }
            link = {
                "invoice": inv["external_id"],
                "vendor": f"V{i:03d}",
                "po": None,
                "grn": None,
            }
            if rng.random() < 0.7:  # PO-backed, received
                po_seq += 1
                grn_seq += 1
                po_no = f"PO-{po_seq:05d}"
                rows["po"].append(
                    {
                        "external_id": f"P{po_seq:05d}",
                        "doc_number": po_no,
                        "vendor_key": name,
                        "amount": str(amount),
                        "currency": "INR",
                        "record_date": (
                            day - timedelta(days=rng.randint(5, 40))
                        ).isoformat(),
                    }
                )
                rows["grn"].append(
                    {
                        "external_id": f"G{grn_seq:05d}",
                        "doc_number": f"GRN-{grn_seq:05d}",
                        "po_number": po_no,
                        "record_date": (
                            day - timedelta(days=rng.randint(0, 4))
                        ).isoformat(),
                        "quantity": str(rng.randint(1, 100)),
                    }
                )
                inv["po_number"] = po_no
                link["po"], link["grn"] = f"P{po_seq:05d}", f"G{grn_seq:05d}"
            rows["ap_invoice"].append(inv)
            links.append(link)
            if rng.random() < 0.9:
                pay_seq += 1
                rows["payment"].append(
                    {
                        "external_id": f"Y{pay_seq:05d}",
                        "doc_number": inv["doc_number"],
                        "vendor_key": name,
                        "amount": str(amount),
                        "currency": "INR",
                        "record_date": (
                            day + timedelta(days=rng.randint(10, 60))
                        ).isoformat(),
                        "bank_reference": f"UTR{rng.randint(10**11, 10**12 - 1)}",
                    }
                )

    for kind in KINDS:
        with (out / f"{kind}.csv").open(
            "w", newline="", encoding="utf-8"
        ) as fh:
            writer = csv.DictWriter(fh, fieldnames=HEADERS[kind])
            writer.writeheader()
            writer.writerows(rows[kind])
    manifest = Manifest(
        seed=seed,
        counts={k: len(v) for k, v in rows.items()},
        expected_links=links,
        faults=[],
    )
    (out / "manifest.json").write_text(
        json.dumps(manifest.as_dict(), indent=2), encoding="utf-8"
    )
    return manifest
