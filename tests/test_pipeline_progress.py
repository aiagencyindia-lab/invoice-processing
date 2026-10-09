"""The pipeline reports which step it is on.

Without this the UI can only say "processing", which is indistinguishable from
"stuck" -- and a run takes about a minute, long enough for someone to conclude
the upload failed and send the invoice again.
"""

from pathlib import Path

import pytest

from invoice_processing.shared_libraries.acting import general_invoice_agent


class _StubAgent:
    """Stands in for every agent: each one is handed on, none is asserted on."""

    def __init__(self, *args, **kwargs):
        pass

    def run(self, *args, **kwargs):
        return {"decision": "ACCEPT"}


@pytest.fixture
def stub_agents(monkeypatch):
    for name in (
        "ClassifierAgent",
        "ExtractorAgent",
        "Phase1ValidatorAgent",
        "Phase2ValidatorAgent",
        "Phase3ValidatorAgent",
        "Phase4ValidatorAgent",
        "TransformerAgent",
        "OutputGeneratorAgent",
        "AuditLoggerAgent",
    ):
        monkeypatch.setattr(general_invoice_agent, name, _StubAgent)


def test_every_agent_announces_itself_in_order(stub_agents, tmp_path: Path):
    seen = []
    general_invoice_agent._run_pipeline(
        tmp_path, tmp_path, 0.0, on_step=seen.append
    )

    assert [record["step"] for record in seen] == list(range(1, 10))
    assert [record["key"] for record in seen] == [
        key for key, _ in general_invoice_agent.PIPELINE_STEPS
    ]
    assert all(record["total"] == 9 for record in seen)


def test_a_broken_reporter_cannot_fail_a_run(stub_agents, tmp_path: Path):
    """Progress is an observation. It must never become a way to lose an
    invoice that extracted perfectly well."""

    def explode(_record):
        raise RuntimeError("progress sink is down")

    result = general_invoice_agent._run_pipeline(
        tmp_path, tmp_path, 0.0, on_step=explode
    )
    assert result["decision"] == "ACCEPT"


def _classifier_saying(document_type, invoice_count):
    class Classifier(_StubAgent):
        def run(self, source_folder, output_folder):
            return {
                "files": {"scan.pdf": {"type": "pdf"}},
                "summary": {
                    "document_type": document_type,
                    "invoice_count": invoice_count,
                },
            }

    return Classifier


@pytest.mark.parametrize("document_type", ["purchase_order", "goods_receipt"])
def test_a_supporting_document_stops_after_the_classifier(
    stub_agents, monkeypatch, tmp_path: Path, document_type
):
    """A PO or GRN goes to confirmation, not through invoice validation."""
    monkeypatch.setattr(
        general_invoice_agent,
        "ClassifierAgent",
        _classifier_saying(document_type, 0),
    )

    class Refuses(_StubAgent):
        def run(self, *args, **kwargs):
            raise AssertionError("no agent runs after the classifier")

    monkeypatch.setattr(general_invoice_agent, "ExtractorAgent", Refuses)
    seen = []

    result = general_invoice_agent._run_pipeline(
        tmp_path, tmp_path, 0.0, on_step=seen.append
    )

    assert result == {
        "decision": "SUPPORTING",
        "document_type": document_type,
        "pdf": str(tmp_path / "scan.pdf"),
    }
    assert [record["step"] for record in seen] == [1]


def test_a_bundle_with_an_invoice_stays_on_the_invoice_path(
    stub_agents, monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(
        general_invoice_agent,
        "ClassifierAgent",
        _classifier_saying("goods_receipt", 1),
    )
    seen = []

    result = general_invoice_agent._run_pipeline(
        tmp_path, tmp_path, 0.0, on_step=seen.append
    )

    assert result["decision"] == "ACCEPT"
    assert len(seen) == 9
