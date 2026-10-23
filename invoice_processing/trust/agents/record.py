"""The one writer of an agent decision: the ledger row, the finding, the
`agent.action.blocked` event and the lineage edge. `person` writes the one
decision about a person (U9): a finding alone.

Ordered, not atomic (program plan B13): the finding and graph stores open
their own sessions. A crash after the ledger row leaves a decision with no
finding; the decision the guard returned does not depend on it.
"""

from __future__ import annotations

from datetime import datetime

from ...db.models import AgentAction
from ...db.session import tenant_session
from ..findings.model import Citation, FindingSpec
from ..webhooks import outbox
from . import ENGINE, ENGINE_VERSION
from .policy import Action, Decision

RULE_VERSION = "agents-rules-1"


def _reasons(decision: Decision) -> list[dict]:
    return [
        {
            "rule": r.rule,
            "type": r.type,
            "severity": r.severity,
            "message": r.message,
        }
        for r in decision.reasons
    ]


def record(
    ctx,
    decision: Decision,
    action: Action,
    *,
    agent: dict,
    mandate: dict | None,
    store,
    findings,
    graph,
    mode: str,
    source: str,
    refused: bool,
    now: datetime,
) -> str:
    """Write the decision; return the ledger row's id. `refused` is whether
    the action was actually stopped (enforcing, inline): only then does
    `agent.action.blocked` go out."""
    if not agent.get("graph_node"):
        # Every finding cites the agent's node; a store built without a
        # graph has none. Build `AgentStore(url, graph=GraphStore(url))`.
        raise ValueError(
            "The agent has no graph node: give AgentStore a graph."
        )
    reasons = _reasons(decision)
    with tenant_session(ctx.tenant_id, store.url) as s:
        action_id = store.record_action(
            s,
            ctx,
            agent_id=agent["id"],
            mandate_id=(mandate or {}).get("id"),
            mandate_version=(mandate or {}).get("version"),
            action=action.kind,
            subject_table=action.subject_table,
            subject_ref=action.subject_ref,
            transaction_node=action.transaction_node,
            vendor_key=action.vendor_key,
            category=action.category,
            amount=action.amount,
            currency=action.currency,
            decision=decision.decision,
            reasons=reasons,
            explanation=decision.explanation[:1000],
            mode=mode,
            source=source,
            occurred_at=action.occurred_at or now,
            request_id=ctx.run_id or None,
        )
    finding_id = None
    if decision.decision != "ALLOW" and source != "dryrun":
        citations = [Citation(agent["graph_node"])]
        if action.transaction_node:
            citations.append(Citation(action.transaction_node))
        finding_id, _ = findings.upsert(
            ctx,
            FindingSpec(
                # The first reason of the severity that decided it (B6).
                type=next(
                    r.type
                    for r in decision.reasons
                    if r.severity
                    == ("BLOCK" if decision.decision == "BLOCK" else "REVIEW")
                ),
                transaction_node=action.transaction_node or agent["graph_node"],
                dedupe_key=f"agent_action:{action_id}",
                impact=None,
                currency=action.currency,
                confidence=None,
                engine_version=ENGINE_VERSION,
                rule_version=RULE_VERSION,
                model_version="",
                summary=f"{agent['name']}: {decision.explanation}"[:500],
                details={
                    "engine": ENGINE,
                    "mode": mode,
                    "source": source,
                    "hold": False,
                    "action": action.kind,
                    "action_id": action_id,
                    "agent_id": agent["id"],
                    "decision": decision.decision,
                    "reasons": reasons,
                    "subject_ref": action.subject_ref,
                },
                citations=tuple(citations),
            ),
        )
    if finding_id is not None or refused:
        with tenant_session(ctx.tenant_id, store.url) as s:
            s.get(AgentAction, action_id).finding_id = finding_id
            if refused:
                outbox.emit(
                    s,
                    ctx.tenant_id,
                    "agent.action.blocked",
                    {
                        "agent_id": agent["id"],
                        "agent_name": agent["name"],
                        "owner": agent["owner"],
                        "action": action.kind,
                        "subject_ref": action.subject_ref,
                        "decision": decision.decision,
                        "reasons": reasons,
                        "explanation": decision.explanation,
                        "action_id": action_id,
                        "finding_id": finding_id,
                    },
                    now=now,
                )
    if (
        decision.decision != "BLOCK"
        and source == "inline"
        and action.transaction_node
        and graph is not None
    ):
        graph.upsert_edge(
            ctx,
            src=agent["graph_node"],
            dst=action.transaction_node,
            kind="acted_on",
            source_system=ENGINE,
            provenance="system_record",
            attrs={"action_id": action_id, "action": action.kind},
            confidence=1.0,
            observed_at=now,
        )
    return action_id


def person(
    ctx,
    decision: Decision,
    action: Action,
    *,
    agent: dict,
    principal,
    findings,
    mode: str,
) -> str:
    """A person refused because their own agent already decided this chain
    (U9). The person acted, not the agent: no ledger row (it would count
    against the agent's caps and velocity) and no `agent.action.blocked`.
    One finding per invoice and person, citing the agent and the invoice."""
    citations = [Citation(agent["graph_node"])]
    if action.transaction_node:
        citations.append(Citation(action.transaction_node))
    finding_id, _ = findings.upsert(
        ctx,
        FindingSpec(
            type=decision.reasons[0].type,
            transaction_node=action.transaction_node or agent["graph_node"],
            dedupe_key=f"owner_chain:{action.subject_ref}:{principal.user_id}",
            impact=None,
            currency=action.currency,
            confidence=None,
            engine_version=ENGINE_VERSION,
            rule_version=RULE_VERSION,
            model_version="",
            summary=decision.explanation[:500],
            details={
                "engine": ENGINE,
                "mode": mode,
                "source": "inline",
                "hold": False,
                "action": action.kind,
                "agent_id": agent["id"],
                "person": principal.email,
                "decision": decision.decision,
                "reasons": _reasons(decision),
                "subject_ref": action.subject_ref,
            },
            citations=tuple(citations),
        ),
    )
    return finding_id
