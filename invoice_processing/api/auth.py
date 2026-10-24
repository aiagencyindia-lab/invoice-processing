"""Authentication and tenant scoping.

One rule governs this module, and every design choice below follows from it:

    **The tenant comes from the signed token. Never from the request.**

A path parameter, a query string, a JSON body or a header supplied by the caller
is an assertion by the caller. A custom claim inside an Identity Platform ID
token is an assertion by Google, signed with a key we verify. Only the second
one may decide which tenant's data a request can see. Every cross-tenant leak in
this class of system is some version of trusting the first kind.

Consequences worth stating, because they look like inconveniences until you see
what they prevent:

* Endpoints do **not** take a `tenant_id` parameter. There is nowhere for a
  caller to put one, so there is nothing for a handler to forget to validate.
* A token with no `tenant_id` claim is rejected. It does not fall back to a
  default tenant -- a misconfigured user would silently read someone's invoices.
* The dev bypass refuses to activate when `ENV=production`, so the one mechanism
  that could hand out an unauthenticated principal cannot follow the code into
  the environment where it would matter.

Verification is injectable (`TokenVerifier`) so tests never touch the network
and never need a real Google key. The production implementation is
`IdentityPlatformVerifier`, which checks the signature, issuer, audience and
expiry through `google.oauth2.id_token`.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, runtime_checkable

from fastapi import Depends, HTTPException, Request

from .. import observability
from ..ports import RunContext
from ..roles import ROLE_RANK  # re-exported: callers import the ladder here

logger = logging.getLogger(__name__)

# Ascending privilege. A role not in this map is treated as no privilege at all,
# so a typo in a claim locks a user out rather than promoting them.

# The only environments in which authentication may be disabled. An allow-list,
# because the cost of an unlisted-but-safe value is an inconvenience and the
# cost of an unlisted-but-production value is an open door.
LOCAL_ENVIRONMENTS = {"local", "dev", "development", "test"}

DEV_PRINCIPAL_HEADER = "X-Dev-Principal"


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """Who is making this request, and which tenant they belong to.

    Frozen: a handler that could rewrite `tenant_id` mid-request would defeat
    the whole point of deriving it from the token.
    """

    user_id: str
    email: str
    tenant_id: str
    role: str = "viewer"

    @property
    def rank(self) -> int:
        return ROLE_RANK.get(self.role, -1)

    def can(self, required: str) -> bool:
        return self.rank >= ROLE_RANK.get(required, 99)

    def context(self, case_id: str = "", run_id: str = "") -> RunContext:
        """The RunContext for work done on this request's behalf."""
        return RunContext(
            tenant_id=self.tenant_id,
            case_id=case_id,
            # Falls back to the request id so every event from one request
            # shares a correlation key. It was always empty before, which is
            # why `request_id` was null on every row in production.
            run_id=run_id or current_request_id.get(""),
            actor=self.email,
            settings={"ip": current_client_ip.get("")}
            if current_client_ip.get("")
            else {},
        )


# Set once per request by the middleware in `service`, read here so a
# RunContext carries them without every endpoint having to pass the Request
# around. Both were columns the audit trail declared and never populated.
# The request id lives in observability so the worker and the log formatter
# can read it without importing the API; this is the same object.
current_request_id = observability.current_request_id
current_client_ip: ContextVar[str] = ContextVar("client_ip", default="")


class AuthError(HTTPException):
    def __init__(self, detail: str, status_code: int = 401):
        super().__init__(status_code=status_code, detail=detail)


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


@runtime_checkable
class TokenVerifier(Protocol):
    """Turns a bearer token into claims, or raises.

    A Protocol rather than a concrete class so tests inject a fake and never
    depend on Google's key endpoint being reachable.
    """

    def verify(self, token: str) -> dict[str, Any]: ...


class IdentityPlatformVerifier:
    """Verifies Google Identity Platform (Firebase Auth) ID tokens.

    `verify_firebase_token` checks the RS256 signature against Google's rotating
    public keys, and checks issuer, audience and expiry. We do not re-implement
    any of that; the value we add is refusing to proceed when the project id is
    unset, since an unset audience would mean accepting tokens minted for any
    other Google project.
    """

    def __init__(self, project_id: str | None = None):
        self.project_id = project_id or os.getenv("GOOGLE_CLOUD_PROJECT", "")
        if not self.project_id:
            raise ValueError(
                "No project id configured for token verification. Set "
                "GOOGLE_CLOUD_PROJECT; without an audience to check, tokens "
                "issued for any Google project would be accepted."
            )
        self._request = None

    def verify(self, token: str) -> dict[str, Any]:
        # Imported lazily: this pulls in google.auth's transport stack, which
        # tests never need and which would otherwise cost every import of the app.
        from google.auth.transport import requests as gr  # noqa: PLC0415
        from google.oauth2 import id_token  # noqa: PLC0415

        if self._request is None:
            self._request = gr.Request()
        try:
            return id_token.verify_firebase_token(
                token, self._request, audience=self.project_id
            )
        except Exception as exc:
            raise AuthError(f"Invalid token: {exc}") from exc


