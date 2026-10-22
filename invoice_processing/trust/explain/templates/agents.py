"""Agent control (FR-28): which agent did what, every reason the policy gave,
and the person's next move. Cites only the agent node and the subject node
(`record.record` cites exactly those)."""

from __future__ import annotations

from ..model import Actor
from . import Context, register

NEXT = {
    "AGENT-OUT-OF-MANDATE": "Approve a mandate that covers this action, or revoke the agent.",
    "AGENT-SELF-APPROVAL": "Have a person, not the agent that initiated it, decide this step.",
    "AGENT-SAME-OWNER-CHAIN": "Have someone outside this owner's agents decide this step.",
    "AGENT-OVER-LIMIT": "Raise the cap or have a person approve.",
    "AGENT-UNVERIFIED-VENDOR": "Verify the vendor's bank account, then retry.",
    "AGENT-CIRCULAR": "Have a person verify the vendor and approve the invoice.",
    "AGENT-SPLIT": "Review the approvals together as one purchase; have a person approve the total.",
    "AGENT-VELOCITY": "Check what drove the burst; pause the agent if it was not expected.",
    "AGENT-INJECTION": "Read the document; remove the agent's access to this vendor if it acted on the text.",
}
WOULD = {
    "AGENT-OUT-OF-MANDATE": [
        "An approved, current mandate whose scope covers this action and counterparty.",
    ],
    "AGENT-SELF-APPROVAL": [
        "A decision by someone other than whoever initiated the document or change.",
    ],
    "AGENT-SAME-OWNER-CHAIN": [
        "An earlier or later step decided by someone outside this owner's agents.",
    ],
    "AGENT-OVER-LIMIT": [
        "A mandate version with a higher cap, approved by a second admin.",
        "The amount within the cap's remaining room.",
    ],
    "AGENT-UNVERIFIED-VENDOR": [
        "A person verifying the vendor's bank account.",
    ],
    "AGENT-CIRCULAR": [
        "The vendor created by a person, or verified by one before this approval.",
    ],
    "AGENT-SPLIT": [
        "The invoices belonging to separate purchases.",
        "A person approving the combined amount.",
    ],
    "AGENT-VELOCITY": [
        "A scheduled batch the owner expected.",
        "A higher velocity limit for this tenant.",
    ],
    "AGENT-INJECTION": [
        "The flagged text being ordinary wording addressed to a person.",
    ],
}


@register(*NEXT)
def agent(ctx: Context) -> dict:
    node = ctx.first("agent")
    name = ctx.attr(node, "name") or "The agent"
    owner = ctx.attr(node, "owner") or "unknown"
    reasons = [
        r
        for r in ctx.details.get("reasons") or []
        if isinstance(r, dict) and r.get("message")
    ]
    code = ctx.finding["type"]
    why = [ctx.step(r["message"], node) for r in reasons] or [
        ctx.step(ctx.finding["summary"], node)
    ]
    if ctx.txn != node:
        why.append(
            ctx.step(
                f"The action: {ctx.detail('action')}, decided "
                f"{ctx.detail('decision')} in {ctx.detail('mode')} mode.",
            )
        )
    return {
        # The reason that named the finding (`record` picks the first of the
        # deciding severity), not whatever rule ran first.
        "what_happened": next(
            (r["message"] for r in reasons if r.get("type") == code),
            ctx.finding["summary"],
        ),
        "why": why,
        "caused_by": [
            Actor("agent", f"{name} (owner {owner})", node),
            ctx.engine_actor(),
        ],
        "next_action": NEXT[code],
        "would_change": WOULD[code],
        "confidence_note": "A rule on the agent's recorded actions."
        if code != "AGENT-INJECTION"
        else "Text patterns, then a model's yes-or-no; confirm before acting.",
    }
