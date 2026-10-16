"""The fallback for a type without a template: the summary, the registry's
description and each citation's excerpt. Cites nothing it was not given."""

from __future__ import annotations

from ...findings.model import TYPES
from . import Context

NEXT = "Review the cited evidence and accept or reject."


def generic(ctx: Context) -> dict:
    why = [ctx.step(TYPES.get(ctx.finding["type"]) or ctx.finding["summary"])]
    for node_id in ctx.cited:
        excerpt = ctx.excerpt(node_id)
        kind = (ctx.nodes.get(node_id) or {}).get("kind") or "node"
        why.append(
            ctx.step(
                f"Cited {kind.replace('_', ' ')}: {excerpt}"
                if excerpt
                else f"Cited {kind.replace('_', ' ')}.",
                node_id,
            )
        )
    return {
        "what_happened": ctx.finding["summary"],
        "why": why,
        "caused_by": [ctx.vendor_actor(ctx.vendor_name), ctx.engine_actor()],
        "next_action": NEXT,
        "would_change": ["Evidence that contradicts the cited nodes."],
    }
