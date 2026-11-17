"""decide(state, questions): the only way a v2 engine asks the model.

Typed questions go in, typed answers with probabilities come out, and every
failure -- outage, timeout, malformed reply, out-of-range value -- becomes the
question's declared fallback, marked as such. The caller decides whether a
fallback means hold (payment) or skip (insight). Nothing here raises for
model trouble, and nothing here logs the state (spec §4.5).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from .. import decisions, gemini
from ..llm_guard import (
    LlmBudgetExceeded,
    LlmCallTimeout,
    LlmUnavailable,
    guarded_generate,
)

logger = logging.getLogger(__name__)

Kind = Literal["choice", "score", "yes_no"]
# prompt -> (json text, model version)
Decider = Callable[[str], tuple[str, str]]


@dataclass(frozen=True)
class Question:
    id: str
    kind: Kind
    text: str
    fallback: Any
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Answer:
    id: str
    value: Any
    probability: float
    model_version: str
    fallback: bool


class _Item(BaseModel):
    id: str
    value: Any
    probability: float = 0.5


class _Reply(BaseModel):
    answers: list[_Item]


_PREAMBLE = (
    "You answer typed questions about a financial record. Reply with JSON "
    '{"answers": [{"id": ..., "value": ..., "probability": 0..1}]}. '
    "For a choice question the value is exactly one of the options; for a "
    "score it is a number from 0 to 1; for a yes_no it is true or false. "
    "Never compute money; never invent facts not in the record.\n"
)


def _prompt(state: dict, questions: Sequence[Question]) -> str:
    asked = [
        {
            "id": q.id,
            "kind": q.kind,
            "question": q.text,
            **({"options": list(q.options)} if q.options else {}),
        }
        for q in questions
    ]
    return (
        _PREAMBLE
        + "Record:\n"
        + json.dumps(state, default=str, sort_keys=True)
        + "\nQuestions:\n"
        + json.dumps(asked)
    )


def _valid(q: Question, value: Any) -> bool:
    if q.kind == "choice":
        return isinstance(value, str) and value in q.options
    if q.kind == "score":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0 <= value <= 1
        )
    return isinstance(value, bool)


class GeminiDecider:
    """Gemini structured output through the existing guarded call."""

    def __init__(self, model_name: str | None = None, budget=None):
        self.model_name = model_name or os.getenv(
            "GEMINI_FLASH_MODEL", "gemini-2.5-flash"
        )
        self.budget = budget

    def __call__(self, prompt: str) -> tuple[str, str]:
        model = gemini.Model(
            self.model_name,
            temperature=0,
            seed=42,
            response_mime_type="application/json",
        )
        response = guarded_generate(model, prompt, self.budget)
        return response.text, self.model_name


_SCORE_LEVELS = ("none", "low", "medium", "high", "certain")


def _ask_decision1(
    state: dict, questions: Sequence[Question]
) -> tuple[str, str]:
    """Gemini Decision 1 asked natively, replied in a Decider's JSON shape."""
    asked = {}
    for i, q in enumerate(questions):
        if q.kind == "choice":
            criteria = {f"o{j}": o for j, o in enumerate(q.options)}
            asked[f"q{i}"] = {
                "type": "choice",
                "instructions": q.text,
                "criteria": criteria,
            }
        elif q.kind == "yes_no":
            asked[f"q{i}"] = {"type": "noul", "instructions": q.text}
        else:
            levels = list(q.options) or list(_SCORE_LEVELS)
            asked[f"q{i}"] = {
                "type": "score",
                "instructions": q.text,
                "criteria": levels,
            }
    answers, _ = decisions.ask(
        "Record:\n" + json.dumps(state, default=str, sort_keys=True), asked
    )
    items = []
    for i, q in enumerate(questions):
        a = answers[f"q{i}"]
        if q.kind == "choice":
            value, p = q.options[int(a["choice"][1:])], a["confidence"]
        elif q.kind == "yes_no":
            value = a["noul"] >= 0.5
            p = a["noul"] if value else 1 - a["noul"]
        else:  # the weighted level index, scaled to 0..1
            value = a["score"] / (len(asked[f"q{i}"]["criteria"]) - 1)
            p = a["confidence"]
        items.append({"id": q.id, "value": value, "probability": p})
    return json.dumps({"answers": items}), decisions.MODEL


class FakeDecider:
    """Scripted answers for tests and fakes; `fail=True` behaves like an outage."""

    def __init__(
        self,
        answers: dict[str, Any],
        probability: float = 0.9,
        fail: bool = False,
    ):
        self.answers, self.probability, self.fail = answers, probability, fail
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> tuple[str, str]:
        self.prompts.append(prompt)
        if self.fail:
            raise LlmUnavailable("fake outage")
        items = [
            {"id": k, "value": v, "probability": self.probability}
            for k, v in self.answers.items()
        ]
        return json.dumps({"answers": items}), "fake"


def decide(
    state: dict,
    questions: Sequence[Question],
    decider: Decider | None = None,
) -> list[Answer]:
    decider = decider or GeminiDecider()
    replies: dict[str, _Item] = {}
    version = "fallback"
    try:
        if decisions.enabled() and isinstance(decider, GeminiDecider):
            try:
                text, version = _ask_decision1(state, questions)
            except Exception as exc:  # Gemini is the fallback
                logger.warning(
                    "trust_decision1_error", extra={"error": type(exc).__name__}
                )
                text, version = decider(_prompt(state, questions))
        else:
            text, version = decider(_prompt(state, questions))
        replies = {
            item.id: item for item in _Reply.model_validate_json(text).answers
        }
    except (
        LlmUnavailable,
        LlmCallTimeout,
        LlmBudgetExceeded,
        ValidationError,
        ValueError,
        gemini.GeminiEmptyResponse,
    ):
        replies = {}
    except Exception as exc:  # the contract is "never raises"
        # The type, never the message: an SDK error can quote the prompt.
        logger.warning(
            "trust_decide_error", extra={"error": type(exc).__name__}
        )
        replies = {}

    answers = []
    for q in questions:
        item = replies.get(q.id)
        if (
            item is not None
            and _valid(q, item.value)
            and 0 <= item.probability <= 1
        ):
            answers.append(
                Answer(
                    q.id, item.value, float(item.probability), version, False
                )
            )
        else:
            answers.append(Answer(q.id, q.fallback, 0.0, version, True))
    logger.info(
        "trust_decide",
        extra={
            "question_ids": [q.id for q in questions],
            "fallback_count": sum(a.fallback for a in answers),
        },
    )
    return answers
