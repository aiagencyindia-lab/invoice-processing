"""A model service that is down must not decide invoices.

On 2026-09-24 billing was switched off on the GCP project. Every Vertex call
came back 403, every fallback in the pipeline caught it as though the document
were unreadable, and the keyword classifier -- guessing from text it could not
confirm -- rejected 300 of 300 benchmark invoices as "Document is not a valid
Tax Invoice". In production that is a false rejection sent to a real supplier.

The guard now raises LlmUnavailable for an account refusal or an outage that
outlives its retries, the fallbacks let it through, and `process_invoice`
returns ERROR -- which the worker retries rather than acking.
"""

import shutil
import time
from pathlib import Path

import pytest

from benchmark.harness import run
from invoice_processing.llm_guard import (
    CallBudget,
    LlmCallTimeout,
    LlmUnavailable,
    guarded_generate,
)
from invoice_processing.shared_libraries.acting import (
    general_invoice_agent as gia,
)

BILLING_403 = (
    "403 This API method requires billing to be enabled. Please enable "
    "billing on project #project-x by visiting https://console.developers."
    "google.com/billing/enable?project=project-x then retry."
)
CASE = Path(gia.__file__).parents[2] / "exemplary_data" / "case_001"


class Model:
    def __init__(self, error):
        self.error, self.calls = error, 0

    def generate_content(self, _contents):
        self.calls += 1
        raise self.error


class TestTheGuard:
    def test_a_billing_refusal_is_unavailable_and_not_retried(self):
        model = Model(RuntimeError(BILLING_403))
        with pytest.raises(LlmUnavailable, match="billing"):
            guarded_generate(model, "p", CallBudget())
        assert model.calls == 1

    def test_an_outage_that_outlives_the_retries_is_unavailable(self):
        model = Model(RuntimeError("503 UNAVAILABLE"))
        with pytest.raises(LlmUnavailable):
            guarded_generate(model, "p", CallBudget(deadline_seconds=30))
        assert model.calls == 3

    def test_a_timeout_stays_a_timeout(self):
        """One slow scan is the document's doing and keeps its fallback."""

        class Slow:
            def generate_content(self, _c):
                time.sleep(5)

        with pytest.raises(LlmCallTimeout):
            guarded_generate(
                Slow(), "p", CallBudget(timeout_seconds=0.2, deadline_seconds=1)
            )

    def test_a_document_error_is_still_raised_as_itself(self):
        with pytest.raises(ValueError):
            guarded_generate(Model(ValueError("bad schema")), "p", CallBudget())


def test_the_pipeline_returns_error_not_a_rejection(monkeypatch, tmp_path):
    model = Model(RuntimeError(BILLING_403))
    monkeypatch.setattr(gia, "_model_for", lambda *_a: model)
    monkeypatch.setattr(gia, "OUTPUT_BASE_DIR", tmp_path / "out")
    monkeypatch.setattr(gia._gcp_config, "initialized", True)
    monkeypatch.setattr(gia._gcp_config, "PROJECT_ID", "project-x")
    monkeypatch.setattr(gia._gcp_config, "API_CALL_DELAY_SECONDS", 0.0)
    source = shutil.copytree(CASE, tmp_path / "case_001")

    result = gia.process_invoice(source)

    assert result["decision"] == "ERROR", result
    assert "billing" in result["error"]
    assert model.calls == 1, "stopped at the first refusal"
    assert not (tmp_path / "out" / "case_001" / "08_decision.json").exists()


class TestTheBenchmarkRefusesAnInvalidRun:
    def test_a_failed_preflight_runs_nothing(self, monkeypatch):
        monkeypatch.setattr(run, "preflight", lambda: "LlmUnavailable: billing")
        monkeypatch.setattr(
            run.subprocess,
            "Popen",
            lambda *a, **k: pytest.fail("a stack was started"),
        )
        assert run.main(["--run", "x", "--profile", "p.json"]) == 2

    def test_the_preflight_reports_why(self, monkeypatch):
        monkeypatch.setattr(
            run, "_PREFLIGHT", "raise SystemExit('billing off')"
        )
        assert run.preflight() == "billing off"

    def test_undecided_invoices_are_counted_from_the_scored_run(self, tmp_path):
        (tmp_path / "results.json").write_text(
            '{"decision": {"counts": {"errors_or_no_decision": 7}}}'
        )
        assert run.undecided(tmp_path) == 7
