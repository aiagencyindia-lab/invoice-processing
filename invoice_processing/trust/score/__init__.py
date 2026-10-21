"""The Transaction Trust Score (FR-25): six dimensions per invoice, each one
able to drive a hold, a composite that is only advisory. The v2/w3-score
branch fills `compute`; `evaluate()` calls it after the engines ran."""

from __future__ import annotations

SCORE_VERSION = "score-1"


def compute(ectx, report: dict) -> None:
    """Write the invoice's `trust_score` graph node (scaffold: nothing)."""
    return None
