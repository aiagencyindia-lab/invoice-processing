"""Gemini Decision 1: closed-set questions at $0.10 per 1M input, no output cost.

It answers only choice, yes/no ("noul") and score questions with
probabilities -- it cannot write values or text, so extraction stays on
Gemini. On only when GEMINI_API_KEY (an AI Studio key) is set; every caller
keeps its Gemini path as the fallback, so a failure here costs a retry, not a
document.
"""

from __future__ import annotations

import os

import httpx

URL = "https://generativelanguage.googleapis.com/v1beta/decisions"
MODEL = "gemini-decision-1"
PRICE_PER_M_INPUT = 0.10


def enabled() -> bool:
    return (
        bool(os.getenv("GEMINI_API_KEY"))
        and os.getenv("GEMINI_DECISIONS", "on") != "off"
    )


def ask(
    input_: str | list, questions: dict, timeout: float = 30
) -> tuple[dict, int]:
    """POST one request; returns (answers by key, input tokens). Raises on any failure."""
    r = httpx.post(
        URL,
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        json={"model": MODEL, "input": input_, "questions": questions},
        timeout=timeout,
    )
    r.raise_for_status()
    body = r.json()
    answers = body.get("answers") or {}
    if set(answers) != set(questions):
        # A blocked prompt comes back with prompt_feedback and no answers.
        raise ValueError(
            f"decisions: missing answers {body.get('prompt_feedback')}"
        )
    return answers, int((body.get("usage") or {}).get("total_input_tokens", 0))


def cost_usd(input_tokens: int) -> float:
    return input_tokens * PRICE_PER_M_INPUT / 1_000_000
