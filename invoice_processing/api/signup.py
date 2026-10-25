"""Self-service sign-up: an account creates its own organisation.

This is deliberately gated. Everywhere else in this service, belonging to a
tenant is something an administrator grants -- `ALLOW_TENANT_PROVISIONING` is
off by default precisely so a customer's admin cannot mint organisations,
inbound-mail addresses and storage prefixes inside the system. Self-service
sign-up hands that same power to anyone with an email address, which is a
product decision rather than a default, so it needs `ALLOW_SELF_SIGNUP` set
explicitly and answers 403 with the reason when it is not.

What it does not do is weaken any check downstream. The caller must present a
token this service has verified against Google's keys; the tenant is created
server-side from that token's identity; and the claims are written by the same
Provisioner the CLI uses. A caller cannot name their own tenant id, choose
their own role, or sign up as somebody else. The address must be verified,
and its domain must not already be bound to an organisation.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import unicodedata
from collections import deque
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from .auth import verified_claims

logger = logging.getLogger(__name__)

# Nothing was kept, so trying again is safe and is the whole remedy.
CLAIMS_UNAVAILABLE = (
    "Your organisation could not be set up because sign-in access could not "
    "be granted just now. Nothing was saved; try again in a moment."
)

router = APIRouter()


class SignUpRequest(BaseModel):
    organisation: str


def self_signup_enabled() -> bool:
    """Whether this deployment lets an account create its own organisation."""
    return os.getenv("ALLOW_SELF_SIGNUP", "").lower() in ("1", "true", "yes")


def slug_for(organisation: str, taken: set[str]) -> str:
    """A URL and email-safe slug, unique among existing tenants.

    The slug becomes an inbound-email local part and an object-storage prefix,
    so it is derived and sanitised here rather than accepted from the caller --
    a chosen slug is a way to collide with, or impersonate, another tenant.
    """
    # Decompose first, so an accented letter becomes its base letter rather
    # than a separator: "Café Ltd" is cafe-ltd, not caf-ltd.
    folded = unicodedata.normalize("NFKD", organisation.strip().lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    base = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    base = base[:40] or "org"
    if not base[0].isalnum():
        base = f"o{base}"
    if base not in taken:
        return base
    # Numeric suffixes rather than random ones, so the second "Acme" is
    # acme-2 and reads as what it is.
    for n in range(2, 1000):
        candidate = f"{base}-{n}"
        if candidate not in taken:
            return candidate
    raise HTTPException(status_code=409, detail="Could not allocate a name.")


MICROSOFT_NOT_SET_UP = (
    "Microsoft sign-in isn't set up for {domain}. Ask your administrator."
)


# The public provider lookup's limits, per caller and in total, per minute.
LOOKUPS_PER_IP = 20
LOOKUPS_OVERALL = 600


class _Window:
    """Calls per key in the last minute.

    ponytail: in-process and per container (Railway runs one); a shared
    limit needs the database or an edge rule. The caller key is the first
    forwarded address, which a client can set, so the overall cap is what
    bounds a caller who rotates it.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._calls: dict[str, deque] = {}

    def clear(self) -> None:
        with self._lock:
            self._calls.clear()

    def take(self, key: str, limit: int, now: float) -> int:
        """0 when the call may go ahead, else seconds until it may."""
        with self._lock:
            calls = self._calls.setdefault(key, deque())
            while calls and calls[0] <= now - 60:
                calls.popleft()
            if len(calls) >= limit:
                return max(1, int(calls[0] + 60 - now) + 1)
            calls.append(now)
            if len(self._calls) > 10_000:  # forget idle callers
                for k in [k for k, v in self._calls.items() if not v]:
                    del self._calls[k]
            return 0


LOOKUP_LIMIT = _Window()


def _lookup_allowed() -> None:
    from . import auth  # noqa: PLC0415

    now = time.monotonic()
    wait = LOOKUP_LIMIT.take(
        "ip:" + auth.current_client_ip.get(""), LOOKUPS_PER_IP, now
    ) or LOOKUP_LIMIT.take("*", LOOKUPS_OVERALL, now)
    if wait:
        raise HTTPException(
            status_code=429,
            detail="Too many lookups; try again in a minute.",
            headers={"Retry-After": str(wait)},
        )


