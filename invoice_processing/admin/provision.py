"""Tenant and user provisioning.

Authentication trusts the `tenant_id` and `role` custom claims inside an
Identity Platform ID token. There is no console field for those, so this is the
only supported way to create them -- and because they are what grant access,
getting this wrong is how a user ends up reading the wrong company's invoices.

Three decisions carry that weight:

* **Claims are written last.** The tenant row, then the user row, then the
  claims. A failure part-way leaves a user who cannot log in, which is a support
  ticket. The opposite order leaves a user who *can* log in to a tenant that
  does not exist, which is a broken session at best and a confusing one at
  worst. Failing towards "no access" is the only acceptable direction.

* **A slug is validated against the inbound-email rules, not just the database.**
  The slug becomes the local part of `<slug>@<inbound-domain>`, and
  `ingest.tenant_for_recipient` lowercases the address and truncates at `+`. So
  a slug containing `+` or an uppercase letter would provision a tenant whose
  email address silently routes somewhere else -- or nowhere. The constraint
  lives in two modules, so a test ties them together.

* **An unknown role is refused, not defaulted.** `ROLE_RANK` gives an
  unrecognised role rank -1, so provisioning a typo would create a user who
  authenticates successfully and is then denied everything, with no obvious
  cause. Better to fail here, where someone is watching.

The claims writer is injectable, so the whole flow is testable without Google
credentials. `IdentityToolkitClaims` is the real one.

    python -m invoice_processing.admin.provision create-tenant \\
        --slug acme --name "Acme Pty Ltd" --region au
    python -m invoice_processing.admin.provision add-user \\
        --tenant acme --email ap@acme.test --role clerk --uid <firebase-uid>
    python -m invoice_processing.admin.provision list
    python -m invoice_processing.admin.provision suspend-tenant \\
        --tenant acme --operator <you> --reason "contract ended"
    python -m invoice_processing.admin.provision reactivate-tenant \\
        --tenant acme --operator <you>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..api.auth import ROLE_RANK
from ..db.models import Tenant, User
from ..db.session import tenant_session

logger = logging.getLogger(__name__)

# Lowercase alphanumerics and hyphens. Deliberately narrower than the database
# column: this string becomes an email local part and a storage path segment.
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class ProvisioningError(RuntimeError):
    """Refused before anything was written."""


class UidTaken(ProvisioningError):
    """The Identity Platform account is already linked to another user row."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_slug(slug: str) -> str:
    """Refuse a slug that would break inbound email or object keys.

    `tenant_for_recipient` lowercases the recipient and cuts at `+`, so an
    uppercase or plus-containing slug would provision a tenant whose email
    address resolves to a different tenant, or to none.
    """
    if not slug:
        raise ProvisioningError("A tenant slug is required.")
    if not SLUG_PATTERN.match(slug):
        raise ProvisioningError(
            f"Invalid slug {slug!r}. Use 2-63 lowercase letters, digits and "
            "hyphens, starting with a letter or digit. The slug becomes the "
            "local part of the tenant's inbound email address, so uppercase "
            "and '+' would route mail to the wrong place."
        )
    return slug


def validate_role(role: str) -> str:
    """Refuse a role the authorization layer would not recognise.

    An unknown role ranks -1 and is denied everything, so a typo here would
    create a user who logs in and can do nothing, with nothing to point at.
    """
    if role not in ROLE_RANK:
        raise ProvisioningError(
            f"Unknown role {role!r}. Valid roles: {', '.join(sorted(ROLE_RANK))}."
        )
    return role


def validate_email(address: str) -> str:
    if not address or address.count("@") != 1 or address.startswith("@"):
        raise ProvisioningError(f"Invalid email address: {address!r}")
    return address.strip().lower()


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


@runtime_checkable
class ClaimsWriter(Protocol):
    """Sets custom claims on an Identity Platform user."""

    def set_claims(self, uid: str, claims: dict[str, Any]) -> None: ...


