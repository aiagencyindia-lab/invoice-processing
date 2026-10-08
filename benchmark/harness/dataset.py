"""The synthetic dataset, read-only, and its masters in the app's shapes.

Nothing here is ever written back. Ground truth is the source of truth for
scoring; the masters are what an administrator would load into a tenant.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DATASET = Path(
    os.environ.get("BENCHMARK_DATASET") or REPO / "benchmark" / "dataset"
)

# check_id -> ground-truth key, in pipeline order.
CHECKS: list[tuple[str, str]] = [
    ("1.1", "extraction_success"),
    ("1.2", "customer_verification"),
    ("1.3", "tax_compliance"),
    ("1.4", "work_authorization"),
    ("1.5", "single_document"),
    ("2.1", "line_items_present"),
    ("2.2", "po_validation"),
    ("3.1", "duplicate_detection"),
    ("3.2", "tax_id_validation"),
    ("3.3", "future_date_check"),
    ("4.1", "total_verification"),
    ("4.2", "line_sum_validation"),
    ("4.3", "authorization_hours_check"),
    ("4.4", "tax_rate_verification"),
]
CHECK_NAME = dict(CHECKS)
COMPANIES = ("A", "B", "C")


@dataclass(frozen=True)
class Submission:
    invoice_id: str
    company_id: str
    vendor_id: str
    seq: int
    filename: str
    pdf_path: Path
    sha256: str
    is_duplicate: bool
    duplicate_of: str
    record: dict  # the ground_truth.json record


def _rows(name: str) -> list[dict]:
    with open(DATASET / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@cache
def ground_truth() -> dict[str, dict]:
    with open(DATASET / "ground_truth.json", encoding="utf-8") as fh:
        data = json.load(fh)
    return {r["invoice_id"]: r for r in data["records"]}


@cache
def master() -> dict[str, dict]:
    return {r["invoice_id"]: r for r in _rows("master_invoice_dataset.csv")}


@cache
def matrix() -> dict[tuple[str, str], dict]:
    """(invoice_id, check_id) -> the long-format expectation row."""
    return {
        (r["invoice_id"], r["check_id"]): r
        for r in _rows("validation_test_matrix.csv")
    }


def submissions(company: str) -> list[Submission]:
    gt = ground_truth()
    out = []
    for row in master().values():
        if row["company_id"] != company:
            continue
        out.append(
            Submission(
                invoice_id=row["invoice_id"],
                company_id=company,
                vendor_id=row["vendor_id"],
                seq=int(row["submission_seq"]),
                filename=row["submission_filename"],
                pdf_path=DATASET / row["document_file"],
                sha256=row["sha256"],
                is_duplicate=row["is_duplicate"] == "True",
                duplicate_of=row["duplicate_of"],
                record=gt[row["invoice_id"]],
            )
        )
    # Duplicates always come after their originals; 3.1 depends on this order.
    return sorted(out, key=lambda s: s.seq)


# ---------------------------------------------------------------------------
# Masters, in the shapes the app loads
# ---------------------------------------------------------------------------


def company(company_id: str) -> dict:
    return next(
        r for r in _rows("customer_master.csv") if r["company_id"] == company_id
    )


def vendors(company_id: str) -> dict[str, dict]:
    return {
        r["vendor_id"]: r
        for r in _rows("vendor_master.csv")
        if r["company_id"] == company_id
    }


def vendor_records(company_id: str) -> list[dict]:
    """The supplier list as the tenant would have loaded it, with the bank
    details and billing domain the dataset's vendor master carries: without
    them step 3.4 has nothing to compare and the measurement is of nothing."""
    return [
        {
            "name": v["name"],
            "tax_id": v["gstin"],
            "kind": v["kind"],
            "email_domain": (v.get("email") or "").split("@")[-1].lower()
            or None,
            "bank_account_name": v["name"],
            "bank_account_number": v["account"],
            "bank_code": v["ifsc"],
            # Verified long before the reference date, so the recent-change
            # signal measures invoices, not the seeding.
            "bank_verified_at": "2025-01-01T00:00:00+00:00",
            "last_bank_change_at": "2025-01-01T00:00:00+00:00",
        }
        for v in vendors(company_id).values()
    ]


def po_records(company_id: str, mode: str) -> list[dict]:
    """`legacy`: today's PO record shape, as an admin could load it through
    PUT /api/purchase-orders. `extended`: the fields the AFTER matcher reads.

    `approved_value` is qty x unit price, i.e. ex-tax. The legacy shape has no
    way to say so; that ambiguity is a BEFORE finding, not something to hide.
    """
    by_vendor = vendors(company_id)
    out = []
    for po in _rows("po_master.csv"):
        if po["customer_id"] != company_id:
            continue
        record = {
            "po_number": po["po_number"],
            "vendor_name": by_vendor[po["vendor_id"]]["name"],
            "total_amount": float(po["approved_value"]),
            "currency": po["currency"],
            "status": "open" if po["status"] == "ACTIVE" else "cancelled",
            "amount_invoiced": 0.0,
        }
        if mode == "extended":
            record.update(
                {
                    "customer_name": company(company_id)["legal_name"],
                    "valid_from": po["valid_from"],
                    "valid_to": po["valid_to"],
                    "approved_quantity": float(po["approved_quantity"]),
                    "approved_unit_price": float(po["approved_unit_price"]),
                    "hsn_sac": po["hsn_sac"],
                    "amount_basis": "ex_tax",
                }
            )
        out.append(record)
    return out


def wa_records(company_id: str) -> list[dict]:
    by_vendor = vendors(company_id)
    return [
        {
            "reference": wa["work_authorization_id"],
            "vendor_name": by_vendor[wa["vendor_id"]]["name"],
            "project": wa["project"],
            "approved_hours": float(wa["approved_hours"]),
            "hourly_rate": float(wa["hourly_rate"]),
            "period_start": wa["period_start"],
            "period_end": wa["period_end"],
            "status": wa["status"].lower(),
        }
        for wa in _rows("work_authorization_master.csv")
        if wa["customer_id"] == company_id
    ]


def hsn_rates() -> dict[str, float]:
    with open(
        Path(__file__).with_name("reference") / "hsn_rates.json",
        encoding="utf-8",
    ) as fh:
        return {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
