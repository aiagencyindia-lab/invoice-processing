"""Two faults the benchmark never planted: the vendor master holds a different
account, then a different IFSC, from what a real extraction printed. Both must
hold the invoice for review."""

import json
from pathlib import Path

import pytest

from invoice_processing.po_matching import normalize_vendor
from invoice_processing.shared_libraries.acting import general_invoice_agent
from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    Phase3ValidatorAgent,
)

RUN = Path("benchmark/runs/after-ecb9bbd/raw/A.jsonl")

pytestmark = pytest.mark.skipif(
    not RUN.exists(), reason="stored benchmark run not present"
)


def _extraction() -> dict:
    # A record whose payee name already equals the vendor name once normalised
    # and which prints no IBAN, so the only signal a planted fault can raise is
    # bank_mismatch and the exact-signal assertions mean something.
    for line in RUN.read_text().splitlines():
        inv = json.loads(line).get("extracted") or {}
        if (
            inv.get("bank_account_number")
            and inv.get("bank_code")
            and not inv.get("bank_iban")
            and inv.get("vendor_name")
            and normalize_vendor(inv.get("bank_account_name"))
            == normalize_vendor(inv["vendor_name"])
        ):
            return inv
    pytest.skip("no stored record with a clean payee name and no IBAN")


def _hold(tmp_path, monkeypatch, vendor_overrides: dict) -> tuple[dict, dict]:
    inv = dict(_extraction())
    vendor = {
        "name": inv["vendor_name"],
        "tax_id": inv["vendor_tax_id"],
        "bank_account_name": inv["vendor_name"],
        "bank_account_number": inv["bank_account_number"],
        "bank_code": inv["bank_code"],
        "bank_verified_at": "2025-01-01T00:00:00+00:00",
        "last_bank_change_at": "2025-01-01T00:00:00+00:00",
        **vendor_overrides,
    }
    monkeypatch.setitem(
        general_invoice_agent._config_store,
        "CONFIG",
        {
            **general_invoice_agent.DEFAULT_CONFIG,
            "duplicate_check_enabled": False,
            "vendor_master_records": [vendor],
        },
    )
    extraction = {
        "invoice": {
            **inv,
            "_routing": "AUTO",
            "_evidence": {"_summary": {"fields_needing_review": []}},
        }
    }
    (tmp_path / "02_extraction.json").write_text(json.dumps(extraction))
    out = Phase3ValidatorAgent(seen_invoices=lambda exclude_case_id="": []).run(
        tmp_path, extraction, {"decision": "CONTINUE"}
    )
    step = next(v for v in out["validations"] if v["step"] == "3.4")
    return step, extraction


def test_changed_account_holds(tmp_path, monkeypatch):
    step, extraction = _hold(
        tmp_path, monkeypatch, {"bank_account_number": "000000000001"}
    )
    assert step["signals"] == ["bank_mismatch"]
    assert extraction["invoice"]["_routing"] == "REVIEW"


def test_changed_ifsc_holds(tmp_path, monkeypatch):
    step, extraction = _hold(
        tmp_path, monkeypatch, {"bank_code": "HDFC0000001"}
    )
    assert step["signals"] == ["bank_mismatch"]
    assert extraction["invoice"]["_routing"] == "REVIEW"


def test_the_real_master_passes(tmp_path, monkeypatch):
    step, extraction = _hold(tmp_path, monkeypatch, {})
    assert (
        step["passed"] is True and extraction["invoice"]["_routing"] == "AUTO"
    )