class IdentityToolkitClaims:
    """Writes custom claims through the Identity Toolkit REST API.

    Used instead of the Firebase Admin SDK, which is not a dependency here. The
    caller's own service-account credentials are used, so provisioning requires
    an operator who already has them -- it is not something the running service
    can do to itself.
    """

    ENDPOINT = "https://identitytoolkit.googleapis.com/v1/projects/{project}/accounts:update"

    def __init__(self, project_id: str | None = None):
        self.project_id = project_id or os.getenv("GOOGLE_CLOUD_PROJECT", "")
        if not self.project_id:
            raise ProvisioningError(
                "GOOGLE_CLOUD_PROJECT is not set; there is no project to "
                "provision into."
            )

    def set_claims(self, uid: str, claims: dict[str, Any]) -> None:
        import google.auth  # noqa: PLC0415
        import requests  # noqa: PLC0415
        from google.auth.transport.requests import Request  # noqa: PLC0415

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(Request())

        response = requests.post(
            self.ENDPOINT.format(project=self.project_id),
            headers={"Authorization": f"Bearer {credentials.token}"},
            json={
                "localId": uid,
                # The API takes the claims as a JSON *string*, not an object.
                "customAttributes": json.dumps(claims),
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise ProvisioningError(
                f"Identity Platform refused the claim update for {uid}: "
                f"{response.status_code} {response.text}"
            )


class DryRunClaims:
    """Records what would be written. The default, so a mistake costs nothing."""

    def __init__(self):
        self.written: list[tuple[str, dict]] = []

    def set_claims(self, uid: str, claims: dict[str, Any]) -> None:
        self.written.append((uid, claims))
        logger.info("[dry-run] would set claims on %s: %s", uid, claims)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


class Provisioner:
    def __init__(
        self, claims: ClaimsWriter | None = None, url: str | None = None
    ):
        self.claims = claims if claims is not None else DryRunClaims()
        self.url = url

    # -- tenants -----------------------------------------------------------

    def create_tenant(self, slug: str, name: str, region: str = "au") -> dict:
        validate_slug(slug)
        if not name.strip():
            raise ProvisioningError("A tenant name is required.")

        with tenant_session("", self.url) as session:
            if session.scalar(select(Tenant).where(Tenant.slug == slug)):
                raise ProvisioningError(f"Tenant {slug!r} already exists.")
            tenant = Tenant(slug=slug, name=name.strip(), region=region)
            session.add(tenant)
            session.flush()
            record = {
                "id": tenant.id,
                "slug": tenant.slug,
                "name": tenant.name,
                "region": tenant.region,
            }
        logger.info("Created tenant %s (%s)", record["slug"], record["id"])
        return record

    def list_tenants(self) -> list[dict]:
        with tenant_session("", self.url) as session:
            return [
                {
                    "id": t.id,
                    "slug": t.slug,
                    "name": t.name,
                    "region": t.region,
                    "status": t.status,
                }
                for t in session.scalars(
                    select(Tenant).order_by(Tenant.slug)
                ).all()
            ]

    def _tenant_by_slug(self, slug: str) -> Tenant:
        """The tenant a grant of access is for; only an active one.

        Both callers (add-user, set-claims) hand out access, and access to a
        suspended or purged tenant is exactly what offboarding withdrew.
        """
        with tenant_session("", self.url) as session:
            tenant = session.scalar(select(Tenant).where(Tenant.slug == slug))
            if tenant is None:
                raise ProvisioningError(
                    f"No tenant {slug!r}. Create it before adding users."
                )
            if tenant.status != "active":
                raise ProvisioningError(
                    f"Tenant {slug!r} is {tenant.status}; access can only be "
                    "granted in an active tenant."
                )
            session.expunge(tenant)
            return tenant

    def tenant_by_id(self, tenant_id: str) -> dict | None:
        """One tenant, by its id.

        The API knows a caller's tenant only by id (that is what the token
        carries), while the provisioning methods key on slug -- this bridges
        the two without exposing a listing of every tenant.
        """
        with tenant_session("", self.url) as session:
            tenant = session.scalar(
                select(Tenant).where(Tenant.id == tenant_id)
            )
            if tenant is None:
                return None
            return {
                "id": tenant.id,
                "slug": tenant.slug,
                "name": tenant.name,
                "region": tenant.region,
                "status": tenant.status,
            }

    def list_users(self, tenant_id: str) -> list[dict]:
        """Users in one tenant.

        Takes a tenant id rather than a slug, and filters on it, so a caller
        cannot enumerate another tenant's people by guessing a slug.
        """
        with tenant_session(tenant_id, self.url) as session:
            users = session.scalars(
                select(User)
                .where(User.tenant_id == tenant_id)
                .order_by(User.email)
            ).all()
            return [
                {
                    "id": u.id,
                    "email": u.email,
                    "role": u.role,
                    "display_name": u.display_name,
                    "is_active": u.is_active,
                    "manager_email": u.manager_email,
                    "uid": u.uid,
                    "last_login_at": u.last_login_at.isoformat()
                    if u.last_login_at
                    else None,
                    "created_at": u.created_at.isoformat()
                    if u.created_at
                    else None,
                }
                for u in users
            ]

    # -- users -------------------------------------------------------------

    def set_manager(
        self, tenant_id: str, user_id: str, manager_email: str | None
    ) -> dict:
        """Point a user at the person overdue approvals escalate to.

        Both ends are looked up inside the one tenant, so neither a user id nor
        a manager email from elsewhere can resolve. Unknown user is a
        LookupError (the API answers 404); a bad manager is a ProvisioningError.
        Returns the user with `previous_manager_email` for the audit trail.
        """
        manager = validate_email(manager_email) if manager_email else None
        with tenant_session(tenant_id, self.url) as session:
            user = session.scalar(
                select(User).where(
                    User.tenant_id == tenant_id, User.id == user_id
                )
            )
            if user is None:
                raise LookupError(user_id)
            if manager is not None:
                if manager == user.email.casefold():
                    raise ProvisioningError(
                        "A person cannot be their own manager."
                    )
                role = session.scalar(
                    select(User.role).where(
                        User.tenant_id == tenant_id, User.email == manager
                    )
                )
                if role is None:
                    raise ProvisioningError(
                        f"{manager} is not a user of this account."
                    )
                # Escalations land on the manager; one who cannot approve
                # would receive steps they are unable to decide.
                if role not in ("approver", "admin"):
                    raise ProvisioningError(
                        f"{manager} is a {role}, so they cannot be a manager "
                        "for approvals; only an approver or admin can approve."
                    )
            previous = user.manager_email
            user.manager_email = manager
            session.flush()
            return {
                "id": user.id,
                "email": user.email,
                "manager_email": manager,
                "previous_manager_email": previous,
            }

    def add_user(
        self,
        tenant_slug: str,
        email: str,
        role: str = "clerk",
        uid: str | None = None,
        display_name: str | None = None,
    ) -> dict:
        """Create the user row, then grant access by writing claims.

        The ordering is the safety property: if the claim write fails, the user
        simply cannot log in. Writing claims first would produce a token that
        authenticates into a tenant with no user record behind it.
        """
        email = validate_email(email)
        validate_role(role)
        tenant = self._tenant_by_slug(tenant_slug)

        with tenant_session(tenant.id, self.url) as session:
            existing = session.scalar(
                select(User).where(
                    User.tenant_id == tenant.id, User.email == email
                )
            )
            if existing is not None:
                raise ProvisioningError(
                    f"{email} already exists in tenant {tenant_slug!r}."
                )
            user = User(
                tenant_id=tenant.id,
                email=email,
                role=role,
                uid=uid or None,
                display_name=display_name,
            )
            session.add(user)
            session.flush()
            user_id = user.id

        claims = {"tenant_id": tenant.id, "role": role}
        if uid:
            # Last, and only with a real Identity Platform uid. Without one the
            # row exists but grants nothing, which is the safe half-state.
            self.claims.set_claims(uid, claims)
        else:
            logger.warning(
                "No --uid given for %s: the database row exists but no claims "
                "were set, so this user cannot sign in yet. Re-run "
                "'set-claims' once the Identity Platform account exists.",
                email,
            )

        return {
            "user_id": user_id,
            "email": email,
            "role": role,
            "tenant_id": tenant.id,
            "tenant_slug": tenant.slug,
            "claims": claims,
            "claims_written": bool(uid),
        }

    def set_claims(self, tenant_slug: str, email: str, uid: str) -> dict:
        """Attach an Identity Platform account to an existing user row."""
        email = validate_email(email)
        tenant = self._tenant_by_slug(tenant_slug)

        try:
            with tenant_session(tenant.id, self.url) as session:
                user = session.scalar(
                    select(User).where(
                        User.tenant_id == tenant.id, User.email == email
                    )
                )
                if user is None:
                    raise ProvisioningError(
                        f"No user {email} in tenant {tenant_slug!r}. Add them first."
                    )
                role = user.role
                user.uid = uid
        except IntegrityError as exc:
            # uq_user_uid is the arbiter; pg8000 names it, SQLite the columns.
            text = str(exc.orig)
            if "uq_user_uid" in text or "users.uid" in text:
                raise UidTaken(
                    "That account is already linked to another person."
                ) from exc
            raise

        claims = {"tenant_id": tenant.id, "role": role}
        self.claims.set_claims(uid, claims)
        return {"email": email, "uid": uid, "claims": claims}

    def inbox_for(self, slug: str) -> str:
        """The address vendors send invoices to."""
        domain = os.getenv("INBOUND_EMAIL_DOMAIN", "")
        if not domain:
            raise ProvisioningError("INBOUND_EMAIL_DOMAIN is not set.")
        return f"{validate_slug(slug)}@{domain}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provision", description="Provision tenants and users."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write Identity Platform claims. Without this the claim "
        "write is a dry run and only the database is touched.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-tenant")
    create.add_argument("--slug", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--region", default="au")

    add = subparsers.add_parser("add-user")
    add.add_argument("--tenant", required=True)
    add.add_argument("--email", required=True)
    add.add_argument("--role", default="clerk", choices=sorted(ROLE_RANK))
    add.add_argument(
        "--uid", help="Identity Platform uid. Omit to defer claims."
    )
    add.add_argument("--display-name")

    claims = subparsers.add_parser("set-claims")
    claims.add_argument("--tenant", required=True)
    claims.add_argument("--email", required=True)
    claims.add_argument("--uid", required=True)

    suspend = subparsers.add_parser("suspend-tenant")
    suspend.add_argument("--tenant", required=True)
    suspend.add_argument("--operator", required=True)
    suspend.add_argument("--reason", required=True)

    reactivate = subparsers.add_parser("reactivate-tenant")
    reactivate.add_argument("--tenant", required=True)
    reactivate.add_argument("--operator", required=True)

    subparsers.add_parser("list")
    return parser


def main(argv: list[str] | None = None) -> int:
    from .. import offboarding  # noqa: PLC0415 - only the CLI needs it

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)

    writer: ClaimsWriter = (
        IdentityToolkitClaims() if args.apply else DryRunClaims()
    )
    if not args.apply:
        logger.info("Dry run: no Identity Platform claims will be written.")
    provisioner = Provisioner(claims=writer)

    try:
        if args.command == "create-tenant":
            record = provisioner.create_tenant(
                args.slug, args.name, args.region
            )
            print(json.dumps(record, indent=2))
            try:
                print(f"Inbound address: {provisioner.inbox_for(args.slug)}")
            except ProvisioningError:
                print("Inbound address: (set INBOUND_EMAIL_DOMAIN to see it)")
        elif args.command == "add-user":
            print(
                json.dumps(
                    provisioner.add_user(
                        args.tenant,
                        args.email,
                        args.role,
                        args.uid,
                        args.display_name,
                    ),
                    indent=2,
                )
            )
        elif args.command == "set-claims":
            print(
                json.dumps(
                    provisioner.set_claims(args.tenant, args.email, args.uid),
                    indent=2,
                )
            )
        elif args.command == "suspend-tenant":
            print(
                json.dumps(
                    offboarding.suspend(
                        args.tenant, operator=args.operator, reason=args.reason
                    ),
                    indent=2,
                )
            )
        elif args.command == "reactivate-tenant":
            print(
                json.dumps(
                    offboarding.reactivate(args.tenant, operator=args.operator),
                    indent=2,
                )
            )
        elif args.command == "list":
            print(json.dumps(provisioner.list_tenants(), indent=2))
    except (ProvisioningError, offboarding.OffboardingError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
