"""Two faults the benchmark never planted: the vendor master holds a different
account, then a different IFSC, from what a real extraction printed. Both must
hold the invoice for review."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from invoice_processing.shared_libraries.acting import general_invoice_agent
from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    Phase3ValidatorAgent,
)

# One extraction from the stored benchmark run (see "source" in the file),
# committed because the runs themselves are gitignored -- read from there, this
# test skipped in CI and the planted faults were never checked where it matters.
# Chosen by the rule the test needs: a payee that already equals the vendor once
# normalised and no IBAN, so the only signal a planted fault can raise is
# bank_mismatch and the exact-signal assertions mean something.
FIXTURE = Path(__file__).parent / "fixtures" / "bank_check" / "planted.json"


def _extraction() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["extracted"]


def _hold(
    tmp_path, monkeypatch, vendor_overrides: dict, config: dict | None = None
) -> tuple[dict, dict]:
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
            **(config or {}),
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


def test_zero_recent_days_means_zero_not_thirty(tmp_path, monkeypatch):
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    changed = {"last_bank_change_at": yesterday}
    step, _ = _hold(tmp_path, monkeypatch, changed)
    assert step["signals"] == ["recent_bank_change"]
    step, _ = _hold(
        tmp_path, monkeypatch, changed, {"bank_change_recent_days": 0}
    )
    assert step["passed"] is True
