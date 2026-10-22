"""One question every governed write asks first: may this agent do this?

`evaluate_action` runs every structural rule (`rules.RULES`) and behavioural
detector (`behaviour.RULES`) and decides by the worst: any BLOCK blocks, any
REVIEW allows with review, else ALLOW. Rules are pure over the action, the
agent, its mandate and the ledger; the guard and the import route record the
decision with `record.record`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from . import behaviour, rules

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Action:
    kind: str
    subject_table: str
    subject_ref: str
    vendor_key: str | None = None
    category: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    transaction_node: str | None = None
    # The email that uploaded the document or proposed the change.
    initiated_by: str | None = None
    # Who already decided earlier steps of this invoice's approval chain.
    prior_deciders: tuple[str, ...] = ()
    vendor_verified: bool | None = None
    vendor_verified_by: str | None = None
    # The document and email text, loaded only if a detector asks.
    text: Callable[[], str] | None = None
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class Reason:
    rule: str
    type: str
    severity: str  # BLOCK | REVIEW
    message: str


@dataclass(frozen=True)
class Decision:
    decision: str
    reasons: tuple[Reason, ...]
    explanation: str
    action_id: str | None = None
    agent_id: str | None = None


def evaluate_action(
    ctx,
    action: Action,
    *,
    agent: dict,
    mandate: dict | None,
    store,
    settings: dict,
    now: datetime,
    mode: str = "advisory",
    decider=None,
) -> Decision:
    reasons: list[Reason] = []
    for rule in rules.RULES + behaviour.RULES:
        try:
            reason = rule(action, agent, mandate, store, settings, now, decider)
        except Exception as exc:
            # Fail closed only where it bites (program plan B8).
            logger.warning(
                "agent_rule_failed", extra={"error": type(exc).__name__}
            )
            reason = Reason(
                "rule-failed",
                "AGENT-OUT-OF-MANDATE",
                "BLOCK" if mode == "enforcing" else "REVIEW",
                "A check could not run.",
            )
        if reason is not None:
            reasons.append(reason)
    severities = {r.severity for r in reasons}
    decision = (
        "BLOCK"
        if "BLOCK" in severities
        else "ALLOW-WITH-REVIEW"
        if reasons
        else "ALLOW"
    )
    # Plain text: the API returns it as JSON and every page escapes it.
    explanation = " ".join(r.message for r in reasons)
    return Decision(
        decision, tuple(reasons), explanation, agent_id=agent.get("id")
    )
