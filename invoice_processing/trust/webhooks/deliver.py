"""Send due outbox rows (PRD §9). The worker calls `due` every
`WEBHOOK_SCAN_SECONDS`.

At least once: a row is marked only after its POST returns, so a crash
between the two sends it again; receivers dedupe on `X-Trust-Delivery`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import Tenant, WebhookEndpoint, WebhookOutbox
from ...db.session import get_engine, tenant_session
from ...masking import mask_text
from ...ports import RunContext
from . import target as _target

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 8
MAX_BACKOFF_MINUTES = 64
# One tenant's backlog cannot fill a whole sweep and starve the others.
PER_TENANT = 10


def sign(secret: str, ts: str, body: str) -> str:
    """`sha256=` + hex HMAC-SHA256 of `"{ts}.{body}"` under the endpoint's secret."""
    mac = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def due(
    now: datetime, *, post=None, limit: int = 50, audit=None, url=None
) -> int:
    """Attempt up to `limit` due rows across active tenants; the number tried."""
    if post is None:
        import requests  # noqa: PLC0415

        post = requests.post
    # `tenants` has no RLS; the outbox forces it, so each tenant is read in
    # its own session. A suspended tenant's events wait, as its approvals do.
    with Session(get_engine(url)) as s:
        tenants = s.scalars(
            select(Tenant.id).where(Tenant.status == "active")
        ).all()
    tried = 0
    for tenant_id in tenants:
        if tried >= limit:
            break
        try:
            tried += _sweep_tenant(
                tenant_id, now, post, min(limit - tried, PER_TENANT), audit, url
            )
        except Exception:
            logger.exception("Webhook sweep failed for a tenant; continuing.")
    return tried


def _sweep_tenant(tenant_id, now, post, limit, audit, url) -> int:
    with tenant_session(tenant_id, url) as s:
        due_rows = s.execute(
            select(WebhookOutbox, WebhookEndpoint.url, WebhookEndpoint.secret)
            .join(
                WebhookEndpoint, WebhookEndpoint.id == WebhookOutbox.endpoint_id
            )
            .where(
                WebhookOutbox.tenant_id == tenant_id,
                WebhookEndpoint.tenant_id == tenant_id,
                WebhookEndpoint.active.is_(True),
                WebhookOutbox.delivered_at.is_(None),
                WebhookOutbox.dead_at.is_(None),
                WebhookOutbox.next_attempt_at <= now,
            )
            .order_by(WebhookOutbox.next_attempt_at, WebhookOutbox.id)
            .limit(limit)
        ).all()
        batch = [
            (r.id, r.event, r.payload, target, secret)
            for r, target, secret in due_rows
        ]
    # The POSTs run outside any transaction: ten slow endpoints must not hold
    # a database connection for minutes. The sweep runs inside the worker's
    # loop, so an endpoint that cannot be reached is tried once per sweep:
    # its other rows wait for the next one rather than cost ten seconds each.
    unreachable: set[str] = set()
    tried = 0
    for row_id, event, payload, target, secret in batch:
        if target in unreachable:
            continue
        tried += 1
        body = json.dumps(payload, default=str, separators=(",", ":"))
        # The moment it is sent, not the sweep's: receivers check freshness.
        ts = str(int(time.time()))
        status, error = None, None
        # Checked per delivery: a public name may have been pointed inward
        # since the endpoint was saved.
        if _target.refused(target):
            unreachable.add(target)
            _record(
                tenant_id,
                row_id,
                now,
                None,
                "The url is internal or ambiguous; not sent.",
                audit,
                url,
            )
            continue
        try:
            response = post(
                target,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Trust-Event": event,
                    "X-Trust-Delivery": row_id,
                    "X-Trust-Timestamp": ts,
                    "X-Trust-Signature": sign(secret, ts, body),
                },
                timeout=TIMEOUT_SECONDS,
                # A redirect would re-send the signed body to wherever it
                # points, http:// included; a 3xx is a failed attempt.
                allow_redirects=False,
            )
            status = response.status_code
            if not 200 <= status < 300:
                error = f"HTTP {status}"
        except Exception as exc:
            unreachable.add(target)
            error = mask_text(f"{type(exc).__name__}: {exc}")[:500]
        _record(tenant_id, row_id, now, status, error, audit, url)
    return tried


def _record(tenant_id, row_id, now, status, error, audit, url) -> None:
    dead = None
    with tenant_session(tenant_id, url) as s:
        row = s.get(WebhookOutbox, row_id)
        if row is None:  # its endpoint was deleted mid-sweep
            return
        row.last_status = status
        if error is None:
            row.delivered_at = now
            row.last_error = None
            return
        row.attempts += 1
        row.last_error = error
        row.next_attempt_at = now + timedelta(
            minutes=min(2**row.attempts, MAX_BACKOFF_MINUTES)
        )
        if row.attempts >= MAX_ATTEMPTS:
            row.dead_at = now
            dead = {
                "event": row.event,
                "attempts": row.attempts,
                "last_status": status,
            }
    if dead is not None:
        if audit is None:
            from ...api import service  # noqa: PLC0415

            audit = service.get_audit()
        ctx = RunContext(
            tenant_id=tenant_id, case_id="", run_id="", actor="worker"
        )
        audit.emit(ctx, "webhook_dead_lettered", dead)
