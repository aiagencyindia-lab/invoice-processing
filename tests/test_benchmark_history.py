"""The round-trip gate: what the generator injected, the graph must link."""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from benchmark.history import eval as history_eval
from benchmark.history import faults, generate
from invoice_processing.trust import evaluate as ev
from invoice_processing.trust.findings import model as finding_model
from invoice_processing.trust.findings.model import Citation, FindingSpec
from invoice_processing.trust.findings.store import FindingStore


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


def test_unlinked_records_become_bench_import_findings(tmp_path, scratch_db):
    data = tmp_path / "data"
    generate.generate(7, data)
    (data / "vendor.csv").write_text("external_id,vendor_key,gstin\n")
    url = scratch_db("bench4.db")
    out = history_eval.run(url, data)
    findings = FindingStore(url).list("bench-history", type="BENCH-IMPORT")
    assert len(findings) == out["counts"]["ap_invoice"] == out["unlinked"]
    first = FindingStore(url).get("bench-history", findings[0]["id"])
    assert first["citations"] and first["status"] == "detected"


def fake_module(impact_scale="1"):
    def inject(rng, rows, faults, *, start, months):
        inv = rows["ap_invoice"][0]
        faults.append(
            {
                "type": "T-BENCH",
                "doc_number": inv["doc_number"],
                "vendor_key": inv["vendor_key"],
                "expected_impact": inv["amount"],
            }
        )
        rows["bank_line"].append(
            {
                "external_id": "B1",
                "record_date": "2025-02-01",
                "amount": "-10",
                "currency": "INR",
                "counterparty": "X",
                "bank_reference": "UTR1",
            }
        )

    return SimpleNamespace(
        __name__="benchmark.history.faults.fake",
        inject=inject,
        GATES={
            "T-BENCH": {"precision": 1.0, "recall": 1.0, "impact_error": 0.0}
        },
        seed_graph=None,
        provider_scripts={},
    )


def bench_runner(data, scale="1"):
    """A fake engine that knows the answers from the test, never from eval."""
    manifest = json.loads((data / "manifest.json").read_text())
    wanted = {f["doc_number"] for f in manifest["faults"]}

    def run(ectx):
        return [
            FindingSpec(
                type="T-BENCH",
                transaction_node=t.node_id,
                dedupe_key=f"T-BENCH:{t.node_id}",
                impact=t.amount * Decimal(scale),
                currency="INR",
                confidence=1.0,
                engine_version="t",
                rule_version="r",
                model_version="none",
                summary="bench",
                citations=(Citation(t.node_id),),
            )
            for t in ectx.transactions()
            if t.doc_number in wanted
        ]

    return run


def test_fault_modules_are_discovered_and_inject_rows(tmp_path, monkeypatch):
    assert all(
        m.__name__.startswith("benchmark.history.faults.")
        for m in faults.modules()
    )
    monkeypatch.setattr(faults, "modules", lambda: [fake_module()])
    manifest = generate.generate(7, tmp_path / "d")
    [fault] = manifest.faults
    assert fault["type"] == "T-BENCH"
    lines = (tmp_path / "d" / "bank_line.csv").read_text().splitlines()
    assert lines[0].startswith("external_id") and len(lines) == 2
    vendor_header = (tmp_path / "d" / "vendor.csv").read_text().splitlines()[0]
    assert {"pan", "bank_account", "bank_code"} <= set(vendor_header.split(","))


def test_eval_runs_engines_and_scores_per_type(
    tmp_path, scratch_db, monkeypatch
):
    monkeypatch.setattr(faults, "modules", lambda: [fake_module()])
    finding_model.register_type("T-BENCH", "bench", engine="leakage")
    data = tmp_path / "d"
    generate.generate(7, data)
    monkeypatch.setattr(ev, "RUNNERS", {"leakage": bench_runner(data)})
    out = history_eval.run(scratch_db("bench5.db"), data)
    assert out["types"]["T-BENCH"] == {
        "expected": 1,
        "found": 1,
        "precision": 1.0,
        "recall": 1.0,
        "impact_error": 0.0,
    }
    assert out["gates"]["T-BENCH.precision"] is True and out["passed"] is True


def test_a_wrong_impact_fails_the_impact_gate(
    tmp_path, scratch_db, monkeypatch
):
    monkeypatch.setattr(faults, "modules", lambda: [fake_module()])
    finding_model.register_type("T-BENCH", "bench", engine="leakage")
    data = tmp_path / "d"
    generate.generate(7, data)
    monkeypatch.setattr(
        ev, "RUNNERS", {"leakage": bench_runner(data, scale="2")}
    )
    out = history_eval.run(scratch_db("bench6.db"), data)
    assert out["types"]["T-BENCH"]["impact_error"] == 1.0
    assert (
        out["gates"]["T-BENCH.impact_error"] is False and out["passed"] is False
    )


def test_type_without_a_module_is_not_gated(tmp_path, scratch_db, monkeypatch):
    monkeypatch.setattr(faults, "modules", lambda: [])
    data = tmp_path / "d"
    generate.generate(7, data)
    out = history_eval.run(scratch_db("bench7.db"), data)
    assert set(out["gates"]) == {
        "invoice_vendor_coverage",
        "po_backed_coverage",
    }
    assert out["types"] == {}
