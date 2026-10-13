"""Step 3.2's checksum verdict describes the tax ID that 3.2 then compares.

The extractor misread Precision Weld's GSTIN 24OXMPF9713X1Z2 as
240XMPF9713X1Z2 (zero for O). Cleanup reads it back as the page prints it,
but the benchmark replay kept the checksum verdict recorded for the misread,
so 3.2 said "Invalid check character ... Matches the tax ID recorded" and
rejected a clean invoice. Cleaning and checking are one step, used by the
extractor and the replay alike.
"""

from pathlib import Path

from invoice_processing.shared_libraries.acting import (
    general_invoice_agent as gia,
)

REPO = Path(__file__).resolve().parent.parent
PRINTED = "24OXMPF9713X1Z2"
MISREAD = "240XMPF9713X1Z2"
STALE = {
    "valid": False,
    "reason": "Invalid check character: expected 'Q', found '2'",
}


def _on(monkeypatch, **config):
    monkeypatch.setitem(gia._config_store, "CONFIG", config)


def test_the_verdict_is_for_the_cleaned_value(monkeypatch):
    _on(monkeypatch, validate_tax_id_checksum=True, tax_id_format="GSTIN")
    invoice = gia.clean_extraction(
        {"vendor_tax_id": MISREAD, "_tax_id_validation": dict(STALE)},
        pages=[f"GSTIN: {PRINTED}"],
    )
    assert invoice["vendor_tax_id"] == PRINTED
    assert invoice["_tax_id_validation"] == {
        "valid": True,
        "reason": "Valid checksum",
    }


def test_a_real_misprint_still_fails(monkeypatch):
    """Nothing on the page to read it back from: the misread stands, and so
    does the failing checksum."""
    _on(monkeypatch, validate_tax_id_checksum=True, tax_id_format="GSTIN")
    invoice = gia.clean_extraction({"vendor_tax_id": MISREAD}, pages=[])
    assert invoice["vendor_tax_id"] == MISREAD
    assert invoice["_tax_id_validation"]["valid"] is False


def test_no_checksum_configured_leaves_no_verdict(monkeypatch):
    """A recorded verdict must not outlive the setting that produced it."""
    _on(monkeypatch)
    invoice = gia.clean_extraction(
        {"vendor_tax_id": MISREAD, "_tax_id_validation": dict(STALE)},
        pages=[],
    )
    assert "_tax_id_validation" not in invoice


def test_the_replay_cleans_the_way_the_extractor_does():
    source = (REPO / "benchmark" / "harness" / "replay.py").read_text()
    assert "clean_extraction(" in source
    assert "extraction_cleanup.clean(" not in source
