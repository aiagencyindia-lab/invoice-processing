"""Per-tenant API keys for the /v2 API (PRD §9, FR-30).

A key is `ik_{prefix}_{secret}`. Only `sha256(secret)` is stored; the prefix
finds the row. `api.service` registers `resolve` with
`auth.set_api_key_resolver`, so a presented key becomes a Principal of the
key's tenant and role, or a 401.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import ApiKey, Tenant
from ..db.session import get_engine, tenant_session

logger = logging.getLogger(__name__)

PREFIX = "ik_"
# A key never outranks the people who administer it.
ROLES = ("viewer", "clerk", "approver")
_TOKEN = re.compile(r"^ik_([0-9a-f]{8})_([A-Za-z0-9_-]{16,})$")


def _prefix() -> str:
    return secrets.token_hex(4)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _row(k: ApiKey) -> dict:
    return {
        "id": k.id,
        "name": k.name,
        "role": k.role,
        "prefix": k.key_prefix,
        "created_by": k.created_by,
        "created_at": k.created_at,
        "last_used_at": k.last_used_at,
        "revoked_at": k.revoked_at,
    }


def create(
    ctx, name: str, role: str, *, audit=None, url=None
) -> tuple[dict, str]:
    """A new key for `ctx.tenant_id`: the row and the plaintext, shown once."""
    name = (name or "").strip()
    if not name or len(name) > 128:
        raise ValueError("name is required (at most 128 characters).")
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}.")
    prefix, secret = _prefix(), secrets.token_urlsafe(32)
    with tenant_session(ctx.tenant_id, url) as s:
        key = ApiKey(
            tenant_id=ctx.tenant_id,
            name=name,
            role=role,
            key_prefix=prefix,
            key_hash=_hash(secret),
            created_by=ctx.actor,
        )
        s.add(key)
        s.flush()
        row = _row(key)
    if audit is not None:
        audit.emit(
            ctx,
            "api_key_created",
            {"name": name, "role": role, "prefix": prefix},
        )
    return row, f"{PREFIX}{prefix}_{secret}"


def list_keys(tenant_id: str, url=None) -> list[dict]:
    with tenant_session(tenant_id, url) as s:
        keys = s.scalars(
            select(ApiKey)
            .where(ApiKey.tenant_id == tenant_id)
            .order_by(ApiKey.created_at, ApiKey.id)
        ).all()
        return [_row(k) for k in keys]


def revoke(ctx, key_id: str, *, audit=None, url=None) -> dict | None:
    """Revoke one of this tenant's live keys; None when there is no such key."""
    with tenant_session(ctx.tenant_id, url) as s:
        key = s.scalars(
            select(ApiKey).where(
                ApiKey.tenant_id == ctx.tenant_id,
                ApiKey.id == key_id,
                ApiKey.revoked_at.is_(None),
            )
        ).first()
        if key is None:
            return None
        key.revoked_at = datetime.now(timezone.utc)
        row = _row(key)
    if audit is not None:
        audit.emit(
            ctx,
            "api_key_revoked",
            {"name": row["name"], "prefix": row["prefix"]},
        )
    return row


def resolve(token: str, url=None):
    """The Principal a key stands for, or None when there is no such key, it
    is revoked, or its tenant is not active. An unreachable database is a
    503, never a guess."""
    from ..api.auth import AuthError, Principal  # noqa: PLC0415

    match = _TOKEN.match(token or "")
    if match is None:
        return None
    prefix, digest = match.group(1), _hash(match.group(2))
    try:
        # `tenants` has no RLS; `api_keys` forces it and the app role cannot
        # bypass it, so the prefix is looked up inside each tenant's session.
        # ponytail: one query per active tenant per key request; a SELECT
        # policy for the lookup (as sso_domains has) makes it one query.
        with Session(get_engine(url)) as s:
            tenants = s.scalars(
                select(Tenant.id).where(Tenant.status == "active")
            ).all()
        for tenant_id in tenants:
            with tenant_session(tenant_id, url) as s:
                for key in s.scalars(
                    select(ApiKey).where(
                        ApiKey.tenant_id == tenant_id,
                        ApiKey.key_prefix == prefix,
                        ApiKey.revoked_at.is_(None),
                    )
                ):
                    if hmac.compare_digest(key.key_hash, digest):
                        key.last_used_at = datetime.now(timezone.utc)
                        return Principal(
                            user_id=f"api-key:{key.id}",
                            email=f"{key.name}@api-key.{tenant_id}",
                            tenant_id=tenant_id,
                            role=key.role,
                        )
    except Exception as exc:
        logger.error("API key lookup failed: %s", type(exc).__name__)
        raise AuthError(
            "API keys need a database that could not be reached. Try again "
            "shortly.",
            status_code=503,
        ) from exc
    return None
