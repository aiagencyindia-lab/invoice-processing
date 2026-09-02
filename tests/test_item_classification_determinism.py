"""Item classification must be reproducible.

`temperature=0` does not make Gemini deterministic. Classifying the same
description five times returned CALLOUT twice and LABOUR_AH three times, and
adding a fixed seed only reduced the disagreement (5/6) rather than removing it.

That variance is not cosmetic: it swung eval cases between PASS and FAIL on
identical inputs, so an accuracy gate in CI could not be trusted and
touchless-rate measurements carried noise that looked like real movement.

Classification is now keyword-first (pure logic), then a memoized LLM result,
and only then a live LLM call. These tests assert the deterministic paths do not
touch the network at all.
"""

import json

import pytest

from invoice_processing import paths
from invoice_processing.shared_libraries.acting import (
    general_invoice_agent as gia,
)
from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    TransformerAgent,
)

# Descriptions the keyword map resolves on its own, with expected codes.
# "Emergency call-out - overtime rate" is the one that used to flake: it matches
# both CALLOUT and LABOUR_AH, and the ordered keyword map settles it.
KEYWORD_RESOLVED = [
    ("Emergency call-out - overtime rate", "LABOUR_AH"),
    ("Technician labour (3.0 hrs)", "LABOUR"),
    ("Air filter replacement (x2 units)", "PARTS"),
    ("System calibration & diagnostics", "LABOUR"),
    ("Technician labour - standard rate", "LABOUR"),
]


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """A TransformerAgent with an isolated cache and the LLM disabled.

    Any call to the LLM fails loudly, so a test that expects a deterministic
    path cannot silently pass by hitting the network.
    """
    monkeypatch.setattr(gia, "ITEM_CODE_CACHE_PATH", tmp_path / "cache.json")

    def _no_llm(self, descriptions):
        raise AssertionError(
            f"LLM called for {descriptions!r}; this path must be deterministic"
        )

    monkeypatch.setattr(TransformerAgent, "_classify_items_llm", _no_llm)
    return TransformerAgent()


@pytest.mark.parametrize(("description", "expected"), KEYWORD_RESOLVED)
def test_keyword_resolved_items_never_call_the_llm(
    agent, description, expected
):
    assert agent._classify_items([description]) == [expected]


def test_ambiguous_overtime_callout_is_stable(agent):
    """The regression: this description flipped between runs."""
    results = {
        tuple(agent._classify_items(["Emergency call-out - overtime rate"]))
        for _ in range(10)
    }
    assert results == {("LABOUR_AH",)}


def test_batch_is_deterministic(agent):
    descriptions = [d for d, _ in KEYWORD_RESOLVED]
    expected = [code for _, code in KEYWORD_RESOLVED]
    for _ in range(5):
        assert agent._classify_items(descriptions) == expected


def test_cached_description_does_not_call_the_llm(agent, tmp_path, monkeypatch):
    """A description with no keyword match is resolved from cache, not the LLM."""
    cache_path = gia.ITEM_CODE_CACHE_PATH
    cache_path.write_text(json.dumps({"gasket and seal kit": "PARTS"}))

    assert agent._classify_items(["Gasket and seal kit"]) == ["PARTS"]


def test_cache_lookup_is_normalized(agent):
    """Whitespace and case must not produce a cache miss."""
    gia.ITEM_CODE_CACHE_PATH.write_text(
        json.dumps({"gasket and seal kit": "PARTS"})
    )

    assert agent._classify_items(["  GASKET  and   Seal Kit "]) == ["PARTS"]


def test_llm_result_is_written_to_cache(tmp_path, monkeypatch):
    """An unmatched description is classified once, then remembered."""
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(gia, "ITEM_CODE_CACHE_PATH", cache_path)

    calls = []

    def _fake_llm(self, descriptions):
        calls.append(list(descriptions))
        return ["PARTS"] * len(descriptions)

    monkeypatch.setattr(TransformerAgent, "_classify_items_llm", _fake_llm)
    agent = TransformerAgent()

    assert agent._classify_items(["Gasket and seal kit"]) == ["PARTS"]
    assert agent._classify_items(["Gasket and seal kit"]) == ["PARTS"]

    assert len(calls) == 1, "second call should have hit the cache"
    assert json.loads(cache_path.read_text()) == {
        "gasket and seal kit": "PARTS"
    }


def test_corrupt_cache_does_not_break_classification(agent):
    """A damaged cache file must degrade to keyword matching, not crash."""
    gia.ITEM_CODE_CACHE_PATH.write_text("{ not json")

    assert agent._classify_items(["Technician labour (3.0 hrs)"]) == ["LABOUR"]


def test_empty_input(agent):
    assert agent._classify_items([]) == []


def test_matches_ground_truth_for_every_sample_line_item(tmp_path, monkeypatch):
    """End-to-end accuracy check over the real fixtures.

    The single description keywords cannot resolve ("Gasket and seal kit") is
    seeded into the cache, mirroring the state after one live classification.
    """

    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"gasket and seal kit": "PARTS"}))
    monkeypatch.setattr(gia, "ITEM_CODE_CACHE_PATH", cache_path)

    def _no_llm(self, descriptions):
        raise AssertionError(f"unexpected LLM call for {descriptions!r}")

    monkeypatch.setattr(TransformerAgent, "_classify_items_llm", _no_llm)
    agent = TransformerAgent()

    expected, actual = [], []
    for case in ("case_001", "case_002", "case_005"):
        gt = json.loads(
            (
                paths.EXEMPLARY_DIR / case / "Postprocessing_Data.json"
            ).read_text()
        )
        for item in json.loads(gt["Line Items"]):
            expected.append(item["item_code"])
            actual.append(agent._classify_items([item["description"]])[0])

    assert actual == expected


class TestTheModelFactoryIsShared:
    """Three call sites constructed a model per LLM call, and two other
    modules each kept a class-level singleton. One cache keyed by what
    actually varies replaces all of it."""

    def test_the_same_model_and_temperature_is_reused(self, monkeypatch):
        built = []

        class _Model:
            def __init__(self, name, generation_config=None):
                built.append((name, generation_config))

        monkeypatch.setattr(gia, "GenerativeModel", _Model)
        gia._model_for.cache_clear()

        first = gia._model_for("gemini-2.5-flash", 0)
        second = gia._model_for("gemini-2.5-flash", 0)

        assert first is second
        assert len(built) == 1

    def test_a_different_temperature_is_a_different_model(self, monkeypatch):
        """The object is stateless; the configuration is not."""
        monkeypatch.setattr(
            gia,
            "GenerativeModel",
            lambda name, generation_config=None: object(),
        )
        gia._model_for.cache_clear()

        assert gia._model_for("gemini-2.5-flash", 0) is not gia._model_for(
            "gemini-2.5-flash", 0.7
        )
