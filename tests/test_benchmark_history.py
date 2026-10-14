"""The round-trip gate: what the generator injected, the graph must link."""

import json

import pytest

from benchmark.history import eval as history_eval
from benchmark.history import generate


def test_generator_is_deterministic_for_a_seed(tmp_path):
    a = generate.generate(7, tmp_path / "a")
    b = generate.generate(7, tmp_path / "b")
    assert a == b
    assert (tmp_path / "a" / "ap_invoice.csv").read_bytes() == (
        tmp_path / "b" / "ap_invoice.csv"
    ).read_bytes()
    assert a.counts["ap_invoice"] >= 200 and a.counts["vendor"] == 60
    manifest = json.loads((tmp_path / "a" / "manifest.json").read_text())
    assert manifest["expected_links"][0].keys() >= {
        "invoice",
        "vendor",
        "po",
        "grn",
    }
    assert manifest["faults"] == []


def test_round_trip_meets_wave0_gates_on_sqlite(tmp_path, scratch_db):
    data = tmp_path / "data"
    generate.generate(7, data)
    out = history_eval.run(scratch_db("bench.db"), data)
    assert out["gates"] == {
        "invoice_vendor_coverage": True,
        "po_backed_coverage": True,
    }
    assert out["invoice_vendor_coverage"] >= 0.95
    assert out["po_backed_coverage"] >= 0.80
    assert out["contract_clause_coverage"] is None
    assert out["not_measurable_until"]["contract_clause_coverage"] == "wave1"
    assert out["passed"] is True


def test_a_dropped_vendor_file_fails_the_vendor_gate(tmp_path, scratch_db):
    data = tmp_path / "data"
    generate.generate(7, data)
    (data / "vendor.csv").write_text("external_id,vendor_key,gstin\n")
    out = history_eval.run(scratch_db("bench2.db"), data)
    assert out["passed"] is False
    assert out["gates"]["invoice_vendor_coverage"] is False


def test_cli_exits_nonzero_on_a_failed_gate(tmp_path, scratch_db):
    data = tmp_path / "data"
    generate.generate(7, data)
    (data / "grn.csv").write_text(
        "external_id,doc_number,po_number,record_date,quantity\n"
    )
    with pytest.raises(SystemExit) as stop:
        history_eval.main(
            [
                "--db",
                scratch_db("bench3.db"),
                "--data",
                str(data),
                "--out",
                str(tmp_path / "runs"),
            ]
        )
    assert stop.value.code == 1
    assert list((tmp_path / "runs").glob("*.json"))
