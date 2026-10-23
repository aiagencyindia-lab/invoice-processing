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
    # The vendor's id when it exists: a mandate may name it (a rename keeps
    # matching). A vendor.create has none yet.
    vendor_id: str | None = None
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
    # Run the deferred detectors on this action once its write has happened.
    defer: bool = False


def evaluate_action(
    ctx,
    action: Action,
    *,
    agent: dict,
    mandate: dict | None,
    store,
    settings: dict,
    now: datetime,
    decider=None,
    skip: tuple = (),
) -> Decision:
    """A structural rule that raises propagates: a control that cannot run
    is the guard's failure (B8), never a finding. A behavioural detector
    that raises is skipped: insights fail open, and it only asks for review.
    Detectors in `skip` run later instead (the guard defers injection to the
    worker: `behaviour.DEFERRED`).
    """
    reasons: list[Reason] = []
    args = (action, agent, mandate, store, settings, now, decider)
    for rule in rules.RULES:
        reason = rule(*args)
        if reason is not None:
            reasons.append(reason)
    for detector in behaviour.RULES:
        if detector in skip:
            continue
        try:
            reason = detector(*args)
        except Exception as exc:
            logger.warning(
                "agent_rule_failed", extra={"error": type(exc).__name__}
            )
            continue
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
