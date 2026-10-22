"""Behavioural detectors (FR-28.4), same signature as `rules`: each
`fn(action, agent, mandate, store, settings, now, decider) -> Reason | None`.

Detections, not controls: every one asks for review, never blocks (program
plan B10). They read only the ledger, the action and the thresholds, and
measure windows at the action's own time (a log row's `occurred_at`), never
the wall clock. The store never counts a dry run (B15).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from .policy import Reason

# The store's windows are [since, until); a row stamped at the action's own
# instant is part of its burst, so the end is nudged past it.
_TICK = timedelta(microseconds=1)


def _at(action, now):
    return action.occurred_at or now


def _fold(value) -> str:
    return (value or "").strip().casefold()


def circular(action, agent, mandate, store, settings, now, decider):
    if action.kind != "invoice.approve" or not action.vendor_key:
        return None
    creators = store.creators_of(agent["tenant_id"], action.vendor_key)
    for c in creators:
        if c["id"] == agent["id"] or _fold(c["owner"]) == _fold(agent["owner"]):
            who = (
                "it"
                if c["id"] == agent["id"]
                else f"{c['name']}, an agent of the same owner,"
            )
            return Reason(
                "circular",
                "AGENT-CIRCULAR",
                "REVIEW",
                f"{agent['name']} approves an invoice to a vendor that "
                f"{who} created.",
            )
    return None


def _amount(value) -> Decimal | None:
    if isinstance(value, dict):
        value = value.get("amount")
    if value in (None, ""):
        return None
    try:
        out = Decimal(str(value))
    except InvalidOperation:
        return None
    return out if out.is_finite() and out > 0 else None


def _split_threshold(mandate, settings) -> Decimal | None:
    caps = ((mandate or {}).get("scope") or {}).get("caps") or {}
    return _amount(caps.get("per_action")) or _amount(
        settings.get("high_value_threshold")
    )


def split(action, agent, mandate, store, settings, now, decider):
    if action.kind != "invoice.approve" or not action.vendor_key:
        return None
    if action.amount is None:
        return None
    line = _split_threshold(mandate, settings)
    if line is None or action.amount >= line:
        return None
    at = _at(action, now)
    priors = [
        r["amount"]
        for r in store.actions(
            agent["tenant_id"],
            agent_id=agent["id"],
            vendor_key=action.vendor_key,
            action="invoice.approve",
            since=at - timedelta(days=settings["agents.split_window_days"]),
            until=at + _TICK,
            decisions=("ALLOW", "ALLOW-WITH-REVIEW"),
            sources=("inline", "log"),
        )
        if r["amount"] is not None and r["currency"] == action.currency
    ]
    parts = [*priors, action.amount]
    total = sum(parts, Decimal(0))
    if (
        len(parts) < settings["agents.split_min_count"]
        or any(p >= line for p in parts)
        or total <= line
    ):
        return None
    return Reason(
        "split",
        "AGENT-SPLIT",
        "REVIEW",
        f"{agent['name']} approved {len(parts)} invoices from this vendor "
        f"within {int(settings['agents.split_window_days'])} days, each under "
        f"{line} but {total} together.",
    )


def velocity(action, agent, mandate, store, settings, now, decider):
    minutes = settings["agents.velocity_window_minutes"]
    at = _at(action, now)
    n = store.count_since(
        agent["tenant_id"],
        agent["id"],
        since=at - timedelta(minutes=minutes),
        until=at + _TICK,
    )
    if n < settings["agents.velocity_max"]:
        return None
    return Reason(
        "velocity",
        "AGENT-VELOCITY",
        "REVIEW",
        f"{agent['name']} already took {n} actions in the last "
        f"{int(minutes)} minutes.",
    )


RULES: tuple = (circular, split, velocity)
