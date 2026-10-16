"""GST and input tax credit (FR-26): lookup -> status -> ITC."""

from __future__ import annotations

from . import Context, register

ITC = "Input tax credit on the invoice depends on the supplier being active and filing."


def _lookup(ctx: Context) -> str:
    return ctx.first("provider_evidence")


@register("GST-INACTIVE", "GST-UNVERIFIED", "GST-NONFILER")
def registry(ctx: Context) -> dict:
    code, d = ctx.finding["type"], ctx.detail
    if code == "GST-INACTIVE":
        middle = ctx.step(
            f"The registry reports GSTIN {d('gstin')} as {d('status')}.",
            _lookup(ctx),
        )
        next_action = (
            "Hold payment until the supplier's registration is active again."
        )
    elif code == "GST-NONFILER":
        middle = ctx.step(
            f"GSTIN {d('gstin')} has missed {d('missed_periods')} return periods.",
            _lookup(ctx),
        )
        next_action = (
            "Ask the supplier to file; weigh the ITC risk before paying."
        )
    else:
        reason = ctx.details.get("reason")
        middle = ctx.step(
            f"GSTIN {d('gstin')} fails its checksum."
            if reason == "checksum"
            else f"The registry could not confirm GSTIN {d('gstin')} ({reason or 'unknown'}).",
            ctx.txn if reason == "checksum" else _lookup(ctx),
        )
        next_action = (
            "Correct the GSTIN or retry the lookup; held until confirmed."
        )
    return {
        "why": [
            ctx.step("The supplier's GSTIN was looked up for this invoice."),
            middle,
            ctx.step(ITC),
        ],
        "caused_by": [
            ctx.vendor_actor(
                f"{ctx.vendor_name} (likely a lapsed or mis-keyed registration)"
            ),
            ctx.engine_actor(),
        ],
        "next_action": next_action,
        "would_change": [
            "A registry lookup returning an active, filing supplier.",
            "A corrected GSTIN on the vendor master.",
        ],
    }


@register("GST-NEW-LARGE-FIRST")
def new_large(ctx: Context) -> dict:
    d = ctx.detail
    return {
        "why": [
            ctx.step(
                f"The supplier was first seen {d('vendor_age_days')} days before this invoice.",
                ctx.first("vendor"),
            ),
            ctx.step(
                f"The invoice is far above the usual amount (median {d('median')})."
            ),
        ],
        "caused_by": [
            ctx.vendor_actor(f"{ctx.vendor_name} (new; likely untested)"),
            ctx.engine_actor(),
        ],
        "next_action": "Verify the supplier and the order before paying a large first invoice.",
        "would_change": [
            "A purchase order for the amount.",
            "A longer history with this supplier.",
        ],
    }


@register("GST-SHARED-IDENTITY")
def shared(ctx: Context) -> dict:
    keys = ", ".join(
        str(k).replace("_", " ") for k in ctx.details.get("shared") or []
    )
    others = list(ctx.of_kind("vendor"))
    return {
        "why": [
            ctx.step(
                f"Two suppliers with different GSTINs share {keys or 'an identity key'}.",
                others[0] if others else ctx.txn,
            ),
            *[
                ctx.step(
                    f"The other supplier: {ctx.attr(n, 'name') or 'unnamed'}.",
                    n,
                )
                for n in others[1:2]
            ],
            ctx.step(
                "One business behind two registrations is a classic ITC fraud pattern."
            ),
        ],
        "caused_by": [
            ctx.vendor_actor(
                f"{ctx.vendor_name} (likely the same business under two GSTINs)"
            ),
            ctx.engine_actor(),
        ],
        "next_action": "Ask the supplier which registration is the real counterparty.",
        "would_change": [
            "Distinct bank accounts, PANs and domains on the two masters."
        ],
    }


@register("GST-ITC-RISK")
def itc_risk(ctx: Context) -> dict:
    signals = ", ".join(
        str(s).replace("risk_", "").replace("_", " ")
        for s in ctx.details.get("signals") or []
    )
    return {
        "why": [
            ctx.step(
                f"Risk signals on the supplier: {signals or 'see the linked findings'}.",
                _lookup(ctx),
            ),
            ctx.step(
                f"The tax on this invoice x factor {ctx.detail('factor')} = {ctx.money} of ITC at risk."
            ),
        ],
        "caused_by": [ctx.vendor_actor(ctx.vendor_name), ctx.engine_actor()],
        "next_action": "Weigh the ITC at risk before paying; consider withholding the tax component.",
        "would_change": ["An active, filing supplier at the next lookup."],
    }
