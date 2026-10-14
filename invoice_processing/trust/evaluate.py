"""trust.evaluate for one processed document: sync its subtree, then run the
engines whose per-tenant mode is not `off`. Wave 0 ships no engine, so this
syncs and reports the modes it would have honoured."""

from __future__ import annotations

from sqlalchemy import select

from ..db.models import Invoice
from ..db.session import tenant_session
from ..ports import RunContext
from .graph.sync import sync_tenant
from .modes import ENGINES, engine_mode


def evaluate(
    ctx: RunContext,
    document_id: str,
    *,
    graph,
    links,
    url: str | None = None,
    settings_store=None,
) -> dict:
    with tenant_session(ctx.tenant_id, url) as s:
        has_invoice = s.scalars(
            select(Invoice.id).where(
                Invoice.tenant_id == ctx.tenant_id,
                Invoice.document_id == document_id,
            )
        ).first()
    if has_invoice is None:
        return {"skipped": "no invoice"}
    counts = sync_tenant(ctx, graph, links, url=url, document_id=document_id)
    modes = {
        name: engine_mode(ctx.tenant_id, name, settings_store)
        for name in ENGINES
    }
    # Engines plug in here from Wave 1:
    # for name, mode in modes.items(): if mode != "off": run(name, mode)
    return {"synced": counts, "engines": modes}
