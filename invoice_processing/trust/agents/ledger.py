"""The spend ledger (FR-28.6): what each agent approved and paid, against its
mandate's caps, read from the decision ledger.

Approved = allowed or reviewed `invoice.approve`; exported (paid) = the same
for `invoice.export`, reported but never summed with it (B9). Dry runs never
count. Spend is keyed by currency: a mandate names one (U10), but an imported
log may hold several, and none is converted.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from ...db.models import Agent, AgentAction
from ...db.session import tenant_session
from . import mandates

MAX_ROWS = 200
APPROVE, EXPORT = "invoice.approve", "invoice.export"


def money(value) -> str:
    """One spelling on both dialects: SQLite sums to float-ish, Postgres to
    Numeric(18,4)."""
    q = Decimal(str(value)).quantize(Decimal("0.0001"))
    return format(q.normalize(), "f")


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )


def window(since, until, now: datetime) -> tuple[datetime, datetime]:
    """Defaults: from the start of this UTC month until now."""
    since = (
        _utc(since)
        if since
        else now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    )
    until = _utc(until) if until else now
    if since >= until:
        raise ValueError("since must be before until.")
    return since, until


def _period(kind: str, now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "day":
        return start, start + timedelta(days=1)
    start = start.replace(day=1)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def _counted(tenant_id, since, until):
    return (
        AgentAction.tenant_id == tenant_id,
        AgentAction.source != "dryrun",
        AgentAction.occurred_at >= since,
        AgentAction.occurred_at < until,
    )


def _spent(tenant_id, since, until):
    """The rows that moved money: counted, not blocked, not a write that
    then failed (an unstamped row still counts)."""
    return (
        *_counted(tenant_id, since, until),
        AgentAction.decision != "BLOCK",
        AgentAction.outcome.is_distinct_from("failed"),
        AgentAction.amount.is_not(None),
    )


def _exports(s, tenant_id, since, until, *, agent_id=None):
    """Each invoice's export once, at its latest amount: exporting an invoice
    twice pays it once. Keyed by owner, so the tenant view counts an invoice
    once per owner however many of their agents exported it."""
    q = (
        select(
            Agent.owner,
            AgentAction.subject_ref,
            AgentAction.currency,
            AgentAction.amount,
        )
        .join(Agent, Agent.id == AgentAction.agent_id)
        .where(
            Agent.tenant_id == tenant_id,
            *_spent(tenant_id, since, until),
            AgentAction.action == EXPORT,
        )
        .order_by(AgentAction.occurred_at, AgentAction.created_at)
    )
    if agent_id is not None:
        q = q.where(AgentAction.agent_id == agent_id)
    latest = {}
    for owner, ref, currency, amount in s.execute(q):
        latest[(owner, ref or "")] = (currency or "", Decimal(str(amount)))
    out: dict = defaultdict(lambda: defaultdict(Decimal))
    for (owner, _), (currency, amount) in latest.items():
        out[owner][currency] += amount
    return out


def _action(row: dict) -> dict:
    amount = row.get("amount")
    return {**row, "amount": None if amount is None else money(amount)}


def agent_ledger(
    store, tenant_id: str, agent: dict, *, since, until, now
) -> dict:
    with tenant_session(tenant_id, store.url) as s:
        sums = s.execute(
            select(
                AgentAction.action,
                AgentAction.vendor_key,
                AgentAction.category,
                AgentAction.currency,
                func.sum(AgentAction.amount),
            )
            .where(
                *_spent(tenant_id, since, until),
                AgentAction.agent_id == agent["id"],
                AgentAction.action == APPROVE,
            )
            .group_by(
                AgentAction.action,
                AgentAction.vendor_key,
                AgentAction.category,
                AgentAction.currency,
            )
        ).all()
        exported = _exports(s, tenant_id, since, until, agent_id=agent["id"])
    spend: dict = {}
    for _kind, vendor, category, currency, total in sums:
        cur = spend.setdefault(
            currency or "",
            {
                "approved": Decimal(0),
                "exported": Decimal(0),
                "by_vendor": defaultdict(Decimal),
                "by_category": defaultdict(Decimal),
            },
        )
        amount = Decimal(str(total))
        cur["approved"] += amount
        if vendor:
            cur["by_vendor"][vendor] += amount
        if category:
            cur["by_category"][category] += amount
    for currency, amount in exported.get(agent["owner"], {}).items():
        spend.setdefault(
            currency,
            {
                "approved": Decimal(0),
                "exported": Decimal(0),
                "by_vendor": defaultdict(Decimal),
                "by_category": defaultdict(Decimal),
            },
        )["exported"] += amount
    spend = {
        c: {
            "approved": money(v["approved"]),
            "exported": money(v["exported"]),
            "by_vendor": {
                k: money(a) for k, a in sorted(v["by_vendor"].items())
            },
            "by_category": {
                k: money(a) for k, a in sorted(v["by_category"].items())
            },
        }
        for c, v in sorted(spend.items())
    }
    rows = store.mandates(tenant_id, agent["id"])
    current = store.current_mandate(tenant_id, agent["id"], now)
    mandate = next(
        (
            m
            for m in mandates.annotate(rows, now)
            if current and m["id"] == current["id"]
        ),
        None,
    )
    period = None
    cap = ((mandate or {}).get("scope") or {}).get("caps", {}).get("per_period")
    if cap:
        start, end = _period(cap["period"], now)
        used = store.spent(
            tenant_id,
            agent["id"],
            since=start,
            until=end,
            currency=mandate["scope"]["currency"],
        )
        limit = Decimal(cap["amount"])
        period = {
            "period": cap["period"],
            "currency": mandate["scope"]["currency"],
            "cap": money(limit),
            "used": money(used),
            "remaining": money(max(limit - used, Decimal(0))),
            "since": start.isoformat(),
            "until": end.isoformat(),
        }
    return {
        "agent": agent,
        "mandate": mandate,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "spend": spend,
        "period": period,
        "actions": [
            _action(r)
            for r in store.actions(
                tenant_id,
                agent_id=agent["id"],
                since=since,
                until=until,
                limit=MAX_ROWS,
            )
        ],
        "exceptions": [
            _action(r)
            for r in store.actions(
                tenant_id,
                agent_id=agent["id"],
                since=since,
                until=until,
                decisions=("BLOCK", "ALLOW-WITH-REVIEW"),
                sources=("inline", "log"),
                limit=MAX_ROWS,
            )
        ],
    }


def tenant_ledger(store, tenant_id: str, *, since, until) -> dict:
    """Per owner: how many agents, what they approved and paid, how often they
    were blocked or sent for review."""
    with tenant_session(tenant_id, store.url) as s:
        agents = s.execute(
            select(Agent.owner, func.count())
            .where(Agent.tenant_id == tenant_id)
            .group_by(Agent.owner)
        ).all()
        rows = s.execute(
            select(
                Agent.owner,
                AgentAction.action,
                AgentAction.decision,
                AgentAction.currency,
                AgentAction.outcome,
                func.count(),
                func.sum(AgentAction.amount),
            )
            .join(Agent, Agent.id == AgentAction.agent_id)
            .where(
                Agent.tenant_id == tenant_id, *_counted(tenant_id, since, until)
            )
            .group_by(
                Agent.owner,
                AgentAction.action,
                AgentAction.decision,
                AgentAction.currency,
                AgentAction.outcome,
            )
        ).all()
        exported = _exports(s, tenant_id, since, until)
    owners = {
        owner: {
            "owner": owner,
            "agents": n,
            "approved": defaultdict(Decimal),
            "exported": defaultdict(Decimal),
            "blocked": 0,
            "reviewed": 0,
        }
        for owner, n in agents
    }
    for owner, action, decision, currency, outcome, n, total in rows:
        o = owners[owner]
        if decision == "BLOCK":
            o["blocked"] += n
            continue
        if decision == "ALLOW-WITH-REVIEW":
            o["reviewed"] += n
        if action == APPROVE and total is not None and outcome != "failed":
            o["approved"][currency or ""] += Decimal(str(total))
    for owner, by_currency in exported.items():
        for currency, amount in by_currency.items():
            owners[owner]["exported"][currency] += amount
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "owners": [
            {
                **o,
                "approved": {
                    c: money(a) for c, a in sorted(o["approved"].items())
                },
                "exported": {
                    c: money(a) for c, a in sorted(o["exported"].items())
                },
            }
            for _, o in sorted(owners.items())
        ],
    }
