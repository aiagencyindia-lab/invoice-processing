"""Document provenance (FR-22): authenticity components, approved twins,
edits after issue and vendor onboarding documents."""

from __future__ import annotations

from ..model import Actor
from . import Context, register

WEAK = 0.5


def _numbers(comps) -> dict:
    """The numeric components; v1 nodes have four, v2 seven."""
    if not isinstance(comps, dict):
        return {}
    return {
        k: float(v) for k, v in comps.items() if isinstance(v, (int, float))
    }


def _weakest(comps) -> str | None:
    weak = sorted(
        ((k, v) for k, v in _numbers(comps).items() if v < WEAK),
        key=lambda kv: kv[1],
    )
    if not weak:
        return None
    return "Weakest: " + ", ".join(f"{k} {v:.2f}" for k, v in weak) + "."


@register("PROV-SUSPECT")
def suspect(ctx: Context) -> dict:
    comps = ctx.details.get("components") or {}
    score = ctx.details.get("score")
    version = ctx.first("document_version")
    parts = ", ".join(f"{k} {v:.2f}" for k, v in _numbers(comps).items())
    weakest = _weakest(comps)
    return {
        "why": [
            ctx.step(
                f"Authenticity score {float(score):.2f}."
                if isinstance(score, (int, float))
                else "A low authenticity score.",
                version,
            ),
            ctx.step(f"Components: {parts or 'not recorded'}.", version),
            *([ctx.step(weakest, version)] if weakest else []),
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


@register("PROV-KYC-SUSPECT")
def kyc_suspect(ctx: Context) -> dict:
    version = ctx.first("document_version")
    subkind = str(ctx.detail("subkind", "onboarding")).replace("_", " ")
    score = ctx.details.get("score")
    weakest = _weakest(ctx.details.get("components"))
    name = ctx.attr(ctx.txn, "name") or "the vendor"
    return {
        "why": [
            ctx.step(
                f"The vendor's {subkind} document scores "
                + (
                    f"{float(score):.2f} on authenticity."
                    if isinstance(score, (int, float))
                    else "low on authenticity."
                ),
                version,
            ),
            *([ctx.step(weakest, version)] if weakest else []),
            ctx.step(
                "Below the tenant's suspect threshold; the vendor is flagged, not held."
            ),
        ],
        "caused_by": [
            Actor(
                "vendor",
                f"{name} (the document may be edited or not issued by the registry)",
                ctx.txn,
            ),
            ctx.engine_actor(),
        ],
        "next_action": (
            "Verify the document at the issuing registry, or ask the vendor "
            "for the original, before paying them."
        ),
        "would_change": [
            "The registry's own copy, or a document matching the vendor's earlier ones.",
            "A document with a text layer, consistent metadata and no later saves.",
        ],
    }


@register("PROV-EDITED")
def edited(ctx: Context) -> dict:
    version = ctx.first("document_version")
    saves = ctx.detail("incremental_updates")
    modified, issued = (
        ctx.details.get("mod_date"),
        ctx.details.get("invoice_date"),
    )
    producer = ctx.details.get("producer")
    why = [
        ctx.step(
            f"The document was saved on top of itself {saves} times after "
            "it was first written.",
            version,
        )
    ]
    if modified and issued:
        why.append(
            ctx.step(
                f"Last saved {str(modified)[:10]}, after the invoice date {issued}.",
                version,
            )
        )
    if producer:
        why.append(ctx.step(f"Last written by {producer}.", version))
    why.append(ctx.step("An insight: the invoice is flagged, not held."))
    return {
        "why": why,
        "caused_by": [
            ctx.vendor_actor(
                f"{ctx.vendor_name} or whoever handled the file after it was issued"
            ),
            ctx.engine_actor(),
        ],
        "next_action": (
            "Ask the vendor for the invoice as first generated and compare "
            "the amounts and bank details."
        ),
        "would_change": [
            "The document as first generated, with no later saves.",
        ],
    }
