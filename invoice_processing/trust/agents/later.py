"""The injection detector, after the write (program plan B10: detectors only
ask for review, so none needs to hold up the request). The guard queues one
`trust.agent.injection` job per ledger row; the worker runs `classify`.

Reading the document and asking the model can take as long as the model's
timeout: never inline, never under the agent lock (inline p95 <= 2 s).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from ...ports import RunContext
from .. import thresholds
from . import behaviour, inputs, record
from .policy import Action


def _settings(tenant_id: str) -> dict:
    from ... import tenant_settings  # noqa: PLC0415 - avoids an import cycle

    return thresholds.merged(
        tenant_settings.effective(tenant_id).get("trust_thresholds")
    )


def classify(
    tenant_id: str,
    action_id: str,
    *,
    store,
    findings,
    text: Callable[[], str] | None = None,
    settings: dict | None = None,
    decider=None,
) -> bool:
    """True when the text the agent acted on addresses automated agents and
    the finding was raised. A missing row or agent, or a subject that is not
    an invoice (no document to read), is nothing to do."""
    row = store.action(tenant_id, action_id)
    if row is None:
        return False
    agent = store.get(tenant_id, row["agent_id"])
    table, _, case_id = row["subject_ref"].partition(":")
    if agent is None or (text is None and (table != "invoices" or not case_id)):
        return False
    action = Action(
        kind=row["action"],
        subject_table=row["subject_table"],
        subject_ref=row["subject_ref"],
        vendor_key=row.get("vendor_key"),
        amount=row.get("amount"),
        currency=row.get("currency"),
        transaction_node=row.get("transaction_node"),
        occurred_at=row.get("occurred_at"),
        text=text or inputs.invoice_text(tenant_id, case_id),
    )
    (detector,) = behaviour.DEFERRED
    reason = detector(
        action,
        agent,
        None,
        store,
        settings or _settings(tenant_id),
        datetime.now(UTC),
        decider,
    )
    if reason is None:
        return False
    ctx = RunContext(
        tenant_id=tenant_id, case_id=case_id, run_id="", actor="worker"
    )
    record.injection(ctx, row, agent, reason, store=store, findings=findings)
    return True
