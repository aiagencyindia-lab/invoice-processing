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
