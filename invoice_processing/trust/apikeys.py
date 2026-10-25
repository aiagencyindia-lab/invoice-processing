"""Per-tenant API keys for the /v2 API (PRD §9, FR-30).

A key is `ik_{tenant_slug}_{prefix}_{secret}`. Only `sha256(secret)` is
stored; the slug names the tenant and the prefix finds the row, so a lookup
reads one tenant. Keys made before the slug was added (`ik_{prefix}_{secret}`)
still resolve, by asking each active tenant in turn. `api.service` registers `resolve` with
`auth.set_api_key_resolver`, so a presented key becomes a Principal of the
key's tenant and role, or a 401.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Agent, ApiKey, Tenant
from ..db.session import get_engine, tenant_session

logger = logging.getLogger(__name__)

PREFIX = "ik_"
# A key never outranks the people who administer it.
ROLES = ("viewer", "clerk", "approver")
# Slugs are `[a-z0-9][a-z0-9-]{1,62}` (provision.SLUG_PATTERN): no `_`, so
# the first one ends the slug.
_TOKEN = re.compile(
    r"^ik_([a-z0-9][a-z0-9-]{1,62})_([0-9a-f]{8})_([A-Za-z0-9_-]{16,})$"
)
_LEGACY = re.compile(r"^ik_([0-9a-f]{8})_([A-Za-z0-9_-]{16,})$")


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
        # Agent control knows a key by `{name}@api-key.{tenant}`: a new key
        # with a registered agent's name would act as that agent.
        email = f"{name}@api-key.{ctx.tenant_id}".casefold()
        if s.scalars(
            select(Agent.id).where(
                Agent.tenant_id == ctx.tenant_id,
                Agent.principal_email == email,
                Agent.status == "active",
            )
        ).first():
            raise ValueError(
                "An active agent already uses this key name; pick another."
            )
        slug = s.scalar(select(Tenant.slug).where(Tenant.id == ctx.tenant_id))
        if not slug:
            raise ValueError("The tenant has no slug to put in the key.")
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
    return row, f"{PREFIX}{slug}_{prefix}_{secret}"


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
        key.revoked_at = datetime.now(UTC)
        row = _row(key)
    if audit is not None:
        audit.emit(
            ctx,
            "api_key_revoked",
            {"name": row["name"], "prefix": row["prefix"]},
        )
    return row


def _find(tenant_id: str, prefix: str, digest: str, url=None):
    """The Principal for this prefix and secret hash in one tenant, or None."""
    from ..api.auth import Principal  # noqa: PLC0415

    with tenant_session(tenant_id, url) as s:
        for key in s.scalars(
            select(ApiKey).where(
                ApiKey.tenant_id == tenant_id,
                ApiKey.key_prefix == prefix,
                ApiKey.revoked_at.is_(None),
            )
        ):
            if hmac.compare_digest(key.key_hash, digest):
                return Principal(
                    user_id=f"api-key:{key.id}",
                    email=f"{key.name}@api-key.{tenant_id}",
                    tenant_id=tenant_id,
                    role=key.role,
                )
    return None


def _scan(prefix: str, digest: str, url=None):
    """A key without a slug: ask each active tenant in turn. `tenants` has
    no RLS; `api_keys` forces it and the app role cannot bypass it.
    ponytail: O(active tenants) queries, only for keys made before slugs."""
    with Session(get_engine(url)) as s:
        tenants = s.scalars(
            select(Tenant.id).where(Tenant.status == "active")
        ).all()
    for tenant_id in tenants:
        found = _find(tenant_id, prefix, digest, url)
        if found is not None:
            return found
    return None


def resolve(token: str, url=None):
    """The Principal a key stands for, or None when there is no such key, it
    is revoked, or its tenant is not active. An unreachable database is a
    503, never a guess."""
    from ..api.auth import AuthError  # noqa: PLC0415

    token = token or ""
    new, legacy = _TOKEN.match(token), _LEGACY.match(token)
    if new is None and legacy is None:
        return None
    try:
        found = None
        if new is not None:
            slug, prefix, secret = new.groups()
            with Session(get_engine(url)) as s:
                tenant_id = s.scalar(
                    select(Tenant.id).where(
                        Tenant.slug == slug, Tenant.status == "active"
                    )
                )
            if tenant_id is not None:
                found = _find(tenant_id, prefix, _hash(secret), url)
        # A legacy secret can happen to read as `{slug}_{prefix}_...`; only
        # when the slug form found nothing is the old form tried.
        if found is None and legacy is not None:
            found = _scan(legacy.group(1), _hash(legacy.group(2)), url)
    except Exception as exc:
        logger.error("API key lookup failed: %s", type(exc).__name__)
        raise AuthError(
            "API keys need a database that could not be reached. Try again "
            "shortly.",
            status_code=503,
        ) from exc
    if found is not None:
        try:
            _stamp(found.tenant_id, found.user_id.removeprefix("api-key:"), url)
        except Exception as exc:  # best effort: a stamp never refuses a key
            logger.warning("API key use not stamped: %s", type(exc).__name__)
    return found


def _stamp(tenant_id: str, key_id: str, url=None) -> None:
    with tenant_session(tenant_id, url) as s:
        key = s.get(ApiKey, key_id)
        if key is not None:
            key.last_used_at = datetime.now(UTC)
