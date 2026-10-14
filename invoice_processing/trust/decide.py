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

from .. import gemini
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

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or os.getenv(
            "GEMINI_FLASH_MODEL", "gemini-2.5-flash"
        )

    def __call__(self, prompt: str) -> tuple[str, str]:
        model = gemini.Model(
            self.model_name,
            temperature=0,
            seed=42,
            response_mime_type="application/json",
        )
        response = guarded_generate(model, prompt)
        return response.text, self.model_name


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
