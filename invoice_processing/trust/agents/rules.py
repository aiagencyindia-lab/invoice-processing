"""Structural controls (FR-28.3): each `fn(action, agent, mandate, store,
settings, now, decider) -> Reason | None`, in severity order. Every one is a
BLOCK: these are the controls a mandate grants, not detections (B10).

Pure over the action, the agent, its mandate and the ledger. The tenant is
the agent's own; windows are measured at the action's `occurred_at` (a log
row is judged when it happened, B15).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ... import po_matching

# Module, not name: policy imports this module before it defines Reason.
from . import policy

# The actions that decide something someone else started.
DECISIONS = ("invoice.approve", "invoice.reject", "bank_change.decide")


def _fold(email) -> str:
    return (email or "").strip().casefold()


def _block(rule: str, type_: str, message: str) -> policy.Reason:
    return policy.Reason(rule, type_, "BLOCK", message)


def _code(currency) -> str:
    return str(currency or "").strip().upper()


def _scope(mandate) -> dict:
    return (mandate or {}).get("scope") or {}


def currency(action, agent, mandate, store, settings, now, decider):
    """A mandate names one currency (U10); no FX."""
    if action.kind != "invoice.approve" or mandate is None:
        return None
    want = _code(_scope(mandate).get("currency"))
    got = _code(action.currency)
    if got == want:
        return None
    return _block(
        "currency",
        "AGENT-OUT-OF-MANDATE",
        f"{agent['name']} may approve in {want or 'no currency'} only; "
        f"this invoice is in {got or 'no stated currency'}.",
    )


def mandate_active(action, agent, mandate, store, settings, now, decider):
    if agent.get("status") == "revoked":
        return _block(
            "mandate_active",
            "AGENT-OUT-OF-MANDATE",
            f"{agent['name']} is revoked.",
        )
    if mandate is None:
        return _block(
            "mandate_active",
            "AGENT-OUT-OF-MANDATE",
            f"{agent['name']} has no active mandate.",
        )
    return None


def in_scope(action, agent, mandate, store, settings, now, decider):
    if mandate is None:
        return None  # mandate_active already said so
    scope = _scope(mandate)
    if action.kind not in (scope.get("actions") or ()):
        return _block(
            "in_scope",
            "AGENT-OUT-OF-MANDATE",
            f"{agent['name']}'s mandate does not cover {action.kind}.",
        )
    parties = scope.get("counterparties")
    if parties is not None and action.vendor_key is not None:
        # The same key `vendors.normalized_name` holds, whatever was stored.
        if action.vendor_key not in {
            po_matching.normalize_vendor(str(p)) for p in parties
        }:
            return _block(
                "in_scope",
                "AGENT-OUT-OF-MANDATE",
                f"{agent['name']}'s mandate does not cover this vendor.",
            )
    return None


def self_approval(action, agent, mandate, store, settings, now, decider):
    if action.kind not in DECISIONS:
        return None
    me = _fold(agent.get("principal_email"))
    if me and _fold(action.initiated_by) == me:
        return _block(
            "self_approval",
            "AGENT-SELF-APPROVAL",
            f"{agent['name']} cannot decide what it initiated.",
        )
    return None


def same_owner_chain(action, agent, mandate, store, settings, now, decider):
    """An owner and their agent are one approver, both ways (U9)."""
    if action.kind not in DECISIONS:
        return None
    owner = _fold(agent.get("owner"))
    others = {_fold(d) for d in action.prior_deciders}
    if action.initiated_by:
        others.add(_fold(action.initiated_by))
    others.discard(_fold(agent.get("principal_email")))
    if owner in others:
        return _block(
            "same_owner_chain",
            "AGENT-SAME-OWNER-CHAIN",
            f"{agent['name']}'s owner already took part in this; "
            "an owner and their agent count as one person.",
        )
    siblings = store.by_emails(agent["tenant_id"], others)
    if any(_fold(a.get("owner")) == owner for a in siblings.values()):
        return _block(
            "same_owner_chain",
            "AGENT-SAME-OWNER-CHAIN",
            f"Another agent of {agent['name']}'s owner already took part "
            "in this; agents of one owner count as one person.",
        )
    return None


def _period(at: datetime, period: str) -> tuple[datetime, datetime]:
    """The UTC calendar day or month around `at`. To the period's end, not to
    `at`: an approval recorded a moment later still counts (B17)."""
    # A naive time is UTC, as the ledger stores it, never the host's zone.
    at = at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
    if period == "day":
        start = at.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    start = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def over_limit(action, agent, mandate, store, settings, now, decider):
    """Spend caps count approvals only (B9); every cap is a hard limit."""
    if action.kind != "invoice.approve" or mandate is None:
        return None
    caps = _scope(mandate).get("caps") or {}
    if not caps:
        return None

    def over(what: str) -> policy.Reason:
        return _block(
            "over_limit",
            "AGENT-OVER-LIMIT",
            f"Approving this would take {agent['name']} over its {what} cap.",
        )

    amount = action.amount
    if amount is None:
        return _block(
            "over_limit",
            "AGENT-OVER-LIMIT",
            f"{agent['name']} has spend caps and this invoice's amount is "
            "unknown.",
        )
    amount = Decimal(amount)
    if amount < 0:
        # A credit would lower what the agent has spent.
        return _block(
            "over_limit",
            "AGENT-OVER-LIMIT",
            f"{agent['name']} has spend caps and this invoice's amount is "
            "negative.",
        )
    if caps.get("per_action") is not None:
        if amount > Decimal(str(caps["per_action"])):
            return over("per-action")
    per_period = caps.get("per_period") or {}
    name = per_period.get("period") or "month"
    since, until = _period(action.occurred_at or now, name)
    cur = _code(_scope(mandate).get("currency"))

    def spent(**by) -> Decimal:
        return store.spent(
            agent["tenant_id"],
            agent["id"],
            since=since,
            until=until,
            currency=cur,
            **by,
        )

    if per_period.get("amount") is not None:
        if spent() + amount > Decimal(str(per_period["amount"])):
            return over(f"per-{name}")
    if caps.get("per_vendor") is not None:
        if not action.vendor_key:
            return over("per-vendor (the vendor is unknown)")
        if spent(vendor_key=action.vendor_key) + amount > Decimal(
            str(caps["per_vendor"])
        ):
            return over("per-vendor")
    per_category = caps.get("per_category") or {}
    if per_category and not action.category:
        return over("per-category (the category is unknown)")
    if per_category.get(action.category) is not None:
        if spent(category=action.category) + amount > Decimal(
            str(per_category[action.category])
        ):
            return over(f"{action.category} category")
    return None


def unverified_vendor(action, agent, mandate, store, settings, now, decider):
    tenant = agent["tenant_id"]
    if action.kind == "bank_change.decide":
        # An agent never decides an agent-proposed bank change (B11).
        if action.initiated_by and store.by_emails(
            tenant, [action.initiated_by]
        ):
            return _block(
                "unverified_vendor",
                "AGENT-UNVERIFIED-VENDOR",
                "A bank change an agent proposed needs a person to decide it.",
            )
        return None
    if action.kind not in ("invoice.approve", "invoice.export"):
        return None
    if not action.vendor_key or not store.creators_of(
        tenant, action.vendor_key
    ):
        return None
    by_person = bool(action.vendor_verified) and not (
        action.vendor_verified_by
        and store.by_emails(tenant, [action.vendor_verified_by])
    )
    if by_person:
        return None
    return _block(
        "unverified_vendor",
        "AGENT-UNVERIFIED-VENDOR",
        "An agent created this vendor and no person has verified its bank "
        "details.",
    )


RULES: tuple = (
    currency,
    mandate_active,
    in_scope,
    self_approval,
    same_owner_chain,
    over_limit,
    unverified_vendor,
)