def principal_from_claims(claims: dict[str, Any]) -> Principal:
    """Build a Principal from verified claims.

    `tenant_id` is required. Defaulting it would mean a user whose claim was
    never set reads a real tenant's invoices, which is the exact failure this
    module exists to prevent.
    """
    tenant_id = claims.get("tenant_id") or claims.get("tenantId")
    if not tenant_id:
        raise AuthError(
            "Token carries no tenant_id claim. Set it as a custom claim when "
            "provisioning the user.",
            status_code=403,
        )
    email = claims.get("email")
    if not email:
        raise AuthError("Token carries no email claim.", status_code=403)
    if claims.get("email_verified") is not True:
        # Unverified email means anyone who can receive at that address can
        # claim it, and our audit trail is keyed on email.
        # Absent or non-boolean counts as unverified: we only trust what Google asserted.
        raise AuthError("Email address is not verified.", status_code=403)

    return Principal(
        user_id=str(claims.get("user_id") or claims.get("sub") or ""),
        email=str(email),
        tenant_id=str(tenant_id),
        role=str(claims.get("role") or "viewer"),
    )


# ---------------------------------------------------------------------------
# Dev bypass
# ---------------------------------------------------------------------------


def dev_principal(request: Request | None = None) -> Principal | None:
    """The unauthenticated principal used for local development, or None.

    Guarded twice. `AUTH_DISABLED` must be explicitly truthy, and `ENV` must not
    be a production-like value -- so if this setting ever escapes into a
    deployed environment it fails closed instead of disabling authentication in
    front of real data.
    """
    if os.getenv("AUTH_DISABLED", "").lower() not in ("1", "true", "yes"):
        return None
    env = os.getenv("ENV", "").lower()
    if env not in LOCAL_ENVIRONMENTS:
        # An allow-list, not a deny-list. A deny-list has to anticipate every
        # spelling of "production" -- `prd`, `live`, `production-eu`, or an
        # unset ENV would all have slipped through and handed out an
        # unauthenticated admin principal.
        logger.error(
            "AUTH_DISABLED is set but ENV=%r is not a known local environment "
            "(%s); refusing to disable authentication.",
            env,
            ", ".join(sorted(LOCAL_ENVIRONMENTS)),
        )
        return None
    logger.warning(
        "AUTH_DISABLED is set: every request runs as a local dev principal. "
        "This must never be set outside local development."
    )
    email = os.getenv("AUTH_DEV_EMAIL", "dev@localhost")
    role = "admin"
    # Who the dev principal is, per request. A browser test of a two-person
    # control (one proposes, another approves) cannot be run by a single fixed
    # admin. Only read here, behind both guards above: this mode already hands
    # every request an unauthenticated admin, so choosing a name or a lesser
    # role grants nothing new, and in any other mode the header is ignored.
    chosen = request.headers.get(DEV_PRINCIPAL_HEADER) if request else None
    if chosen is not None:
        email, _, role = chosen.strip().rpartition(":")
        if "@" not in email or role not in ROLE_RANK:
            # Refused rather than ignored: falling back to admin would let a
            # mistyped test run as the wrong person and still pass.
            raise AuthError(
                f"{DEV_PRINCIPAL_HEADER} must be '<email>:<role>' with a role "
                f"of {', '.join(ROLE_RANK)}.",
                status_code=400,
            )
    return Principal(
        user_id="dev" if chosen is None else f"dev:{email}",
        email=email,
        tenant_id=os.getenv("AUTH_DEV_TENANT", "default"),
        role=role,
    )


# ---------------------------------------------------------------------------
# FastAPI wiring
# ---------------------------------------------------------------------------

_verifier: TokenVerifier | None = None


def set_verifier(verifier: TokenVerifier | None) -> None:
    """Install the verifier. Tests inject a fake; startup installs the real one."""
    global _verifier
    _verifier = verifier


# What sign-in needs from the rest of the app: the users directory, the
# claims writer and the audit sink. Registered by `api.service` at import
# (this module cannot import service without a cycle); tests set it directly.
_sso_services = None


def set_sso_services(provider) -> None:
    global _sso_services
    _sso_services = provider


