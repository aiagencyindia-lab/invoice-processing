"""Payment Trust fault family: amount outliers and first payments to
unverified accounts (BEC is proven by the replay harness, not the CSV).

Every vendor already in the history gets a bank account the fake verifier
confirms, as a tenant's verified masters would; the faults are then
* an outlier: up to two open, non-PO invoices per chosen vendor billed at 5x
  the vendor's median (its payment row removed, so it is still to be paid);
* a first payment: a new vendor whose one open invoice goes to an account
  the verifier does not know.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from statistics import median

# Vendors with outliers, and outliers per vendor.
OUTLIERS = 45
PER_VENDOR = 2
FIRST_PAYMENTS = 5
# The verifier knows exactly these accounts: one per vendor row position.
_VERIFIED_SLOTS = 1000


def _account(slot: int) -> str:
    return f"{91_000_000_000 + slot}"


provider_scripts = {
    "bank_verification": {
        _account(i): {"result": "verified", "name_match": 1.0}
        for i in range(_VERIFIED_SLOTS)
    }
}
_GATE = {"precision": 0.9, "recall": 0.8, "impact_error": 0.1}
GATES = {"PAY-ANOMALY": dict(_GATE), "PAY-UNVERIFIED": dict(_GATE)}


def inject(rng, rows, faults, *, start, months) -> None:
    for slot, vendor in enumerate(rows["vendor"][:_VERIFIED_SLOTS]):
        if not vendor.get("bank_account"):
            vendor["bank_account"] = _account(slot)
            vendor["bank_code"] = vendor.get("bank_code") or "HDFC0000001"

    paid = {p["doc_number"] for p in rows["payment"]}
    by_vendor: dict[str, list[dict]] = {}
    for inv in rows["ap_invoice"]:
        by_vendor.setdefault(inv["vendor_key"], []).append(inv)
    # An outlier only where 5x the median is beyond anything the vendor
    # billed before (twice its largest invoice): a vendor whose amounts
    # already span 100x has no "unusual" amount to plant.
    chosen = [
        v
        for v, invs in sorted(by_vendor.items())
        if len(invs) >= 8
        and median(amounts := [Decimal(i["amount"]) for i in invs]) * 5
        > 2 * max(amounts)
    ]
    targets = []
    for vendor_key in rng.sample(chosen, min(OUTLIERS, len(chosen))):
        invs = by_vendor[vendor_key]
        plain = [i for i in invs if not i["po_number"]] or invs
        for target in rng.sample(plain, min(PER_VENDOR, len(plain))):
            # 5x the median of the vendor's untouched invoices.
            base = [Decimal(i["amount"]) for i in invs if i not in targets]
            target["amount"] = str(median(base) * 5)
            targets.append(target)
    drop = {t["doc_number"] for t in targets} & paid
    rows["payment"][:] = [
        p for p in rows["payment"] if p["doc_number"] not in drop
    ]
    for target in targets:
        # What the engine will compute: the median of every other invoice.
        others = [
            Decimal(i["amount"])
            for i in by_vendor[target["vendor_key"]]
            if i is not target
        ]
        impact = Decimal(target["amount"]) - median(others)
        faults.append(
            {
                "type": "PAY-ANOMALY",
                "doc_number": target["doc_number"],
                "vendor_key": target["vendor_key"],
                "expected_impact": str(impact.quantize(Decimal("0.01"))),
            }
        )

    for n in range(FIRST_PAYMENTS):
        name = f"Nova Fabrication P{n}"
        rows["vendor"].append(
            {
                "external_id": f"VPAY{n}",
                "vendor_key": name,
                "gstin": f"29AABCN{5000 + n:04d}B1Z{n}",
                "email_domain": f"novafab{n}.in",
                "bank_account": f"{80_000_000_000 + n}",
                "bank_code": "ICIC0000002",
            }
        )
        day = start + timedelta(days=30 * (months - 1) + rng.randint(0, 27))
        amount = Decimal(rng.randint(50_000, 400_000))
        doc = f"INV-PAY-{n}"
        rows["ap_invoice"].append(
            {
                "external_id": f"IPAY{n}",
                "doc_number": doc,
                "vendor_key": name,
                "amount": str(amount),
                "currency": "INR",
                "record_date": day.isoformat(),
                "po_number": "",
            }
        )
        faults.append(
            {
                "type": "PAY-UNVERIFIED",
                "doc_number": doc,
                "vendor_key": name,
                "expected_impact": str(amount),
            }
        )
        # A first payment is also a first-time vendor (no impact: no median).
        faults.append(
            {
                "type": "PAY-ANOMALY",
                "doc_number": doc,
                "vendor_key": name,
                "expected_impact": None,
            }
        )
