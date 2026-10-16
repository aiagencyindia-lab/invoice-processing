"""Payment Trust (FR-21): provider evidence -> verdict -> hold."""

from __future__ import annotations

from ..model import Actor
from . import Context, register

CALL_BACK = "A call-back to the vendor on a number already on file confirming the account."


def _provider(ctx: Context) -> str:
    return ctx.first("provider_evidence")


def _provider_actor(ctx: Context) -> list[Actor]:
    node = ctx.first("provider_evidence", default="")
    if not node:
        return []
    name = (
        ctx.detail("provider", None) or ctx.attr(node, "provider") or "provider"
    )
    return [Actor("provider", f"{name} (bank verification)", node)]


@register("PAY-UNVERIFIED")
def unverified(ctx: Context) -> dict:
    d = ctx.detail
    if ctx.details.get("reason") == "no_account_on_file":
        why = [
            ctx.step(
                "No bank account is on file for this vendor.",
                ctx.first("vendor"),
            ),
            ctx.step(f"Payment of {ctx.money} is held until one is verified."),
        ]
    else:
        why = [
            ctx.step(
                f"{d('provider')} answered {d('result', 'nothing')} for account "
                f"{d('account')} (status {d('provider_status')}).",
                _provider(ctx),
            ),
            ctx.step(
                f"Only a 'verified' answer releases payment: {ctx.money} is held."
            ),
        ]
    return {
        "why": why,
        "caused_by": [
            *_provider_actor(ctx),
            ctx.vendor_actor(ctx.vendor_name),
            ctx.engine_actor(),
        ],
        "next_action": "Verify the account with the provider or by two-person review of the vendor master.",
        "would_change": ["A verified result from the provider.", CALL_BACK],
    }


@register("PAY-NAME-MISMATCH")
def name_mismatch(ctx: Context) -> dict:
    d = ctx.detail
    return {
        "why": [
            ctx.step(
                f"The bank's name for account {d('account')} matches the vendor "
                f"{float(ctx.details.get('name_match') or 0):.0%}, below the "
                f"{float(ctx.details.get('name_match_min') or 0):.0%} floor.",
                _provider(ctx),
            ),
            ctx.step(
                f"Payment of {ctx.money} is held until the payee is confirmed."
            ),
        ],
        "caused_by": [
            *_provider_actor(ctx),
            ctx.vendor_actor(
                f"{ctx.vendor_name} (likely a changed or impersonated payee)"
            ),
            ctx.engine_actor(),
        ],
        "next_action": "Confirm the account holder with the vendor before paying.",
        "would_change": ["A verified result naming the vendor.", CALL_BACK],
    }


@register("PAY-BEC", "PAY-BEC-SIGNAL")
def bec(ctx: Context) -> dict:
    evidence = ctx.details.get("evidence") or {}
    signals = ctx.details.get("signals") or list(evidence)
    why = [
        ctx.step(
            f"{s.replace('_', ' ')}: {evidence.get(s) or 'seen on this invoice'}."
        )
        for s in signals
    ]
    for node in ctx.of_kind("bank_change")[:1]:
        why.append(
            ctx.step(
                "The payee's bank details changed close to this invoice.", node
            )
        )
    hold = ctx.finding["type"] == "PAY-BEC"
    why.append(
        ctx.step(
            f"{len(signals)} signal(s): payment of {ctx.money} is held."
            if hold
            else "One signal alone does not hold payment; it is noted for review."
        )
    )
    return {
        "why": why,
        "caused_by": [
            ctx.vendor_actor(
                f"the sender claiming to be {ctx.vendor_name} (likely impersonation)"
            ),
            ctx.engine_actor(),
        ],
        "next_action": "Confirm the invoice and bank details with the vendor on a known number before paying."
        if hold
        else "Review the signal; no hold applies.",
        "would_change": [
            CALL_BACK,
            "A sender domain and bank account matching the vendor master.",
        ],
    }


@register("PAY-ANOMALY")
def anomaly(ctx: Context) -> dict:
    d = ctx.detail
    signals = ", ".join(
        s.replace("_", " ") for s in ctx.details.get("signals") or []
    )
    return {
        "why": [
            ctx.step(
                f"Vendor history of {d('history', 0)} transactions, median {d('median', 'unknown')}.",
                ctx.first("vendor"),
            ),
            ctx.step(f"This invoice is unusual: {signals or 'see summary'}."),
            ctx.step(f"Amount above the median: {ctx.money}.")
            if ctx.finding.get("impact")
            else ctx.step("No amount above the median is computed."),
        ],
        "caused_by": [
            ctx.vendor_actor(
                f"{ctx.vendor_name} (likely a one-off or a new pattern)"
            ),
            ctx.engine_actor(),
        ],
        "next_action": "Confirm the amount and timing with the requester; no hold applies.",
        "would_change": [
            "A purchase order or contract covering the amount.",
            "More history for this vendor.",
        ],
    }
