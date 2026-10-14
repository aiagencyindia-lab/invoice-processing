"""decide(): typed questions in, typed answers out, fallbacks on every failure."""

import logging

import pytest

from invoice_processing.llm_guard import LlmUnavailable
from invoice_processing.trust.decide import (
    Answer,
    FakeDecider,
    Question,
    decide,
)

Q = (
    Question(
        "field",
        "choice",
        "Which field is 'Bill No'?",
        fallback="",
        options=("doc_number", "external_id"),
    ),
    Question("sure", "score", "How sure?", fallback=0.0),
    Question("same", "yes_no", "Same vendor?", fallback=False),
)


def test_valid_answers_carry_probability_and_version():
    answers = decide(
        {"header": "Bill No"},
        Q,
        FakeDecider({"field": "doc_number", "sure": 0.4, "same": True}),
    )
    assert answers[0] == Answer("field", "doc_number", 0.9, "fake", False)
    assert answers[1].value == 0.4 and answers[2].value is True


def test_unavailable_model_returns_every_fallback_marked():
    answers = decide({}, Q, FakeDecider({}, fail=True))
    assert [a.fallback for a in answers] == [True, True, True]
    assert [a.value for a in answers] == ["", 0.0, False]


def test_choice_outside_options_and_missing_id_fall_back():
    answers = decide({}, Q, FakeDecider({"field": "zzz", "sure": 0.5}))
    assert answers[0].fallback and answers[0].value == ""
    assert not answers[1].fallback
    assert answers[2].fallback and answers[2].value is False


def test_score_outside_unit_interval_falls_back():
    assert decide({}, Q[1:2], FakeDecider({"sure": 7}))[0].fallback is True


def test_log_line_names_ids_not_state(caplog):
    with caplog.at_level(
        logging.INFO, logger="invoice_processing.trust.decide"
    ):
        decide({"secret": "GSTIN-123"}, Q, FakeDecider({}, fail=True))
    record = next(r for r in caplog.records if r.getMessage() == "trust_decide")
    assert record.question_ids == ["field", "sure", "same"]
    assert record.fallback_count == 3
    assert "GSTIN-123" not in caplog.text


def test_fake_fail_raises_the_outage_type_internally():
    with pytest.raises(LlmUnavailable):
        FakeDecider({}, fail=True)("prompt")
