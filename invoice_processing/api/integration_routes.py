"""API keys and webhook endpoints, administered per tenant (PRD §9, FR-30).

Admin only, and never by a key: a machine credential cannot mint another
one or point the tenant's events somewhere new.
"""

from __future__ import annotations

import ipaddress
import secrets
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from ..db.models import WebhookEndpoint, WebhookOutbox
from ..db.session import tenant_session
from ..masking import mask_row
from ..trust import apikeys
from ..trust.webhooks import EVENTS
from .auth import Principal, require_role

router = APIRouter()
NEEDS_DB = "Integrations need a database. Configure DATABASE_URL."
MAX_DELIVERIES = 50


def _admin(
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> Principal:
    if principal.user_id.startswith("api-key:"):
        raise HTTPException(403, "An API key cannot administer integrations.")
    from . import service  # noqa: PLC0415

    if not service._database_configured():
        raise HTTPException(503, NEEDS_DB)
    return principal


Admin = Annotated[Principal, Depends(_admin)]


def _audit():
    from . import service  # noqa: PLC0415

    return service.get_audit()


# -- API keys -------------------------------------------------------------------


class KeyCreate(BaseModel):
    name: str = Field(max_length=128)
    role: str = Field(default="viewer", max_length=16)


@router.get("/api/integrations/api-keys")
def list_api_keys(principal: Admin):
    return {"keys": apikeys.list_keys(principal.tenant_id)}


@router.post("/api/integrations/api-keys", status_code=201)
def create_api_key(body: KeyCreate, principal: Admin):
    try:
        row, key = apikeys.create(
            principal.context(), body.name, body.role, audit=_audit()
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {**row, "key": key}


@router.delete("/api/integrations/api-keys/{key_id}")
def revoke_api_key(key_id: str, principal: Admin):
    row = apikeys.revoke(principal.context(), key_id, audit=_audit())
    if row is None:
        raise HTTPException(404, "Unknown API key.")
    return row


# -- webhooks -------------------------------------------------------------------


def _events(events: list[str]) -> list[str]:
    if events == ["*"] or (events and set(events) <= set(EVENTS)):
        return sorted(set(events))
    raise HTTPException(
        400,
        f'events must be ["*"] or a non-empty subset of {", ".join(EVENTS)}.',
    )


def _endpoint(ep: WebhookEndpoint) -> dict:
    return {
        "id": ep.id,
        "url": ep.url,
        "events": ep.events or [],
        "active": ep.active,
        "description": ep.description,
        "created_by": ep.created_by,
        "created_at": ep.created_at,
        # Shown in full once, on create.
        "secret": f"whsec_…{ep.secret[-4:]}",
    }


def _get(s, tenant_id: str, endpoint_id: str) -> WebhookEndpoint:
    ep = s.scalars(
        select(WebhookEndpoint).where(
            WebhookEndpoint.tenant_id == tenant_id,
            WebhookEndpoint.id == endpoint_id,
        )
    ).first()
    if ep is None:
        raise HTTPException(404, "Unknown webhook.")
    return ep


def _internal(host: str) -> bool:
    """Loopback, private, link-local or reserved: the worker must not be
    pointed into its own network.

    ponytail: literal addresses and localhost names only; a public name that
    resolves inward (or rebinds) still passes. Resolve and pin per delivery
    if that matters.
    """
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not ip.is_global


class WebhookCreate(BaseModel):
    url: str = Field(max_length=1024)
    events: list[str] = Field(max_length=len(EVENTS))
    description: str | None = Field(default=None, max_length=256)


class WebhookPatch(BaseModel):
    active: bool | None = None
    events: list[str] | None = Field(default=None, max_length=len(EVENTS))
    description: str | None = Field(default=None, max_length=256)


@router.get("/api/integrations/webhooks")
def list_webhooks(principal: Admin):
    t = principal.tenant_id
    with tenant_session(t) as s:
        eps = s.scalars(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.tenant_id == t)
            .order_by(WebhookEndpoint.created_at, WebhookEndpoint.id)
        ).all()
        return {"endpoints": [_endpoint(ep) for ep in eps]}


@router.post("/api/integrations/webhooks", status_code=201)
def create_webhook(body: WebhookCreate, principal: Admin):
    parts = urlsplit(body.url.strip())
    if parts.scheme != "https" or not parts.hostname:
        raise HTTPException(400, "url must be an https:// address.")
    if _internal(parts.hostname):
        raise HTTPException(400, "url must be a public address.")
    events = _events(body.events)
    secret = f"whsec_{secrets.token_urlsafe(24)}"
    t = principal.tenant_id
    with tenant_session(t) as s:
        ep = WebhookEndpoint(
            tenant_id=t,
            url=body.url.strip(),
            secret=secret,
            events=events,
            active=True,
            description=body.description,
            created_by=principal.email,
        )
        s.add(ep)
        s.flush()
        out = {**_endpoint(ep), "secret": secret}
    # The host only: a query string may carry the receiver's own token.
    _audit().emit(
        principal.context(),
        "webhook_endpoint_created",
        {"host": parts.hostname, "events": events},
    )
    return out


@router.patch("/api/integrations/webhooks/{endpoint_id}")
def update_webhook(endpoint_id: str, body: WebhookPatch, principal: Admin):
    changes = body.model_dump(exclude_unset=True)
    if "events" in changes:
        changes["events"] = _events(changes["events"] or [])
    if changes.get("active") is None:
        changes.pop("active", None)
    with tenant_session(principal.tenant_id) as s:
        ep = _get(s, principal.tenant_id, endpoint_id)
        for field, value in changes.items():
            setattr(ep, field, value)
        out = _endpoint(ep)
    _audit().emit(
        principal.context(),
        "webhook_endpoint_updated",
        {"host": urlsplit(out["url"]).hostname, "changed": sorted(changes)},
    )
    return out


@router.delete("/api/integrations/webhooks/{endpoint_id}")
def delete_webhook(endpoint_id: str, principal: Admin):
    t = principal.tenant_id
    with tenant_session(t) as s:
        ep = _get(s, t, endpoint_id)
        host = urlsplit(ep.url).hostname
        s.execute(
            delete(WebhookOutbox).where(
                WebhookOutbox.tenant_id == t,
                WebhookOutbox.endpoint_id == endpoint_id,
            )
        )
        s.delete(ep)
    _audit().emit(
        principal.context(), "webhook_endpoint_deleted", {"host": host}
    )
    return {"id": endpoint_id, "deleted": True}


@router.post("/api/integrations/webhooks/{endpoint_id}/test", status_code=202)
def ping_webhook(endpoint_id: str, principal: Admin):
    """Queue a `ping` for this endpoint alone, whatever it subscribes to."""
    t = principal.tenant_id
    now = datetime.now(UTC)
    with tenant_session(t) as s:
        ep = _get(s, t, endpoint_id)
        if not ep.active:
            raise HTTPException(
                409, "This webhook is paused; activate it first."
            )
        row = WebhookOutbox(
            tenant_id=t,
            endpoint_id=ep.id,
            event="ping",
            payload={
                "event": "ping",
                "tenant": t,
                "occurred_at": now.isoformat(),
                "data": {},
            },
            attempts=0,
            next_attempt_at=now,
        )
        s.add(row)
        s.flush()
        return {"delivery_id": row.id, "event": "ping"}


@router.get("/api/integrations/webhooks/{endpoint_id}/deliveries")
def list_deliveries(endpoint_id: str, principal: Admin):
    t = principal.tenant_id
    with tenant_session(t) as s:
        _get(s, t, endpoint_id)
        rows = s.scalars(
            select(WebhookOutbox)
            .where(
                WebhookOutbox.tenant_id == t,
                WebhookOutbox.endpoint_id == endpoint_id,
            )
            .order_by(WebhookOutbox.created_at.desc(), WebhookOutbox.id)
            .limit(MAX_DELIVERIES)
        ).all()
        return {
            "deliveries": [
                {
                    "id": r.id,
                    "event": r.event,
                    "attempts": r.attempts,
                    "next_attempt_at": r.next_attempt_at,
                    "delivered_at": r.delivered_at,
                    "dead_at": r.dead_at,
                    "last_status": r.last_status,
                    "last_error": r.last_error,
                    "created_at": r.created_at,
                    "payload": _masked(r.payload or {}),
                }
                for r in rows
            ]
        }


def _masked(payload: dict) -> dict:
    out = mask_row(payload)
    if isinstance(out.get("data"), dict):
        out["data"] = mask_row(out["data"])
    return out
