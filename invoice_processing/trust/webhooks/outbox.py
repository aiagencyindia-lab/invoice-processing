"""Write an event for every endpoint that wants it."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from ...db.models import WebhookEndpoint, WebhookOutbox
from . import EVENTS


def emit(
    s, tenant_id: str, event: str, data: dict, *, now: datetime | None = None
) -> int:
    """Queue `event` for each active endpoint subscribed to it (or to "*").
    `s` is the caller's session, so the row commits with the change."""
    if event not in EVENTS:
        raise ValueError(f"No such event: {event}")
    now = now or datetime.now(timezone.utc)
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
