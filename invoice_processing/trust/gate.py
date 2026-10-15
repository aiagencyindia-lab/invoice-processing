"""The payment hold: one question, asked where money is about to move.

An invoice is held while it has an open finding of a hold type whose engine is
`enforcing` for this tenant (spec §3.1.5). "Open" is every status but
`rejected`: a hold lifts when a person rules the finding wrong, or when an
administrator overrides at approval with a reason; resolving a confirmed hold
(`prevented`, `recovered`) does not make the payment safe to release.

Asked from the final approval step and from export. Fails open when the trust
layer is not deployed or the invoice has not been evaluated yet; the window
between approval and an asynchronous evaluation is a known Wave 2 item.
"""

from __future__ import annotations

from sqlalchemy import select

from ..db.models import Invoice
from ..db.session import tenant_session
from .findings import model as finding_model
from .findings.store import FindingStore
from .modes import ENGINES, engine_mode

HELD_UNTIL = "rejected"


def payment_hold(
    tenant_id: str, case_id: str, *, url: str | None = None, settings_store=None
) -> str | None:
    from ..api import service  # noqa: PLC0415 - the gate is asked from the API

    graph = service.get_graph_store()
    if graph is None:
        return None
    enforcing = {
        name
        for name in ENGINES
        if engine_mode(tenant_id, name, settings_store) == "enforcing"
    }
    if not enforcing:
        return None
    with tenant_session(tenant_id, url) as s:
        invoice_id = s.scalars(
            select(Invoice.id)
            .where(Invoice.tenant_id == tenant_id, Invoice.case_id == case_id)
            .order_by(Invoice.created_at.desc())
        ).first()
    if invoice_id is None:
        return None
    node = graph.nodes_by_ref(tenant_id, "invoices", [invoice_id]).get(
        invoice_id
    )
    if node is None:
        return None
    holding = []
    for f in FindingStore(url).list(tenant_id, transaction_node=node):
        if f["status"] == HELD_UNTIL:
            continue
        # evaluate() stamps engine and hold on every finding, so this process
        # need not import the engine packages that registered the types.
        details = f["details"]
        holds = details.get("hold", f["type"] in finding_model.HOLD_TYPES)
        engine = details.get("engine") or finding_model.TYPE_ENGINE.get(
            f["type"]
        )
        if holds and engine in enforcing:
            holding.append(f"{f['type']} ({f['summary']})")
    if not holding:
        return None
    return (
        "Payment is held: "
        + "; ".join(sorted(holding))
        + ". Rule the finding wrong to lift it, or an administrator can "
        "override with a reason."
    )
