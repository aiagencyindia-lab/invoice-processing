"""Write an event for every endpoint that wants it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select

from ...db.models import WebhookEndpoint, WebhookOutbox
from ...db.session import tenant_session
from . import EVENTS

# Delivered or dead-lettered rows kept this long (the endpoint's delivery
# list shows them); a row still owed a delivery is never purged.
RETENTION_DAYS = 90


def emit(
    s, tenant_id: str, event: str, data: dict, *, now: datetime | None = None
) -> int:
    """Queue `event` for each active endpoint subscribed to it (or to "*").
    `s` is the caller's session, so the row commits with the change."""
    if event not in EVENTS:
        raise ValueError(f"No such event: {event}")
    now = now or datetime.now(UTC)
    # ponytail: one endpoint query per event; cache per session if a batch
    # run's thousands of findings make it show.
    endpoints = s.scalars(
        select(WebhookEndpoint).where(
            WebhookEndpoint.tenant_id == tenant_id,
            WebhookEndpoint.active.is_(True),
        )
    ).all()
    added = 0
    for ep in endpoints:
        wanted = ep.events or []
        if event not in wanted and "*" not in wanted:
            continue
        s.add(
            WebhookOutbox(
                tenant_id=tenant_id,
                endpoint_id=ep.id,
                event=event,
                payload={
                    "event": event,
                    "tenant": tenant_id,
                    "occurred_at": now.isoformat(),
                    "data": data,
                },
                attempts=0,
                next_attempt_at=now,
            )
        )
        added += 1
    return added


def purge_finished(
    tenant_id: str, now: datetime, *, days: int = RETENTION_DAYS, url=None
) -> int:
    """Daily housekeeping (the worker's retention sweep): delivered or dead
    rows older than `days`. Returns rows gone."""
    cutoff = now - timedelta(days=days)
    with tenant_session(tenant_id, url) as s:
        gone = s.execute(
            delete(WebhookOutbox).where(
                WebhookOutbox.tenant_id == tenant_id,
                or_(
                    WebhookOutbox.delivered_at < cutoff,
                    WebhookOutbox.dead_at < cutoff,
                ),
            )
        ).rowcount
    return int(gone or 0)
