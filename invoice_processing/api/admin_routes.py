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
from contextlib import contextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

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


class UserPatch(BaseModel):
    manager_email: str | None = Field(default=None, max_length=320)
    role: str | None = None
    is_active: bool | None = None


class SsoDomainCreate(BaseModel):
    domain: str = Field(max_length=253)
    provider: Literal["google", "microsoft"]
    default_role: Literal["viewer", "clerk"]


class ClaimsUpdate(BaseModel):
    email: str
    uid: str


def _sso_domains():
    from . import service  # noqa: PLC0415

    repo = service.get_sso_domains()
    if repo is None:
        raise HTTPException(
            status_code=503,
            detail="Single sign-on needs a database. Set DATABASE_URL or "
            "CLOUD_SQL_INSTANCE and run the Alembic migrations.",
        )
    return repo


def _directory():
    from . import service  # noqa: PLC0415

    directory = service.get_user_directory()
    if directory is None:
        raise HTTPException(
            status_code=503, detail="Managing users needs a database."
        )
    return directory


def _provisioner():
    from ..admin.provision import Provisioner  # noqa: PLC0415
    from . import service  # noqa: PLC0415

    return Provisioner(claims=service.get_claims_writer())


def _provisioning_error() -> type[Exception]:
    from ..admin.provision import ProvisioningError  # noqa: PLC0415

    return ProvisioningError


@contextmanager
def _database_required():
    """Turn "no such table" into an answer an administrator can act on.

    Every endpoint here reads the tenants and users tables. With no database
    configured the app falls back to in-memory SQLite, which has no schema, so
    these raised a bare 500 -- the admin screen showed "Internal Server Error"
    and gave no hint that the cause was configuration rather than a fault.
    """
    from sqlalchemy.exc import OperationalError  # noqa: PLC0415

    try:
        yield
    except OperationalError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "No database is configured on this deployment, so tenants and "
                "users cannot be managed here. Set DATABASE_URL or "
                "CLOUD_SQL_INSTANCE and run the Alembic migrations."
            ),
        ) from exc


def _own_tenant(principal: Principal) -> dict:
    with _database_required():
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
    with _database_required():
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


@router.patch("/api/admin/users/{user_id}")
def patch_user(
    user_id: str,
    request: UserPatch,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    """Set a user's manager, role or active flag (never one's own role/flag)."""
    from . import service  # noqa: PLC0415

    # Omitted and null differ: null clears the manager, omitted is a mistake.
    fields = request.model_fields_set
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    record: dict = {"id": user_id}
    if "manager_email" in fields:
        try:
            with _database_required():
                changed = _provisioner().set_manager(
                    principal.tenant_id, user_id, request.manager_email
                )
        except LookupError:
            raise HTTPException(
                status_code=404, detail="Unknown user"
            ) from None
        except _provisioning_error() as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        previous = changed.pop("previous_manager_email")
        service.get_audit().emit(
            principal.context(),
            "user_updated",
            {"user_id": user_id, "manager_email": changed["manager_email"]},
            before={"manager_email": previous},
        )
        record.update(changed)
    if "role" in fields or "is_active" in fields:
        from .auth import ROLE_RANK  # noqa: PLC0415

        directory = _directory()
        try:
            current = next(
                u
                for u in _provisioner().list_users(principal.tenant_id)
                if u["id"] == user_id
            )
        except StopIteration:
            raise HTTPException(
                status_code=404, detail="Unknown user"
            ) from None
        if current["email"].lower() == principal.email.lower():
            raise HTTPException(
                status_code=409,
                detail="You cannot change your own role or deactivate yourself; ask another admin.",
            )
        try:
            if "role" in fields:
                if request.role not in ROLE_RANK:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Unknown role {request.role!r}.",
                    )
                changed = directory.set_role(
                    principal.tenant_id, user_id, request.role
                )
                service.get_audit().emit(
                    principal.context(),
                    "user_role_changed",
                    {
                        "user_id": user_id,
                        "email": changed["email"],
                        "role": request.role,
                    },
                    before={"role": changed["previous_role"]},
                )
                record.update(changed)
            if "is_active" in fields:
                changed = directory.set_active(
                    principal.tenant_id, user_id, bool(request.is_active)
                )
                service.get_audit().emit(
                    principal.context(),
                    "user_reactivated"
                    if request.is_active
                    else "user_deactivated",
                    {"user_id": user_id, "email": changed["email"]},
                )
                record.update(changed)
        except LookupError:
            raise HTTPException(
                status_code=404, detail="Unknown user"
            ) from None
        if current.get("uid"):
            # The claim is a cache of the row; keep it in step where it is live.
            fresh = directory.find(principal.tenant_id, current["email"])
            service.get_claims_writer().set_claims(
                current["uid"],
                {"tenant_id": principal.tenant_id, "role": fresh["role"]},
            )
    record.pop("previous_role", None)
    record.pop("previous_active", None)
    record.pop("tenant_name", None)
    return record


@router.get("/api/admin/sso-domains")
def list_sso_domains(
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    return {"domains": _sso_domains().list(principal.tenant_id)}


@router.post("/api/admin/sso-domains", status_code=201)
def add_sso_domain(
    request: SsoDomainCreate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    """Let a company's verified accounts sign in here.

    No proof of ownership: a bound domain admits only Google-verified
    accounts of exactly that domain, and Google verifies only addresses it
    hosts, so binding a domain you do not own admits nobody.
    """
    from .. import sso  # noqa: PLC0415
    from ..db.repositories import DomainTaken  # noqa: PLC0415
    from . import service  # noqa: PLC0415

    try:
        domain = sso.normalize_domain(request.domain)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        record = _sso_domains().add(
            principal.tenant_id,
            domain=domain,
            provider=request.provider,
            default_role=request.default_role,
            created_by=principal.email,
        )
    except DomainTaken:
        # Which organisation holds it is their business, not the caller's.
        raise HTTPException(
            status_code=409,
            detail="That domain is already connected to another organisation.",
        ) from None
    service.get_audit().emit(
        principal.context(),
        "sso_domain_added",
        {
            "domain": domain,
            "provider": request.provider,
            "default_role": request.default_role,
        },
    )
    return record


@router.delete("/api/admin/sso-domains/{domain_id}", status_code=204)
def remove_sso_domain(
    domain_id: str,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> Response:
    from . import service  # noqa: PLC0415

    repo = _sso_domains()
    before = next(
        (d for d in repo.list(principal.tenant_id) if d["id"] == domain_id),
        None,
    )
    if before is None or not repo.remove(principal.tenant_id, domain_id):
        raise HTTPException(status_code=404, detail="Unknown domain")
    service.get_audit().emit(
        principal.context(), "sso_domain_removed", {"domain": before["domain"]}
    )
    return Response(status_code=204)


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
