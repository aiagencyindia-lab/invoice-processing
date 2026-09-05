"""The acting pipeline must not be able to hang on Vertex.

Every extraction call went straight to `model.generate_content`, which takes no
timeout and whose underlying RPC has none either. A stalled request therefore
blocked the worker until the queue lease expired ten minutes later, retried,
and stalled again -- three times, then a dead-lettered invoice. It was observed
twice at 15-25 minutes, and once during the benchmark at 294 seconds for a
single document.

The critic already had the fix (`guarded_generate`), written after the same
symptom and never shared. It is now in `invoice_processing/llm_guard.py` and
both callers use it.
"""

import ast
import time
from pathlib import Path

import pytest

from invoice_processing.llm_guard import (
    CallBudget,
    LlmCallTimeout,
    guarded_generate,
)
from invoice_processing.shared_libraries.acting import general_invoice_agent
from invoice_processing.shared_libraries.investigation import (
    investigate_agent_reconst as critic,
)

GIA_SOURCE = Path(general_invoice_agent.__file__)


class FakeModel:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    def generate_content(self, contents):
        self.calls += 1
        return self.behaviour(self.calls)


class TestTheGuardStopsAHang:
    def test_a_stalled_call_is_abandoned_rather_than_waited_out(self):
        model = FakeModel(lambda _n: time.sleep(30))
        started = time.monotonic()
        with pytest.raises(LlmCallTimeout):
            guarded_generate(
                model,
                "prompt",
                CallBudget(timeout_seconds=0.3, deadline_seconds=2),
            )
        assert time.monotonic() - started < 5

    def test_an_infrastructure_error_is_retried(self):
        def flaky(call_number):
            if call_number == 1:
                raise RuntimeError("503 UNAVAILABLE")
            return "second time lucky"

        model = FakeModel(flaky)
        assert (
            guarded_generate(model, "prompt", CallBudget())
            == "second time lucky"
        )
        assert model.calls == 2

    def test_a_real_error_is_surfaced_immediately(self):
        """A malformed request is not a flaky network. Retrying it three times
        spends three times as much to fail the same way."""
        model = FakeModel(
            lambda _n: (_ for _ in ()).throw(ValueError("bad schema"))
        )
        with pytest.raises(ValueError):
            guarded_generate(model, "prompt", CallBudget())
        assert model.calls == 1


class TestThePipelineUsesIt:
    def _generate_content_callers(self) -> set[str]:
        """Every function in the acting pipeline that calls generate_content."""
        tree = ast.parse(GIA_SOURCE.read_text(encoding="utf-8"))
        callers = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "generate_content"
                ):
                    callers.add(node.name)
        return callers

    def test_no_pipeline_code_calls_vertex_directly(self):
        """The three call sites -- OCR, text, and PDF -- are the whole surface.

        A fourth added later without the guard reintroduces the hang, which is
        what this test exists to catch.
        """
        assert self._generate_content_callers() <= {"_generate"}, (
            "these functions call model.generate_content directly and can hang: "
            f"{sorted(self._generate_content_callers() - {'_generate'})}"
        )

    def test_the_pipeline_calls_the_guard(self):
        source = GIA_SOURCE.read_text(encoding="utf-8")
        assert "guarded_generate" in source

    def test_the_critic_shares_the_same_guard(self):
        """Moved, not copied: a second implementation would drift from this one
        exactly as the first two did."""
        assert critic.guarded_generate is guarded_generate
        assert critic.CallBudget is CallBudget