@router.post("/api/sso/microsoft")
def microsoft_provider(email: str = Body("", embed=True)) -> dict:
    """Which OIDC provider a work address signs in through.

    Public, before sign-in: the login page needs it to open the right
    customer's Microsoft provider. It reveals only whether a domain uses
    Ledgerly through Microsoft (SECURITY.md); a Google binding, an unbound
    or public-mailbox domain and a malformed, missing or over-long address
    all answer the same. POST, so the address is not in a URL a proxy logs.
    """
    from .. import sso  # noqa: PLC0415
    from . import auth  # noqa: PLC0415

    _lookup_allowed()
    email = email if isinstance(email, str) and len(email) <= 320 else ""
    raw = sso.domain_of(email) if "@" in email else ""
    not_set_up = HTTPException(
        status_code=404,
        detail=MICROSOFT_NOT_SET_UP.format(domain=raw or "that address"),
    )
    try:
        domain = sso.normalize_domain(raw)
    except ValueError:
        raise not_set_up from None
    services = auth._services()
    if services is None:
        raise HTTPException(
            status_code=503,
            detail="Microsoft sign-in needs a database, which is not "
            "configured.",
        )
    try:
        bound = services.directory.domain(domain)
    except Exception as exc:
        raise sso._unavailable(exc) from exc
    if (
        bound is None
        or bound.get("provider") != "microsoft"
        or not bound.get("idp_provider_id")
    ):
        raise not_set_up
    return {"provider_id": bound["idp_provider_id"]}


@router.post("/api/signup")
def sign_up(
    request: SignUpRequest,
    claims: Annotated[dict[str, Any], Depends(verified_claims)],
) -> dict:
    """Create an organisation for the signed-in account and make it the admin."""
    if not self_signup_enabled():
        raise HTTPException(
            status_code=403,
            detail=(
                "Sign-up is not open on this deployment. An administrator "
                "adds accounts to an organisation."
            ),
        )

    # Already belongs somewhere: creating a second organisation silently would
    # strand every invoice they have already reviewed.
    if claims.get("tenant_id") or claims.get("tenantId"):
        raise HTTPException(
            status_code=409,
            detail="This account already belongs to an organisation.",
        )

    # The verified address is the identity everything below is keyed on; a
    # token that does not vouch for it cannot found an organisation.
    if claims.get("email_verified") is not True:
        raise HTTPException(
            status_code=403,
            detail="Verify your email address before creating an organisation.",
        )

    # A bound domain already has an organisation; a second one on it would
    # split a company across tenants. Same accessor and lookup as sign-in,
    # so an unreachable database refuses (503) instead of skipping the check.
    # 403, not 409: the page reads 409 as "already a member".
    from .. import sso  # noqa: PLC0415
    from .auth import _services  # noqa: PLC0415

    services = _services()
    if services is not None:
        try:
            bound = services.directory.domain(
                sso.domain_of(str(claims["email"]))
            )
        except Exception as exc:
            raise sso._unavailable(exc) from exc
        if bound is not None:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Your organisation's domain is already connected to "
                    "Ledgerly. Sign in with Google using this address and "
                    "you'll join it."
                ),
            )

    name = request.organisation.strip()
    if len(name) < 2:
        raise HTTPException(
            status_code=400, detail="An organisation name is required."
        )

    from ..admin.provision import (  # noqa: PLC0415
        ClaimsRefused,
        Provisioner,
        ProvisioningError,
    )
    from . import service  # noqa: PLC0415

    provisioner = Provisioner(claims=service.get_claims_writer())
    existing = {t["slug"] for t in provisioner.list_tenants()}
    slug = slug_for(name, existing)

    email = str(claims["email"])
    uid = str(claims.get("user_id") or claims.get("sub") or "")
    if not uid:
        raise HTTPException(
            status_code=403, detail="Token carries no account id."
        )

    try:
        # admin: they are the only member, and nobody else can grant them
        # anything afterwards.
        record = provisioner.provision_signup(slug, name, email, uid)
    except ClaimsRefused as exc:
        # Identity Platform's own text names projects and APIs; it is for
        # the log, not for the person signing up.
        logger.warning(
            "Sign-up for uid %s could not write claims: %s", uid, exc
        )
        raise HTTPException(status_code=503, detail=CLAIMS_UNAVAILABLE) from exc
    except ProvisioningError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    tenant = record["tenant"]
    # The uid, not the address: log lines are read by more people than the
    # audit trail, which keeps the address on its own row.
    logger.info("Self sign-up created tenant %s for uid %s", slug, uid)

    return {
        "tenant": tenant,
        "role": record["role"],
        "claims_written": record["claims_written"],
    }
