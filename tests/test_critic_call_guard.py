"""The critic must never hang, and must never spend without a ceiling.

Two runs of the pipeline stalled for 15-25 minutes with the process otherwise
healthy. The cause was not slowness: the critic called
`model.generate_content()` directly from 11 sites, the Vertex SDK's
generate_content accepts no timeout parameter, and so a stalled request blocked
forever. A retry helper existed in the file but nothing ever called it.

Every critic LLM call now goes through `guarded_generate`, which enforces a
per-call wall-clock timeout, retries infrastructure errors with backoff, and
holds a per-case ceiling on both call count and elapsed time.

These are the investigation module's first tests -- it previously had none, at
6000+ lines.
"""

import time

import pytest

from invoice_processing import paths
from invoice_processing.shared_libraries.investigation.investigate_agent_reconst import (
    CallBudget,
    LlmBudgetExceeded,
    LlmCallTimeout,
    RuleDiscoveryEngine,
    guarded_generate,
)


class FakeModel:
    """Stands in for a Vertex model. Records calls; never touches the network."""

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or (lambda n: "ok")
        self.calls = 0

    def generate_content(self, prompt):
        self.calls += 1
        return self.behaviour(self.calls)


class TestTimeout:
    def test_a_hanging_call_does_not_block_forever(self):
        """The regression: a stalled request used to hang the whole pipeline."""

        def hang(_n):
            time.sleep(30)

        model = FakeModel(hang)
        budget = CallBudget(timeout_seconds=0.5, deadline_seconds=10)

        started = time.monotonic()
        with pytest.raises((LlmCallTimeout, LlmBudgetExceeded)):
            guarded_generate(model, "prompt", budget)
        elapsed = time.monotonic() - started

        assert elapsed < 10, (
            f"caller waited {elapsed:.1f}s; timeout not enforced"
        )

    def test_a_fast_call_returns_normally(self):
        model = FakeModel(lambda n: "result")
        assert guarded_generate(model, "prompt", CallBudget()) == "result"


class TestBudget:
    def test_call_count_is_capped(self):
        model = FakeModel(lambda n: "ok")
        budget = CallBudget(max_calls=3)

        for _ in range(3):
            guarded_generate(model, "p", budget)

        with pytest.raises(LlmBudgetExceeded, match="budget exhausted"):
            guarded_generate(model, "p", budget)
        assert model.calls == 3, "no call should be made past the ceiling"

    def test_deadline_is_enforced(self):
        budget = CallBudget(deadline_seconds=0.01)
        time.sleep(0.05)
        with pytest.raises(LlmBudgetExceeded, match="deadline"):
            guarded_generate(FakeModel(), "p", budget)

    def test_budget_tracks_calls_made(self):
        model = FakeModel()
        budget = CallBudget(max_calls=10)
        guarded_generate(model, "p", budget)
        guarded_generate(model, "p", budget)
        assert budget.calls_made == 2


class TestRetry:
    def test_infrastructure_errors_are_retried(self):
        def flaky(n):
            if n < 3:
                raise RuntimeError("503 Service UNAVAILABLE")
            return "recovered"

        model = FakeModel(flaky)
        result = guarded_generate(model, "p", CallBudget(deadline_seconds=30))
        assert result == "recovered"
        assert model.calls == 3

    def test_real_errors_are_not_retried(self):
        """A genuine bug must surface immediately, not be masked by retries."""

        def broken(_n):
            raise ValueError("malformed prompt")

        model = FakeModel(broken)
        with pytest.raises(ValueError, match="malformed prompt"):
            guarded_generate(model, "p", CallBudget())
        assert model.calls == 1, "a non-infrastructure error must not retry"

    def test_retry_gives_up_within_the_deadline(self):
        def always_down(_n):
            raise RuntimeError("503 UNAVAILABLE")

        model = FakeModel(always_down)
        started = time.monotonic()
        with pytest.raises(RuntimeError):
            guarded_generate(model, "p", CallBudget(deadline_seconds=2))
        assert time.monotonic() - started < 10


class TestDiscoveryCacheKey:
    """The cache key used to be sha256(rules_book) alone."""

    def _engine(self, **kwargs):
        defaults = {
            "rules_content": "rules",
            "model_name": "m1",
            "tenant_id": "t1",
        }
        defaults.update(kwargs)
        return RuleDiscoveryEngine(**defaults)

    def test_same_inputs_produce_the_same_key(self):
        assert (
            self._engine()._compute_rules_hash()
            == self._engine()._compute_rules_hash()
        )

    def test_tenants_do_not_share_a_cache_entry(self):
        """Otherwise one tenant's discovered rules get served to another."""
        assert (
            self._engine(tenant_id="t1")._compute_rules_hash()
            != self._engine(tenant_id="t2")._compute_rules_hash()
        )

    def test_model_change_invalidates_the_cache(self):
        assert (
            self._engine(model_name="m1")._compute_rules_hash()
            != self._engine(model_name="m2")._compute_rules_hash()
        )

    def test_rules_book_change_invalidates_the_cache(self):
        assert (
            self._engine(rules_content="a")._compute_rules_hash()
            != self._engine(rules_content="b")._compute_rules_hash()
        )


def test_no_unguarded_generate_content_calls_remain():
    """Every critic LLM call must go through the guard."""
    source = (
        paths.SHARED_LIBS_DIR / "investigation" / "investigate_agent_reconst.py"
    ).read_text()

    assert "self.model.generate_content(" not in source, (
        "an unguarded generate_content call was reintroduced; it has no "
        "timeout and will hang the pipeline"
    )
