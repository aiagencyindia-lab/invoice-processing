"""The agent registry (FR-28.1, U2, B1, B19): an actor is an agent once an
admin registers it here, bound to the principal it acts as."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ...db.models import ApiKey
from ...db.session import tenant_session

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AgentConflict(Exception):
    """The principal already stands for an agent, or cannot be told apart."""


def _email(value, field: str) -> str:
    if not isinstance(value, str) or not EMAIL.match(value.strip()):
        raise ValueError(f"{field} must be an email address.")
    return value.strip()


def _api_key(tenant_id: str, key_id: str, url) -> tuple[str, str]:
    """The principal id and email `apikeys.resolve` gives this live key."""
    with tenant_session(tenant_id, url) as s:
        live = (ApiKey.tenant_id == tenant_id, ApiKey.revoked_at.is_(None))
        key = s.scalars(
            select(ApiKey).where(*live, ApiKey.id == key_id)
        ).first()
        if key is None:
            raise LookupError("Unknown API key.")
        # The rules know an actor by the casefolded email it writes; two live
        # keys whose names casefold alike write the same one.
        names = s.scalars(
            select(ApiKey.name).where(*live, ApiKey.id != key_id)
        ).all()
        if any(n.casefold() == key.name.casefold() for n in names):
            raise AgentConflict(
                "Another live key has this name; rename one before "
                "registering it."
            )
        return f"api-key:{key.id}", f"{key.name}@api-key.{tenant_id}"


def register(ctx, body: dict, *, store, audit, url=None) -> dict:
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("name is required.")
    owner = _email(body.get("owner"), "owner")
    principal = body.get("principal")
    if principal is None:
        kind, principal_id, email = "external", None, None
    elif principal.get("api_key_id"):
        kind = "api_key"
        principal_id, email = _api_key(
            ctx.tenant_id, principal["api_key_id"], url
        )
    elif principal.get("user_id"):
        kind, principal_id = "user", principal["user_id"]
        if principal_id.startswith("api-key:"):
            raise ValueError("Register an API key by its api_key_id.")
        email = _email(principal.get("email"), "principal.email")
    else:
        raise ValueError(
            "principal needs an api_key_id, or a user_id and email."
        )
    if principal_id and (
        store.by_principal(ctx.tenant_id, principal_id)
        or store.by_emails(ctx.tenant_id, [email])
    ):
        raise AgentConflict("That principal is already an agent.")
    try:
        agent = store.register(
            ctx,
            name=name,
            owner=owner,
            kind=kind,
            principal_id=principal_id,
            principal_email=email,
            description=body.get("description"),
        )
    except IntegrityError:
        raise AgentConflict("That principal is already an agent.") from None
    audit.emit(
        ctx,
        "agent_registered",
        {"name": name, "kind": kind, "owner": agent["owner"]},
    )
    return agent


def revoke(ctx, agent_id: str, reason: str, *, store, audit) -> dict:
    agent = store.get(ctx.tenant_id, agent_id)
    if agent is None:
        raise LookupError("Unknown agent.")
    if agent["status"] == "revoked":
        return agent
    agent = store.revoke(ctx, agent_id, reason)
    if agent is None:  # deleted since the read above
        raise LookupError("Unknown agent.")
    from . import mandates  # noqa: PLC0415 - mandates imports this package

    mandates.reseal(ctx, store, agent.pop("revoked_mandates"))
    audit.emit(ctx, "agent_revoked", {"name": agent["name"], "reason": reason})
    return agent
