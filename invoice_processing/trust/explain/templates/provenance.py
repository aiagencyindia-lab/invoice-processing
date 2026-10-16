"""Document provenance (FR-22): authenticity components and approved twins."""

from __future__ import annotations

from . import Context, register


@register("PROV-SUSPECT")
def suspect(ctx: Context) -> dict:
    comps = ctx.details.get("components") or {}
    score = ctx.details.get("score")
    version = ctx.first("document_version")
    parts = ", ".join(
        f"{k} {float(v):.2f}"
        for k, v in comps.items()
        if isinstance(v, (int, float))
    )
    return {
        "why": [
            ctx.step(
                f"Authenticity score {float(score):.2f}."
                if isinstance(score, (int, float))
                else "A low authenticity score.",
                version,
            ),
            ctx.step(f"Components: {parts or 'not recorded'}.", version),
            ctx.step(
                "Below the tenant's suspect threshold; the invoice is flagged, not held."
            ),
        ],
        "caused_by": [
            ctx.vendor_actor(
                f"{ctx.vendor_name} (likely an unusual channel or a re-generated document)"
            ),
            ctx.engine_actor(),
        ],
        "next_action": "Compare the document with earlier ones from this vendor before approving.",
        "would_change": [
            "More history for this vendor's template.",
            "A document with a text layer and consistent metadata.",
        ],
    }


@register("PROV-TAMPER")
def tamper(ctx: Context) -> dict:
    twins = [n for n in ctx.of_kind("invoice") if n != ctx.txn]
    why = [ctx.step(f"This copy: {ctx.excerpt(ctx.txn) or 'submitted'}.")]
    why += [
        ctx.step(
            f"Approved earlier: {ctx.excerpt(n) or ctx.attr(n, 'invoice_number') or 'an approved twin'}.",
            n,
        )
        for n in twins[:5]
    ]
    why.append(
        ctx.step(
            f"Same invoice number, different document bytes ({ctx.detail('approved_twins', 1)} approved twin(s)): payment of {ctx.money} is held."
        )
    )
    return {
        "why": why,
        "caused_by": [
            ctx.vendor_actor(
                f"the sender claiming to be {ctx.vendor_name} (likely an altered re-submission)"
            ),
            *ctx.person_actors(),
            ctx.engine_actor(),
        ],
        "next_action": "Open both documents side by side; reject the altered copy.",
        "would_change": [
            "A credit note and a corrected invoice under a new number."
        ],
    }
