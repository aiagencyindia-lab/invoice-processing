"""Tenant and user administration, without a terminal.

Provisioning was CLI-only, so onboarding a colleague meant someone with shell
access running a command. These endpoints put the same `Provisioner` behind the
UI — with one deliberate asymmetry.

**Managing users is a tenant-admin action. Creating tenants is not.**

An admin here is a customer's administrator, not the platform operator. If
tenant creation were open to them, any customer could mint tenants inside your
system — new namespaces, new inbound-email addresses, new storage prefixes,
none of them accounted for by anyone. So user management is always available to
an admin and always scoped to their own tenant, while tenant creation is gated
behind an explicit environment flag that is off by default. Where that flag is
unset the endpoint explains itself rather than 404-ing.

The same reasoning shapes the read side: `GET /api/admin/tenants` returns the
caller's own tenant only. A listing of every tenant is a customer list, and one
customer's admin has no business reading it.
"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import Principal, require_role

# `admin.provision` imports `api.auth`, so importing it at module scope closes
# a cycle through the api package. It is imported inside the helpers instead.


router = APIRouter()


def tenant_provisioning_enabled() -> bool:
    """Whether this deployment lets tenants be created over the API.

    Off by default and fail-closed: the cost of it being unavailable is one
    CLI command by an operator, and the cost of it being open is any customer
    admin creating namespaces in your system.
    """
    return os.getenv("ALLOW_TENANT_PROVISIONING", "").lower() in (
        "1",
        "true",
        "yes",
    )


class TenantCreate(BaseModel):
    slug: str
    name: str
    region: str = "au"


class UserCreate(BaseModel):
    email: str
    role: str = "clerk"
    display_name: str | None = None
    # Identity Platform uid. Without one the row is created but grants nothing,
    # which is the safe half-state.
    uid: str | None = None


class ClaimsUpdate(BaseModel):
    email: str
    uid: str


def _provisioner():
    from ..admin.provision import Provisioner  # noqa: PLC0415
    from . import service  # noqa: PLC0415

    return Provisioner(claims=service.get_claims_writer())


def _provisioning_error() -> type[Exception]:
    from ..admin.provision import ProvisioningError  # noqa: PLC0415

    return ProvisioningError


def _own_tenant(principal: Principal) -> dict:
    tenant = _provisioner().tenant_by_id(principal.tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Your tenant has no record in the database. It may not have "
                "been provisioned yet."
            ),
        )
    return tenant


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------


@router.get("/api/admin/tenants")
def read_own_tenant(
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    """The caller's own tenant. Never a listing of all of them."""
    from .. import paths  # noqa: PLC0415

    tenant = _own_tenant(principal)
    domain = os.getenv("INBOUND_EMAIL_DOMAIN", "")
    return {
        "tenant": {
            **tenant,
            # The address suppliers email invoices to. Shown here because it is
            # derived from the slug and nowhere else surfaces it.
            "inbox": f"{tenant['slug']}@{domain}" if domain else None,
            "purchase_order_file": paths.purchase_order_file(tenant["id"]).name,
        },
        "can_create_tenants": tenant_provisioning_enabled(),
    }


@router.post("/api/admin/tenants")
def create_tenant(
    request: TenantCreate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    """Create a tenant. Disabled unless the deployment opts in.

    See the module docstring: an admin here is a customer's administrator, and
    tenant creation is a platform operation.
    """
    if not tenant_provisioning_enabled():
        raise HTTPException(
            status_code=403,
            detail=(
                "Creating tenants is not enabled on this deployment. It is a "
                "platform operation rather than a tenant one. An operator can "
                "run: python -m invoice_processing.admin.provision create-tenant"
            ),
        )
    try:
        return {
            "tenant": _provisioner().create_tenant(
                request.slug, request.name, request.region
            )
        }
    except _provisioning_error() as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/api/admin/users")
def list_users(
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    """Users in the caller's own tenant.

    Filtered on the tenant id from the token, so there is no slug or id a
    caller could supply to enumerate someone else's people.
    """
    users = _provisioner().list_users(principal.tenant_id)
    return {
        "users": users,
        "count": len(users),
        "roles": ["viewer", "clerk", "approver", "admin"],
    }


@router.post("/api/admin/users")
def create_user(
    request: UserCreate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    """Add a user to the caller's own tenant.

    The tenant is taken from the token, never the body — the same rule the rest
    of the API follows, and the reason no endpoint accepts a `tenant_id`.

    Claims are written last, so a failure leaves a user who cannot sign in
    rather than one who can sign in to a tenant that does not exist.
    """
    from . import service  # noqa: PLC0415

    tenant = _own_tenant(principal)
    from ..admin.provision import validate_email, validate_role  # noqa: PLC0415

    try:
        validate_email(request.email)
        validate_role(request.role)
        record = _provisioner().add_user(
            tenant["slug"],
            request.email,
            request.role,
            request.uid,
            request.display_name,
        )
    except _provisioning_error() as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    service.get_audit().emit(
        principal.context(),
        "user_created",
        {"email": record["email"], "role": record["role"]},
    )
    if not record["claims_written"]:
        record["warning"] = (
            "Created, but no Identity Platform account was linked, so this "
            "user cannot sign in yet. Add their uid to finish."
        )
    return record


@router.post("/api/admin/users/claims")
def set_user_claims(
    request: ClaimsUpdate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    """Link an Identity Platform account to an existing user, granting access.

    The role comes from the stored user rather than the request: the database
    is the record of who is what, and taking it from the body would let a
    caller grant a role they never assigned.
    """
    from . import service  # noqa: PLC0415

    tenant = _own_tenant(principal)
    try:
        record = _provisioner().set_claims(
            tenant["slug"], request.email, request.uid
        )
    except _provisioning_error() as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    service.get_audit().emit(
        principal.context(),
        "user_claims_set",
        {"email": record["email"], "role": record["claims"]["role"]},
    )
    return {
        **record,
        "applied": service.claims_are_live(),
        "note": (
            None
            if service.claims_are_live()
            else "Dry run: claims were not written. Set APPLY_IDENTITY_CLAIMS=1 "
            "to grant access for real."
        ),
    }
