"""Who a verified token is, once the users table has a say.

The tenant still comes from the signed token and from nowhere else. What this
module adds is the users row's word on role and active status, and the one
path by which a token with no tenant yet can earn one: a verified company
address whose domain an admin bound to this tenant.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

from .api.auth import AuthError, Principal, principal_from_claims

logger = logging.getLogger(__name__)

PUBLIC_MAILBOX_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "icloud.com",
        "proton.me",
        "protonmail.com",
    }
)
_DOMAIN = re.compile(
    r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
# ponytail: one write per quarter hour; sharpen if anyone needs minute-level last-seen.
LOGIN_TOUCH = timedelta(minutes=15)
GOOGLE = "google.com"
MICROSOFT = "microsoft.com"


class SsoServices(NamedTuple):
    directory: Any  # PgUserDirectory-shaped
    claims: Any  # ClaimsWriter
    audit: Any  # AuditSink


def normalize_domain(value: str) -> str:
    domain = (value or "").strip().lower()
    if not _DOMAIN.match(domain):
        raise ValueError(f"{value!r} is not a domain name.")
    if domain in PUBLIC_MAILBOX_DOMAINS:
        raise ValueError(
            f"{domain} is a public mailbox provider; anyone can get an address "
            "there, so it cannot identify an organisation."
        )
    return domain


def domain_of(email: str) -> str:
    return (email or "").rpartition("@")[2].strip().lower()


def _unavailable(exc: Exception) -> AuthError:
    logger.error(
        "Sign-in needs the users directory and could not read it: %s", exc
    )
    return AuthError(
        "Sign-in needs a database that could not be reached. Try again shortly.",
        status_code=503,
    )


def _refuse_inactive(row: dict) -> None:
    if row.get("is_active") is False:
        raise AuthError(
            "This account has been deactivated. Ask an admin of "
            f"{row.get('tenant_name') or 'your organisation'} to reactivate it.",
            status_code=403,
        )


def _touch(services: SsoServices, row: dict, now: datetime) -> None:
    last = row.get("last_login_at")
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last is None or now - last >= LOGIN_TOUCH:
        try:
            services.directory.touch_login(row["tenant_id"], row["id"], now)
        except Exception as exc:  # a lost timestamp must not cost a request
            logger.warning(
                "last_login_at not updated for %s: %s", row.get("email"), exc
            )


def enforce_active(
    principal: Principal, services: SsoServices | None
) -> Principal:
    """Refuse a deactivated row; keep the principal's own role (dev bypass)."""
    if services is None:
        return principal
    try:
        row = services.directory.find(principal.tenant_id, principal.email)
    except Exception as exc:
        raise _unavailable(exc) from exc
    if row is not None:
        _refuse_inactive(row)
    return principal


def resolve_principal(
    claims: dict[str, Any],
    services: SsoServices | None,
    now: datetime | None = None,
) -> Principal:
    now = now or datetime.now(timezone.utc)
    if claims.get("tenant_id") or claims.get("tenantId"):
        base = principal_from_claims(claims)
        if services is None:
            return base
        try:
            row = services.directory.find(base.tenant_id, base.email)
        except Exception as exc:
            raise _unavailable(exc) from exc
        if row is None:
            return base
        _refuse_inactive(row)
        _touch(services, {**row, "tenant_id": base.tenant_id}, now)
        return Principal(
            user_id=base.user_id,
            email=base.email,
            tenant_id=base.tenant_id,
            role=str(row["role"]),
        )

    # No tenant yet: the only way in is a bound company domain.
    if services is None:
        return principal_from_claims(claims)  # raises the usual 403
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise AuthError("Token carries no email claim.", status_code=403)
    if claims.get("email_verified") is not True:
        raise AuthError("Email address is not verified.", status_code=403)
    provider = (claims.get("firebase") or {}).get("sign_in_provider")
    if provider == MICROSOFT:
        raise AuthError(
            "Microsoft sign-in is not enabled yet.", status_code=403
        )
    if provider != GOOGLE:
        return principal_from_claims(claims)  # raises the usual 403
    try:
        bound = services.directory.domain(domain_of(email))
    except Exception as exc:
        raise _unavailable(exc) from exc
    if bound is None or bound.get("provider") != "google":
        # A binding for another provider never admits a Google account.
        return principal_from_claims(claims)  # raises the usual 403

    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    tenant_id = bound["tenant_id"]
    uid = str(claims.get("user_id") or claims.get("sub") or "")
    role = str(bound["default_role"])
    created = False
    try:
        row = services.directory.find(tenant_id, email)
        if row is None:
            ctx = Principal(
                user_id=uid, email=email, tenant_id=tenant_id, role=role
            ).context()
            payload = {"email": email, "role": role, "domain": bound["domain"]}
            try:
                # The event is written in the insert's own transaction, so a
                # user can never exist without the record of how they arrived.
                row = services.directory.provision(
                    tenant_id,
                    email,
                    role,
                    uid or None,
                    (claims.get("name") or None),
                    audit=(ctx, "user_provisioned_by_sso", payload),
                    now=now,
                )
                created = True
            except IntegrityError:
                # A concurrent first login inserted the row first.
                row = services.directory.find(tenant_id, email)
                if row is None:
                    raise
        if not created:
            _refuse_inactive(row)
            if not row.get("uid") and uid:
                services.directory.link_uid(tenant_id, row["id"], uid)
    except AuthError:
        raise
    except Exception as exc:
        raise _unavailable(exc) from exc

    principal = Principal(
        user_id=uid, email=email, tenant_id=tenant_id, role=str(row["role"])
    )
    if uid:
        try:
            services.claims.set_claims(
                uid, {"tenant_id": tenant_id, "role": row["role"]}
            )
        except Exception as exc:
            # The row is the authority now; the next request writes the claim.
            logger.warning("Claims not written for %s: %s", email, exc)
    if not created:  # provision already stamped last_login_at
        _touch(services, {**row, "tenant_id": tenant_id}, now)
    return principal
