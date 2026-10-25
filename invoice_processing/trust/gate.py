"""The payment hold: one question, asked where money is about to move.

An invoice is held while it has an open finding of a hold type whose engine is
`enforcing` for this tenant (spec §3.1.5). "Open" is every status but
`rejected`: a hold lifts when a person rules the finding wrong, or when an
administrator overrides at approval with a reason; resolving a confirmed hold
(`prevented`, `recovered`) does not make the payment safe to release.

Asked from the final approval step and from export. Fails open only when the
trust layer is not deployed. An invoice that an enforcing engine able to hold
payment has not checked yet (evaluation is asynchronous) is held until it has:
"nothing found" and "not looked yet" are different answers.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from ..db.models import Invoice
from ..db.session import tenant_session
from .findings import model as finding_model
from .findings.store import FindingStore
from .modes import ENGINES, HOLD_ENGINES

HELD_UNTIL = "rejected"


def payment_hold(
    tenant_id: str, case_id: str, *, url: str | None = None, settings_store=None
) -> str | None:
    """Why this payment is held, as one sentence, or None."""
    return payment_hold_detail(
        tenant_id, case_id, url=url, settings_store=settings_store
    )["message"]


def payment_hold_detail(
    tenant_id: str, case_id: str, *, url: str | None = None, settings_store=None
) -> dict:
    """The hold and what it is made of: {held, message, holds: [{finding_id,
    type, engine, summary}], waiting: [engine], transaction_node}. An
    override names the finding ids it covered, so export can tell a hold
    raised later from the one an administrator let through."""
    from ..api import service  # noqa: PLC0415 - the gate is asked from the API

    none = {
        "held": False,
        "message": None,
        "holds": [],
        "waiting": [],
        "transaction_node": None,
    }
    graph = service.get_graph_store()
    if graph is None:
        return none
    from .. import tenant_settings  # noqa: PLC0415 - avoids an import cycle

    # One settings read: the workbench asks this on every invoice it opens.
    modes = (
        tenant_settings.effective(tenant_id, settings_store).get(
            "trust_engine_modes"
        )
        or {}
    )
    enforcing = {
        name for name in ENGINES if modes.get(name, "off") == "enforcing"
    }
    if not enforcing:
        return none
    with tenant_session(tenant_id, url) as s:
        row = s.execute(
            select(Invoice.id, Invoice.created_at)
            .where(Invoice.tenant_id == tenant_id, Invoice.case_id == case_id)
            .order_by(Invoice.created_at.desc())
        ).first()
    if row is None:
        return none
    invoice_id, created_at = row
    waiting = sorted(
        (enforcing & HOLD_ENGINES)
        - _checked(graph, tenant_id, invoice_id, created_at)
    )
    node = graph.nodes_by_ref(tenant_id, "invoices", [invoice_id]).get(
        invoice_id
    )
    holding = []
    for f in (
        FindingStore(url).list(tenant_id, transaction_node=node) if node else []
    ):
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
            holding.append(
                {
                    "finding_id": f["id"],
                    "type": f["type"],
                    "engine": engine,
                    "summary": f["summary"],
                }
            )
    holding.sort(key=lambda h: (h["type"], h["finding_id"]))
    message = _message(
        [f"{h['type']} ({h['summary']})" for h in holding], waiting
    )
    return {
        "held": message is not None,
        "message": message,
        "holds": holding,
        "waiting": waiting,
        "transaction_node": node,
    }


def _checked(graph, tenant_id, invoice_id, created_at) -> set[str]:
    from .evaluate import TENANT_RUN, evaluation_key  # noqa: PLC0415

    own = graph.open_node(
        tenant_id, "trust_evaluation", evaluation_key(f"invoices:{invoice_id}")
    )
    done = set((own or {}).get("attrs") or {})
    batch = graph.open_node(tenant_id, "trust_evaluation", TENANT_RUN)
    if created_at is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)  # SQLite
    for name, at in ((batch or {}).get("attrs") or {}).items():
        # A batch run covers the invoices that existed when it ran.
        if created_at is None or datetime.fromisoformat(at) >= created_at:
            done.add(name)
    return done


def _message(holding: list[str], waiting: list[str]) -> str | None:
    parts = []
    if holding:
        parts.append(
            "Payment is held: "
            + "; ".join(sorted(holding))
            + ". Rule the finding wrong to lift it"
        )
    if waiting:
        parts.append(
            "Payment is held until the trust checks for this invoice finish ("
            + ", ".join(waiting)
            + ")"
        )
    if not parts:
        return None
    return (
        ". ".join(parts) + ", or an administrator can override with a reason."
    )
