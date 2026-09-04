"""Is this tax identifier well-formed, and is it *this supplier's*?

A checksum answers the first question. It cannot answer the second, and the
second is the one that catches fraud: an invoice carrying a structurally
perfect tax id belonging to somebody else passes every checksum ever written.
In the benchmark corpus that gap is 18 of the 49 planted tax-id faults --
another vendor's registration, or a valid-looking one that no supplier holds.

The master is per tenant and optional. Without one, step 3.2 keeps doing
exactly what it did before: a checksum, and nothing more. That matters because
a tenant that has not loaded a vendor list must not suddenly have every invoice
rejected as coming from an unknown supplier.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import master_files, po_matching, tax_ids

RECORD_KEY = "vendors"

# Outcomes, most severe first.
NO_TAX_ID = "no_tax_id"
UNKNOWN_VENDOR = "unknown_vendor"
TAX_ID_MISMATCH = "tax_id_mismatch"
MATCHED = "matched"
NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class Vendor:
    name: str
    tax_id: str
    kind: str = ""  # "goods" or "service", where the tenant records it

    @classmethod
    def from_dict(cls, data: dict) -> Vendor:
        return cls(
            name=str(data.get("name") or ""),
            tax_id=str(data.get("tax_id") or ""),
            kind=str(data.get("kind") or "").lower(),
        )


@dataclass(frozen=True)
class VendorMatch:
    outcome: str
    vendor: Vendor | None = None
    evidence: str = ""

    @property
    def matched(self) -> bool:
        return self.outcome in (MATCHED, NOT_CONFIGURED)


class VendorMaster:
    """The tenant's suppliers, indexed by normalised name."""

    def __init__(self, path=None, records: list[dict] | None = None):
        self.path = path
        self._records = records
        self._index: dict[str, Vendor] | None = None

    def _load(self) -> dict[str, Vendor]:
        if self._index is None:
            records = (
                self._records
                if self._records is not None
                else master_files.read_records(self.path, RECORD_KEY)
            )
            self._index = {}
            for record in records:
                vendor = Vendor.from_dict(record)
                key = po_matching.normalize_vendor(vendor.name)
                if key:
                    self._index[key] = vendor
        return self._index

    def __len__(self) -> int:
        return len(self._load())

    def find(self, vendor_name: str) -> Vendor | None:
        return self._load().get(po_matching.normalize_vendor(vendor_name))


def check_vendor_tax_id(invoice: dict, master: VendorMaster) -> VendorMatch:
    """Compare the invoice's tax id against the one this vendor is known by.

    An empty master means the tenant has not loaded one, which is reported as
    `not_configured` -- distinct from "this vendor is unknown", because the
    first is a configuration state and the second is a finding.
    """
    if not len(master):
        return VendorMatch(NOT_CONFIGURED, evidence="No vendor master loaded")

    stated = tax_ids.normalize(invoice.get("vendor_tax_id"))
    name = str(invoice.get("vendor_name") or "")
    vendor = master.find(name)

    if vendor is None:
        return VendorMatch(
            UNKNOWN_VENDOR,
            evidence=(
                f"{name or 'The supplier'} is not in the vendor master, so the "
                f"tax ID {invoice.get('vendor_tax_id') or '(none)'} could not be "
                "confirmed as theirs"
            ),
        )
    if not stated:
        return VendorMatch(
            NO_TAX_ID,
            vendor,
            evidence=f"No tax ID on the invoice; {vendor.name} is registered as {vendor.tax_id}",
        )
    if stated != tax_ids.normalize(vendor.tax_id):
        return VendorMatch(
            TAX_ID_MISMATCH,
            vendor,
            evidence=(
                f"Invoice states {invoice.get('vendor_tax_id')}; "
                f"{vendor.name} is registered as {vendor.tax_id}"
            ),
        )
    return VendorMatch(
        MATCHED,
        vendor,
        evidence=f"Matches the tax ID recorded for {vendor.name}",
    )
