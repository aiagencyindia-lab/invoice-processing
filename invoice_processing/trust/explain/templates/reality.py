"""Reality checks (FR-23): what was invoiced -> what the evidence shows ->
the document that would settle it."""

from __future__ import annotations

from ..model import Actor
from . import Context, register

# What would corroborate each kind of supply.
MISSING = {
    "goods": "A goods receipt (GRN) or proof of delivery (POD) for the quantity invoiced.",
    "services": "Approved timesheets or tickets for the hours billed.",
    "subscription": "A seat report for the period showing the seats billed.",
}
REQUEST = "Raise an evidence request"
_RESULT = {
    "CONTRADICTED": "The evidence falls short beyond tolerance: payment is held in enforcing mode.",
    "PARTIAL": "The evidence falls a little short, within three times the tolerance.",
    "NO_EVIDENCE": "Nothing on record corroborates what was invoiced.",
}


def _reality(ctx: Context) -> str:
    return ctx.first("reality_check")


def _provider_actor(ctx: Context) -> list[Actor]:
    node = ctx.first("provider_evidence", default="")
    if not node:
        return []
    name = ctx.attr(node, "provider") or "provider"
    capability = ctx.attr(node, "capability") or "lookup"
    return [Actor("provider", f"{name} ({capability})", node)]


@register("REAL-CONTRADICTED", "REAL-PARTIAL", "REAL-NO-EVIDENCE")
def band(ctx: Context) -> dict:
    node = _reality(ctx)

    def get(key):
        return ctx.attr(node, key) or ctx.detail(key)

    kind, unit = get("kind"), get("unit")
    expected, found, result = get("expected"), get("found"), get("result")
    what = (
        f"Invoiced {expected} {unit}; no evidence on record corroborates it."
        if result == "NO_EVIDENCE"
        else f"Invoiced {expected} {unit}; the evidence on record shows {found} {unit}."
    )
    evidence = [n for n in ctx.others() if n != node][:3]
    return {
        "what_happened": what,
        "why": [
            ctx.step(
                f"The invoice was checked as {kind}: {expected} {unit} invoiced.",
                node,
            ),
            *[
                ctx.step(
                    f"Evidence on record: {(ctx.nodes.get(n) or {}).get('kind', 'node').replace('_', ' ')}.",
                    n,
                )
                for n in evidence
            ],
            ctx.step(_RESULT.get(result, ctx.finding["summary"])),
        ],
        "caused_by": [
            *_provider_actor(ctx),
            ctx.vendor_actor(
                f"{ctx.vendor_name} (likely billed ahead of delivery)"
                if result != "NO_EVIDENCE"
                else ctx.vendor_name
            ),
            ctx.engine_actor(),
        ],
        "next_action": f"{REQUEST} for the missing proof; pay what the evidence supports.",
        "would_change": [
            MISSING.get(kind, "Evidence of what was supplied."),
            "A corrected invoice for what was supplied.",
        ],
    }


@register("REAL-PRICE-JUMP", "REAL-VOLUME-SPIKE")
def plausibility(ctx: Context) -> dict:
    d = ctx.detail
    line = ctx.first("invoice_line")
    if ctx.finding["type"] == "REAL-PRICE-JUMP":
        what = f"Line {d('line_number')} is priced {d('unit_price')} against the vendor's usual {d('median')}."
        would = [
            "A contract rate clause or amendment covering the new price.",
            "A credit note for the difference.",
        ]
        ask = "a justification of the new price"
    else:
        what = f"Line {d('line_number')} bills {d('quantity')} against the vendor's usual {d('median')}."
        would = [
            MISSING["goods"],
            "A purchase order for the larger volume.",
        ]
        ask = "proof of the volume supplied"
    return {
        "what_happened": what,
        "why": [
            ctx.step(what, line),
            ctx.step(
                f"Robust z-score {d('z')} over {d('history')} earlier lines from this vendor."
            ),
            ctx.step(f"The difference prices at {ctx.money}."),
        ],
        "caused_by": [ctx.vendor_actor(ctx.vendor_name), ctx.engine_actor()],
        "next_action": f"{REQUEST} for {ask} before paying.",
        "would_change": would,
    }


@register("REAL-SHUTDOWN")
def shutdown(ctx: Context) -> dict:
    billed = ctx.details.get("billed") or ["?", "?"]
    shut = ctx.details.get("shutdown") or ["?", "?"]
    what = (
        f"Billed for {billed[0]} to {billed[1]}, while the business was shut "
        f"from {shut[0]} to {shut[1]}."
    )
    return {
        "what_happened": what,
        "why": [
            ctx.step(what),
            ctx.step(
                "Work billed for a shutdown is unlikely to have happened."
            ),
        ],
        "caused_by": [ctx.vendor_actor(ctx.vendor_name), ctx.engine_actor()],
        "next_action": f"{REQUEST} for proof the work was done during the shutdown.",
        "would_change": [
            "A site log or approved timesheet dated within the shutdown.",
            "A corrected service period on the invoice.",
        ],
    }