def _services():
    if _sso_services is None:
        return None
    try:
        return _sso_services()
    except Exception as exc:  # an unreachable database refuses, never guesses
        from .. import sso  # noqa: PLC0415

        raise sso._unavailable(exc) from exc


def get_verifier() -> TokenVerifier:
    global _verifier
    if _verifier is None:
        _verifier = IdentityPlatformVerifier()
    return _verifier


# Machine credentials for the /v2 API (trust/apikeys.py). Registered by
# `api.service`; None until it is, which refuses every key.
API_KEY_HEADER = "X-API-Key"
API_KEY_PREFIX = "ik_"
_api_key_resolver = None


def set_api_key_resolver(resolver) -> None:
    """`resolver(token) -> Principal | None`; None means no such key."""
    global _api_key_resolver
    _api_key_resolver = resolver


def api_key_token(request: Request, bearer: str | None) -> str | None:
    """The API key this request presents, if it presents one instead of a
    user's token. A signed-in user's bearer always wins: a key header next
    to it is ignored, so a key can never move a user to another tenant."""
    if bearer:
        return bearer if bearer.startswith(API_KEY_PREFIX) else None
    key = (request.headers.get(API_KEY_HEADER) or "").strip()
    return key or None


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def verified_claims(request: Request) -> dict[str, Any]:
    """The token's claims, verified, with no tenant requirement.

    `current_principal` refuses a token that carries no `tenant_id`, which is
    correct everywhere except the one endpoint whose job is to give an account
    its first tenant. That endpoint still needs the signature, issuer, audience
    and expiry checked -- it is the identity that is trusted here, never the
    caller's word about who they are.
    """
    token = bearer_token(request)
    if not token:
        raise AuthError("Authentication required.")
    claims = get_verifier().verify(token)
    if not claims.get("email"):
        raise AuthError("Token carries no email claim.", status_code=403)
    return claims


def current_principal(request: Request) -> Principal:
    """FastAPI dependency: the authenticated caller.

    Order matters. The token is tried first, so that in a dev environment a real
    token still identifies the real user rather than being shadowed by the
    bypass.
    """
    # The /v2 guard has already resolved this request's caller: reuse it, so
    # an API key is looked up once.
    principal = getattr(request.state, "principal", None) or _resolve_principal(
        request
    )
    # For the request's log line. Not a ContextVar: this dependency runs in a
    # threadpool with a copied context, so a value set here would never reach
    # the middleware that writes the line. The scope's state is shared.
    request.state.tenant_id = principal.tenant_id
    return principal


def _resolve_principal(request: Request) -> Principal:
    token = bearer_token(request)
    key = api_key_token(request, token)
    if key is not None:
        resolver = _api_key_resolver
        principal = resolver(key) if resolver is not None else None
        if principal is None:
            raise AuthError("Invalid API key.")
        return principal
    if token:
        from .. import sso  # noqa: PLC0415

        return sso.resolve_principal(get_verifier().verify(token), _services())

    dev = dev_principal(request)
    if dev is not None:
        from .. import sso  # noqa: PLC0415

        return sso.enforce_active(dev, _services())

    raise AuthError("Authentication required.")


def require_role(required: str):
    """Dependency factory enforcing a minimum role.

    Returns 403, not 404: the caller is authenticated and the resource exists;
    hiding that would make a permissions problem look like a bug. The refusal
    is written to the audit trail when there is one: an auditor's first
    question about access control is whether denials leave a trace.
    """

    def dependency(
        request: Request,
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if principal.can(required):
            return principal
        _deny(
            request,
            principal,
            required,
            f"Role '{principal.role}' cannot perform this action; "
            f"'{required}' or higher is required.",
        )

    return dependency


def require_exact_role(role: str):
    """Dependency factory for a power one role holds alone, whatever the
    ladder says (an admin outranks a controller and still cannot approve an
    agent's registration). A person only: an API key never passes."""

    def dependency(
        request: Request,
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if principal.role == role and not (principal.user_id or "").startswith(
            "api-key:"
        ):
            return principal
        _deny(
            request,
            principal,
            role,
            f"Only a {role} can perform this action.",
        )

    return dependency


def _deny(request: Request, principal: Principal, required: str, message: str):
    """403, written to the audit trail when there is one."""
    services = _services()
    if services is not None and services.audit is not None:
        route = request.scope.get("route")
        try:
            services.audit.emit(
                principal.context(),
                "access_denied",
                {
                    # The template, so one line covers every invoice.
                    "path": getattr(route, "path", request.url.path),
                    "method": request.method,
                    "required_role": required,
                    "actual_role": principal.role,
                },
            )
        except Exception as exc:
            from .. import sso  # noqa: PLC0415

            raise sso._unavailable(exc) from exc
    raise AuthError(message, status_code=403)
