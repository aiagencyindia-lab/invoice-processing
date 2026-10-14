# Microsoft Sign-in Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Microsoft 365 user joins their Ledgerly tenant through an Identity Platform OIDC provider created for their Entra tenant, and nobody from any other Entra tenant can, whatever their email says.

**Architecture:** The operator binds a domain to a tenant *and* an OIDC provider id (`bind-microsoft`). `sso.resolve_principal` admits an `oidc.*` token only when its provider id and email domain both match a Microsoft binding, through one join helper shared with Google. A public lookup (`GET /api/sso/microsoft?email=`) tells the login page which provider to open.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/Alembic, Firebase JS SDK 13 (`OAuthProvider`), Next.js 16.

**Spec:** `docs/superpowers/specs/2026-10-14-microsoft-sign-in-design.md`

## Global Constraints

- Trust rule: a token without a tenant claim joins through a Microsoft binding only if `firebase.sign_in_provider == binding.idp_provider_id` **and** the email's domain equals `binding.domain`.
- Only the operator sets `idp_provider_id`; the admin API accepts `provider: "google"` only.
- Provider id format: `^oidc\.[a-z0-9-]{1,40}$`.
- `microsoft.com` (generic provider) stays refused with the existing sentence "Microsoft sign-in is not enabled yet."
- Refusal sentence for a non-matching OIDC token: `This Microsoft account's organisation isn't connected to Ledgerly.` (403)
- Lookup 404 sentence: `Microsoft sign-in isn't set up for {domain}. Ask your administrator.` (`{domain}` is the address's domain, or `that address` when there is none)
- `email_verified` is required for every provider except `oidc.*`.
- New migration revises `d1a2b3c4e5f6`; revision id `a8d4e6f1b2c3`.
- `ruff` line length 80; every `# noqa` carries its reason; never `git commit -a`.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Rulings made while planning

- **The frontend's Microsoft button cannot be covered by e2e**: the e2e stack runs with no Firebase config, so the login page never renders the sign-in buttons. Covered instead by Python contract tests over `SignIn.tsx`/`auth.ts` and by the live test. Cost if wrong: a broken button is caught only live.
- **`bind-microsoft`'s audit row is emitted after the binding commits**, as the admin route already does for Google bindings, not in its transaction. Cost if wrong: a crash between the two leaves a binding without its event (the admin route has the same gap).
- **`email_verified` stays required for providers other than Google and OIDC too** (keeps today's refusal sentence for password accounts).

## Review Focus

1. An `oidc.*` token whose email has no domain (or an uppercase domain) → refused or matched case-insensitively, never a crash. Pinned in Task 3.
2. Two Microsoft bindings in different tenants with different providers: each provider only reaches its own tenant. Pinned in Task 3.
3. The lookup called with an email containing `+` or uppercase → same answer as the plain lower-case address. Pinned in Task 4.
4. An existing Microsoft-bound user (row already present) signing in again via the OIDC provider → linked, not duplicated, claims rewritten. Pinned in Task 3.
5. A Google binding row (no `idp_provider_id`) never matches an `oidc.*` token whose provider string is empty-ish. Pinned in Task 3.

---

## File Structure

| File | Change |
|---|---|
| `invoice_processing/db/migrations/versions/a8d4e6f1b2c3_sso_idp_provider_id.py` | Create: the column |
| `invoice_processing/db/models.py` | `SsoDomain.idp_provider_id` |
| `invoice_processing/db/repositories.py` | `PgSsoDomainRepository.add/_dict`, `PgUserDirectory.domain` carry it |
| `invoice_processing/admin/provision.py` | `Provisioner.bind_microsoft` + `bind-microsoft` subcommand |
| `invoice_processing/sso.py` | OIDC path, shared `_join` helper |
| `invoice_processing/api/signup.py` | `GET /api/sso/microsoft` |
| `invoice_processing/api/admin_routes.py` | `SsoDomainCreate.provider: Literal["google"]` |
| `frontend/src/lib/auth.ts`, `frontend/src/components/SignIn.tsx`, `frontend/src/app/admin/page.tsx`, `frontend/src/lib/api.ts` | Button, provider sign-in, admin display |
| Tests | `tests/test_sso.py`, `tests/test_sso_repository.py`, `tests/test_provisioning.py`, `tests/test_signup_api.py`, `tests/test_admin_api.py`, `tests/test_tenant_isolation_api.py`, `tests/test_frontend_reaches_the_flow.py`, `tests/test_gcs_and_migrations.py`, `tests/test_restore_check.py` |
| `SECURITY.md`, `deployment/MICROSOFT_SIGN_IN.md` | Docs |

---

### Task 1: `idp_provider_id` in the schema and repositories

**Files:** Create the migration; modify `db/models.py` (~line 731), `db/repositories.py` (`PgSsoDomainRepository._dict`/`add` ~3261-3300, `PgUserDirectory.domain` ~3213); tests `tests/test_sso_repository.py`, `tests/test_gcs_and_migrations.py`, `tests/test_restore_check.py`.

**Interfaces — Produces:** `PgSsoDomainRepository.add(tenant_id, *, domain, provider, default_role, created_by, idp_provider_id=None, idp_tenant_id=None) -> dict`; `_dict` and `PgUserDirectory.domain()` results gain `"idp_provider_id"` (and `_dict` gains `"idp_tenant_id"`).

- [ ] **Step 1: Failing tests.** Append to `tests/test_sso_repository.py` (uses its `db` fixture and `ACME`):

```python
class TestMicrosoftBinding:
    def test_the_provider_id_round_trips(self, db):
        repo = PgSsoDomainRepository(db)
        added = repo.add(
            ACME,
            domain="Acme.com",
            provider="microsoft",
            default_role="clerk",
            created_by="operator:vineet",
            idp_provider_id="oidc.acme",
            idp_tenant_id="11111111-2222-3333-4444-555555555555",
        )
        assert added["idp_provider_id"] == "oidc.acme"
        assert repo.list(ACME)[0]["idp_tenant_id"].startswith("1111")
        bound = PgUserDirectory(db).domain("acme.com")
        assert (bound["provider"], bound["idp_provider_id"]) == (
            "microsoft",
            "oidc.acme",
        )

    def test_a_google_binding_has_no_provider_id(self, db):
        PgSsoDomainRepository(db).add(
            ACME,
            domain="acme.com",
            provider="google",
            default_role="viewer",
            created_by="pat@acme.com",
        )
        assert PgUserDirectory(db).domain("acme.com")["idp_provider_id"] is None
```

In `tests/test_gcs_and_migrations.py` add `("a8d4e6f1b2c3", 0),` to the `test_migration_protects_exactly_the_tables_it_creates` list. In `tests/test_restore_check.py` change `test_the_head_is_the_newest_migration` to expect `"a8d4e6f1b2c3"`.

- [ ] **Step 2: Run** `./.venv/bin/python -m pytest tests/test_sso_repository.py tests/test_gcs_and_migrations.py tests/test_restore_check.py -q -p no:cacheprovider` — Expected FAIL: `TypeError ... unexpected keyword argument 'idp_provider_id'`, `IndexError` (no migration file), head mismatch.

- [ ] **Step 3: Migration** `invoice_processing/db/migrations/versions/a8d4e6f1b2c3_sso_idp_provider_id.py`:

```python
"""sso_domains.idp_provider_id: the OIDC provider a Microsoft binding trusts

A Microsoft binding admits a token only through the Identity Platform OIDC
provider created for that customer's Entra tenant; this is its id
(`oidc.<name>`). Set by the operator, never by a tenant admin.

Revision ID: a8d4e6f1b2c3
Revises: d1a2b3c4e5f6
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8d4e6f1b2c3"
down_revision: str | None = "d1a2b3c4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RLS_TABLES: list[str] = []


def upgrade() -> None:
    with op.batch_alter_table("sso_domains") as batch:
        batch.add_column(sa.Column("idp_provider_id", sa.String(64)))


def downgrade() -> None:
    with op.batch_alter_table("sso_domains") as batch:
        batch.drop_column("idp_provider_id")
```

- [ ] **Step 4: Model** in `SsoDomain` (replace the `idp_tenant_id` comment and add the column):

```python
    # The customer's Entra tenant id, for the record (Microsoft bindings).
    idp_tenant_id: Mapped[str | None] = mapped_column(String(64))
    # The Identity Platform OIDC provider a Microsoft binding trusts
    # (`oidc.<name>`); its issuer is the customer's Entra tenant. Operator-set.
    idp_provider_id: Mapped[str | None] = mapped_column(String(64))
```

- [ ] **Step 5: Repositories.** `PgSsoDomainRepository._dict` adds `"idp_provider_id": d.idp_provider_id, "idp_tenant_id": d.idp_tenant_id`; `add` gains keyword parameters `idp_provider_id: str | None = None, idp_tenant_id: str | None = None` passed to `SsoDomain(...)`; `PgUserDirectory.domain` adds `"idp_provider_id": row.idp_provider_id` to its dict.

- [ ] **Step 6: Run** the Step 2 command, then `./.venv/bin/python -m pytest tests/test_admin_api.py tests/test_sso.py -q -p no:cacheprovider`. Expected PASS.

- [ ] **Step 7: Commit** `git add` the migration, models, repositories and the three test files; message "Store the OIDC provider a Microsoft binding trusts".

---

### Task 2: `bind-microsoft`, the operator's command

**Files:** `invoice_processing/admin/provision.py`; `tests/test_provisioning.py`.

**Interfaces — Consumes:** `PgSsoDomainRepository.add(..., idp_provider_id, idp_tenant_id)` (Task 1). **Produces:** `Provisioner.bind_microsoft(tenant_slug, domain, provider_id, entra_tenant, role, operator) -> dict` (the binding dict); `PROVIDER_ID = re.compile(r"^oidc\.[a-z0-9-]{1,40}$")`.

- [ ] **Step 1: Failing tests.** Append to `tests/test_provisioning.py`:

```python
class TestBindMicrosoft:
    TID = "11111111-2222-3333-4444-555555555555"

    def _bind(self, provisioner, **kw):
        args = dict(
            tenant_slug="acme",
            domain="acme.com",
            provider_id="oidc.acme",
            entra_tenant=self.TID,
            role="clerk",
            operator="vineet",
        )
        args.update(kw)
        return provisioner.bind_microsoft(**args)

    def test_binds_and_records_it(self, provisioner, url):
        from invoice_processing.db.repositories import (  # noqa: PLC0415
            PgAuditSink,
            PgUserDirectory,
        )

        tenant_id = provisioner.create_tenant("acme", "Acme")["id"]
        record = self._bind(provisioner, domain="Acme.COM")
        assert (record["provider"], record["idp_provider_id"]) == (
            "microsoft",
            "oidc.acme",
        )
        bound = PgUserDirectory(url).domain("acme.com")
        assert bound["tenant_id"] == tenant_id
        events = PgAuditSink(url).query(tenant_id)
        assert [e.action for e in events] == ["sso_domain_bound"]
        assert events[0].after["idp_provider_id"] == "oidc.acme"
        assert events[0].after["operator"] == "vineet"

    @pytest.mark.parametrize(
        "provider_id", ["microsoft.com", "oidc.", "oidc.Acme", "saml.acme"]
    )
    def test_refuses_a_malformed_provider_id(self, provisioner, provider_id):
        provisioner.create_tenant("acme", "Acme")
        with pytest.raises(ProvisioningError, match="provider"):
            self._bind(provisioner, provider_id=provider_id)

    def test_refuses_a_public_mailbox_domain(self, provisioner):
        provisioner.create_tenant("acme", "Acme")
        with pytest.raises(ProvisioningError, match="public"):
            self._bind(provisioner, domain="outlook.com")

    def test_refuses_a_domain_already_bound(self, provisioner):
        provisioner.create_tenant("acme", "Acme")
        provisioner.create_tenant("globex", "Globex")
        self._bind(provisioner)
        with pytest.raises(ProvisioningError, match="already"):
            self._bind(provisioner, tenant_slug="globex")

    def test_refuses_a_closed_tenant(self, provisioner, url):
        from invoice_processing.db.models import Tenant  # noqa: PLC0415
        from invoice_processing.db.session import (  # noqa: PLC0415
            tenant_session,
        )

        tenant_id = provisioner.create_tenant("acme", "Acme")["id"]
        with tenant_session("", url) as session:
            session.get(Tenant, tenant_id).status = "suspended"
        with pytest.raises(ProvisioningError, match="suspended"):
            self._bind(provisioner)

    def test_the_command_line(self, provisioner, url, monkeypatch, capsys):
        provisioner.create_tenant("acme", "Acme")
        monkeypatch.setenv("DATABASE_URL", url)
        code = provision.main(
            [
                "bind-microsoft",
                "--tenant",
                "acme",
                "--domain",
                "acme.com",
                "--provider",
                "oidc.acme",
                "--entra-tenant",
                self.TID,
                "--role",
                "clerk",
                "--operator",
                "vineet",
            ]
        )
        assert code == 0
        assert "oidc.acme" in capsys.readouterr().out
```

- [ ] **Step 2: Run** `./.venv/bin/python -m pytest tests/test_provisioning.py -q -p no:cacheprovider -k BindMicrosoft` — Expected FAIL: `AttributeError: 'Provisioner' object has no attribute 'bind_microsoft'`.

- [ ] **Step 3: Implement** in `admin/provision.py` — near `SLUG_PATTERN`:

```python
# An Identity Platform OIDC provider id. Microsoft customers each get one,
# whose issuer is their own Entra tenant.
PROVIDER_ID = re.compile(r"^oidc\.[a-z0-9-]{1,40}$")
```

and a `Provisioner` method after `set_claims`:

```python
    def bind_microsoft(
        self,
        tenant_slug: str,
        domain: str,
        provider_id: str,
        entra_tenant: str,
        role: str,
        operator: str,
    ) -> dict:
        """Let a customer's Microsoft 365 users sign in to their tenant.

        Operator-only: the provider was created by the operator for this
        customer's Entra tenant, so only the operator may point a binding at
        it. A tenant admin choosing it could reach another customer's users.
        """
        from .. import sso  # noqa: PLC0415 - sso imports the API stack
        from ..db.repositories import (  # noqa: PLC0415
            DomainTaken,
            PgAuditSink,
            PgSsoDomainRepository,
        )
        from ..ports import RunContext  # noqa: PLC0415

        if not PROVIDER_ID.match(provider_id or ""):
            raise ProvisioningError(
                f"{provider_id!r} is not an OIDC provider id (oidc.<name>)."
            )
        if role not in ("viewer", "clerk"):
            raise ProvisioningError("The default role is viewer or clerk.")
        if not (entra_tenant or "").strip():
            raise ProvisioningError("The Entra tenant id is required.")
        try:
            domain = sso.normalize_domain(domain)
        except ValueError as exc:
            raise ProvisioningError(str(exc)) from exc
        tenant = self._tenant_by_slug(tenant_slug)
        try:
            record = PgSsoDomainRepository(self.url).add(
                tenant.id,
                domain=domain,
                provider="microsoft",
                default_role=role,
                created_by=f"operator:{operator}",
                idp_provider_id=provider_id,
                idp_tenant_id=entra_tenant.strip(),
            )
        except DomainTaken as exc:
            raise ProvisioningError(f"{domain} is already bound.") from exc
        # ponytail: emitted after the binding commits, like the admin route's
        # Google binding; a crash between the two loses only the event.
        PgAuditSink(self.url).emit(
            RunContext(tenant_id=tenant.id, actor=operator),
            "sso_domain_bound",
            {
                "domain": domain,
                "provider": "microsoft",
                "idp_provider_id": provider_id,
                "idp_tenant_id": entra_tenant.strip(),
                "default_role": role,
                "operator": operator,
            },
        )
        return record
```

`_tenant_by_slug` already refuses a non-active tenant (message contains the status). `sso.normalize_domain` raises "…is a public mailbox provider…" — the test matches "public".

In `build_parser`, before `subparsers.add_parser("list")`:

```python
    bind = subparsers.add_parser("bind-microsoft")
    bind.add_argument("--tenant", required=True)
    bind.add_argument("--domain", required=True)
    bind.add_argument("--provider", required=True, help="oidc.<name>")
    bind.add_argument("--entra-tenant", required=True)
    bind.add_argument("--role", default="viewer", choices=["viewer", "clerk"])
    bind.add_argument("--operator", required=True)
```

In `main`, before the `list` branch:

```python
        elif args.command == "bind-microsoft":
            print(
                json.dumps(
                    provisioner.bind_microsoft(
                        args.tenant,
                        args.domain,
                        args.provider,
                        args.entra_tenant,
                        args.role,
                        args.operator,
                    ),
                    indent=2,
                )
            )
```

Add the usage line to the module docstring:

```
    python -m invoice_processing.admin.provision bind-microsoft \\
        --tenant acme --domain acme.com --provider oidc.acme \\
        --entra-tenant <tid> --role clerk --operator <you>
```

- [ ] **Step 4: Run** the Step 2 command, then the whole file. Expected PASS.
- [ ] **Step 5: Commit** "Let the operator bind a customer's domain to its Microsoft OIDC provider".

---

### Task 3: The OIDC sign-in path

**Files:** `invoice_processing/sso.py` (~lines 230-320); `tests/test_sso.py`.

**Interfaces — Consumes:** directory `domain()` dicts with `idp_provider_id` (Task 1). **Produces:** `sso.OIDC_PREFIX = "oidc."`, `sso.MICROSOFT_NOT_CONNECTED` (the refusal sentence); private `_join(claims, services, now, email, bound) -> Principal`.

- [ ] **Step 1: Failing tests.** Append to `tests/test_sso.py`:

```python
MS_BOUND = dict(
    BOUND,
    id="d-ms",
    provider="microsoft",
    default_role="clerk",
    idp_provider_id="oidc.acme",
)


def from_entra(provider="oidc.acme", **extra):
    """An OIDC token: Entra sends no email_verified, so Firebase may not."""
    return unclaimed(
        email_verified=False,
        firebase={"sign_in_provider": provider},
        **extra,
    )


class TestMicrosoftThroughOidc:
    def test_the_bound_provider_provisions_and_writes_claims(self):
        d, c = FakeDirectory(domains=[MS_BOUND]), FakeClaims()
        p = sso.resolve_principal(from_entra(), sso.SsoServices(d, c), now=NOW)
        assert (p.tenant_id, p.role) == (ACME, "clerk")
        assert d.provisioned[0]["uid"] == "fb-9"
        assert c.written == [("fb-9", {"tenant_id": ACME, "role": "clerk"})]
        assert d.audits[0][1] == "user_provisioned_by_sso"

    def test_another_tenants_provider_is_refused_whatever_the_email(self):
        """The attacker case: an Entra tenant that is not Acme's can give a
        user an @acme.com address, but signs in through its own provider."""
        d = FakeDirectory(domains=[MS_BOUND])
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(
                from_entra(provider="oidc.evil"), services(directory=d)
            )
        assert exc.value.status_code == 403
        assert exc.value.detail == sso.MICROSOFT_NOT_CONNECTED
        assert d.provisioned == []

    def test_a_google_binding_never_admits_an_oidc_token(self):
        d = FakeDirectory(domains=[dict(BOUND, idp_provider_id=None)])
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(from_entra(), services(directory=d))
        assert exc.value.detail == sso.MICROSOFT_NOT_CONNECTED
        assert d.provisioned == []

    def test_a_microsoft_binding_never_admits_a_google_token(self):
        d = FakeDirectory(domains=[MS_BOUND])
        with pytest.raises(AuthError):
            sso.resolve_principal(unclaimed(), services(directory=d))
        assert d.provisioned == []

    def test_each_provider_reaches_only_its_own_tenant(self):
        globex = dict(
            MS_BOUND,
            id="d-gx",
            tenant_id="tenant-globex",
            domain="globex.com",
            idp_provider_id="oidc.globex",
        )
        d = FakeDirectory(domains=[MS_BOUND, globex])
        with pytest.raises(AuthError):
            sso.resolve_principal(
                from_entra(provider="oidc.globex"), services(directory=d)
            )
        p = sso.resolve_principal(
            from_entra(provider="oidc.globex", email="new@globex.com"),
            sso.SsoServices(d, FakeClaims()),
            now=NOW,
        )
        assert p.tenant_id == "tenant-globex"

    def test_the_generic_microsoft_provider_is_still_refused(self):
        d = FakeDirectory(domains=[MS_BOUND])
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(
                from_entra(provider="microsoft.com"), services(directory=d)
            )
        assert "not enabled" in exc.value.detail

    def test_an_uppercase_address_matches_its_binding(self):
        d = FakeDirectory(domains=[MS_BOUND])
        p = sso.resolve_principal(
            from_entra(email="New@ACME.com"),
            sso.SsoServices(d, FakeClaims()),
            now=NOW,
        )
        assert p.email == "new@acme.com"

    def test_an_address_with_no_domain_is_refused_not_crashed(self):
        d = FakeDirectory(domains=[MS_BOUND])
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(
                from_entra(email="no-at-sign"), services(directory=d)
            )
        assert exc.value.status_code == 403

    def test_a_returning_user_is_linked_not_duplicated(self):
        d = FakeDirectory(
            users=[dict(ROW, email="new@acme.com", uid=None)],
            domains=[MS_BOUND],
        )
        c = FakeClaims()
        p = sso.resolve_principal(from_entra(), sso.SsoServices(d, c), now=NOW)
        assert d.provisioned == []
        assert d.linked == [(ROW["id"], "fb-9")]
        assert c.written == [("fb-9", {"tenant_id": ACME, "role": p.role})]

    def test_a_directory_failure_is_a_503(self):
        d = FakeDirectory(domains=[MS_BOUND], fail=True)
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(from_entra(), services(directory=d))
        assert exc.value.status_code == 503

    def test_a_suspended_tenants_binding_provisions_no_one(self):
        d = FakeDirectory(domains=[MS_BOUND])
        d.tenant_statuses[ACME] = "suspended"
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(from_entra(), services(directory=d))
        assert exc.value.detail == sso.SUSPENDED
        assert d.provisioned == []

    def test_a_google_token_still_needs_a_verified_email(self):
        d = FakeDirectory(domains=[BOUND])
        with pytest.raises(AuthError, match="not verified"):
            sso.resolve_principal(
                unclaimed(email_verified=False), services(directory=d)
            )
```

Check `ROW` in this file for its `id`/`tenant_id`/`role` keys (it is the existing users-row fixture) and that `FakeDirectory.linked` records `(user_id, uid)`; adjust the two assertions to the real shapes if they differ.

- [ ] **Step 2: Run** `./.venv/bin/python -m pytest tests/test_sso.py -q -p no:cacheprovider -k Oidc` — Expected FAIL: `AttributeError: module 'invoice_processing.sso' has no attribute 'MICROSOFT_NOT_CONNECTED'` and refusals where joins are expected.

- [ ] **Step 3: Implement.** In `sso.py`, constants next to `GOOGLE`/`MICROSOFT`:

```python
# Identity Platform OIDC providers, one per Microsoft customer; the provider
# id is the proof of which Entra tenant a person came from.
OIDC_PREFIX = "oidc."
MICROSOFT_NOT_CONNECTED = (
    "This Microsoft account's organisation isn't connected to Ledgerly."
)
```

Replace the tail of `resolve_principal` from `# No tenant yet: the only way in is a bound company domain.` to its end with:

```python
    # No tenant yet: the only way in is a bound company domain.
    if services is None:
        return principal_from_claims(claims)  # raises the usual 403
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise AuthError("Token carries no email claim.", status_code=403)
    provider = str((claims.get("firebase") or {}).get("sign_in_provider") or "")
    oidc = provider.startswith(OIDC_PREFIX)
    # Entra sends no email_verified; for an OIDC provider the provider match
    # below is what vouches for the address (its tenant owns its users).
    if not oidc and claims.get("email_verified") is not True:
        raise AuthError("Email address is not verified.", status_code=403)
    if provider == MICROSOFT:
        raise AuthError(
            "Microsoft sign-in is not enabled yet.", status_code=403
        )
    if provider != GOOGLE and not oidc:
        return principal_from_claims(claims)  # raises the usual 403
    try:
        bound = services.directory.domain(domain_of(email))
    except Exception as exc:
        raise _unavailable(exc) from exc
    if oidc:
        if (
            bound is None
            or bound.get("provider") != "microsoft"
            or bound.get("idp_provider_id") != provider
        ):
            raise AuthError(MICROSOFT_NOT_CONNECTED, status_code=403)
    elif bound is None or bound.get("provider") != "google":
        # A binding for another provider never admits a Google account.
        return principal_from_claims(claims)  # raises the usual 403
    return _join(claims, services, now, email, bound)


def _join(
    claims: dict[str, Any],
    services: SsoServices,
    now: datetime,
    email: str,
    bound: dict,
) -> Principal:
    """Provision or link a person through a binding that admitted them."""
```

and move the existing body — from `from sqlalchemy.exc import IntegrityError  # noqa: PLC0415` through the final `return principal` — unchanged into `_join` (it uses only `claims`, `services`, `now`, `email`, `bound`).

`domain_of("no-at-sign")` returns `"no-at-sign"`; `FakeDirectory.domain` returns None for it → `MICROSOFT_NOT_CONNECTED` (403), satisfying the no-domain test.

- [ ] **Step 4: Run** `./.venv/bin/python -m pytest tests/test_sso.py tests/test_signup_api.py tests/test_admin_api.py tests/test_tenant_isolation_api.py -q -p no:cacheprovider`. Expected PASS. If an existing test asserted "not verified" for a Microsoft token, that order changed on purpose only for `oidc.*`; `microsoft.com` keeps the verified check first.
- [ ] **Step 5: Commit** "Admit a Microsoft user only through their own tenant's OIDC provider".

---

### Task 4: The lookup endpoint and the Google-only admin form

**Files:** `invoice_processing/api/signup.py`; `invoice_processing/api/admin_routes.py` (`SsoDomainCreate`, ~line 80); tests `tests/test_signup_api.py`, `tests/test_admin_api.py`, `tests/test_tenant_isolation_api.py`.

**Interfaces — Produces:** `GET /api/sso/microsoft?email=` → `{"provider_id": str}`; `signup.MICROSOFT_NOT_SET_UP` (format string with `{domain}`).

- [ ] **Step 1: Failing tests.** Append to `tests/test_signup_api.py` (its `env` fixture gives a real directory over `env["url"]`):

```python
class TestMicrosoftLookup:
    def _bind(self, env, provider="microsoft", provider_id="oidc.acme"):
        from invoice_processing.db.repositories import (  # noqa: PLC0415
            PgSsoDomainRepository,
        )

        tenant_id = "77777777-7777-7777-7777-777777777777"
        with tenant_session(tenant_id, env["url"]) as session:
            session.add(Tenant(id=tenant_id, slug="acme", name="Acme"))
        PgSsoDomainRepository(env["url"]).add(
            tenant_id,
            domain="acme.com",
            provider=provider,
            default_role="clerk",
            created_by="operator:v",
            idp_provider_id=provider_id if provider == "microsoft" else None,
        )

    def _get(self, email):
        return TestClient(app).get(
            "/api/sso/microsoft", params={"email": email}
        )

    @pytest.mark.parametrize(
        "email", ["pat@acme.com", "Pat@ACME.com", "pat+ap@acme.com"]
    )
    def test_a_microsoft_binding_names_its_provider(self, env, email):
        self._bind(env)
        response = self._get(email)
        assert response.status_code == 200
        assert response.json() == {"provider_id": "oidc.acme"}

    @pytest.mark.parametrize(
        ("email", "domain"),
        [
            ("pat@unbound.com", "unbound.com"),
            ("pat@outlook.com", "outlook.com"),
            ("not-an-address", "that address"),
        ],
    )
    def test_anything_else_is_not_set_up(self, env, email, domain):
        self._bind(env)
        response = self._get(email)
        assert response.status_code == 404
        assert response.json()["detail"] == signup.MICROSOFT_NOT_SET_UP.format(
            domain=domain
        )

    def test_a_google_binding_is_not_set_up_for_microsoft(self, env):
        self._bind(env, provider="google")
        assert self._get("pat@acme.com").status_code == 404
```

Append to `tests/test_admin_api.py` (reuse that file's admin client fixture/helper; read its top for the name):

```python
def test_a_tenant_admin_cannot_create_a_microsoft_binding(admin_client):
    response = admin_client.post(
        "/api/admin/sso-domains",
        json={"domain": "acme.com", "provider": "microsoft", "default_role": "viewer"},
    )
    assert response.status_code == 422
```

In `tests/test_tenant_isolation_api.py` `EXEMPT`, add:

```python
        "/api/sso/microsoft": "pre-auth lookup; reveals only whether a "
        "domain uses Ledgerly through Microsoft (test_signup_api.py)",
```

- [ ] **Step 2: Run** `./.venv/bin/python -m pytest tests/test_signup_api.py tests/test_admin_api.py tests/test_tenant_isolation_api.py -q -p no:cacheprovider` — Expected FAIL: 404 from FastAPI for the unknown route (detail "Not Found"), the admin route answering 201, and the isolation test passing only after the route exists (it fails first on "EXEMPT lists a route that does not exist" if such a check exists; otherwise it passes — note which in the ledger).

- [ ] **Step 3: Implement** in `api/signup.py` (add `Query` to the FastAPI import):

```python
MICROSOFT_NOT_SET_UP = (
    "Microsoft sign-in isn't set up for {domain}. Ask your administrator."
)


@router.get("/api/sso/microsoft")
def microsoft_provider(email: str = Query(max_length=320)) -> dict:
    """Which OIDC provider a work address signs in through.

    Public, before sign-in: the login page needs it to open the right
    customer's Microsoft provider. It reveals only whether a domain uses
    Ledgerly through Microsoft (SECURITY.md); a Google binding, an unbound
    or public-mailbox domain and a malformed address all answer the same.
    """
    from .. import sso  # noqa: PLC0415
    from . import auth  # noqa: PLC0415

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
    if bound is None or bound.get("provider") != "microsoft":
        raise not_set_up
    return {"provider_id": bound["idp_provider_id"]}
```

(`domain_of("pat+ap@acme.com")` is `acme.com`; `normalize_domain` lowercases.)

In `admin_routes.py`: `provider: Literal["google"]` in `SsoDomainCreate`, with the comment `# Microsoft bindings are operator-only (provision bind-microsoft).`

- [ ] **Step 4: Run** the Step 2 command. Expected PASS.
- [ ] **Step 5: Commit** "Tell the login page which Microsoft provider an address uses, and keep Microsoft bindings off the admin form".

---

### Task 5: The frontend

**Files:** `frontend/src/lib/auth.ts`, `frontend/src/components/SignIn.tsx`, `frontend/src/app/admin/page.tsx`, `frontend/src/lib/api.ts` (`SsoDomain` type); `tests/test_frontend_reaches_the_flow.py`.

**Interfaces — Consumes:** `GET /api/sso/microsoft` (Task 4). **Produces:** `signInWithMicrosoft(providerId: string, email: string): Promise<void>` in `auth.ts`.

- [ ] **Step 1: Failing contract tests.** Append to `tests/test_frontend_reaches_the_flow.py`:

```python
class TestMicrosoftSignIn:
    def test_the_button_asks_which_provider_then_signs_in_through_it(self):
        sign_in = (FRONTEND / "components" / "SignIn.tsx").read_text(
            encoding="utf-8"
        )
        auth = (FRONTEND / "lib" / "auth.ts").read_text(encoding="utf-8")
        assert "/api/sso/microsoft" in sign_in
        assert "signInWithMicrosoft" in sign_in
        assert "new mod.OAuthProvider(providerId)" in auth
        assert "login_hint" in auth

    def test_the_admin_form_offers_google_only(self):
        admin = (FRONTEND / "app" / "admin" / "page.tsx").read_text(
            encoding="utf-8"
        )
        assert 'value="microsoft"' not in admin
        assert "set up by Ledgerly support" in admin
```

- [ ] **Step 2: Run** `./.venv/bin/python -m pytest tests/test_frontend_reaches_the_flow.py -q -p no:cacheprovider -k Microsoft` — Expected FAIL.

- [ ] **Step 3: `auth.ts`.** Replace `signInWithGoogle` with a shared helper:

```ts
async function signInWith(
  make: (mod: Awaited<ReturnType<typeof client>>["mod"]) => import("firebase/auth").AuthProvider,
): Promise<void> {
  const { mod, auth } = await client();
  const provider = make(mod);
  try {
    await mod.signInWithPopup(auth, provider);
  } catch (caught) {
    if (!POPUP_UNAVAILABLE.has(codeOf(caught))) throw asSignInError(caught);
    // The popup could not open -- an extension, a mobile browser, a corporate
    // policy. Redirecting navigates in this same tab, which nothing blocks.
    // It is the fallback rather than the default because it round-trips
    // through the Firebase auth domain, and a browser partitioning
    // third-party storage can drop the result on the way back.
    await mod.signInWithRedirect(auth, provider);
  }
}

export async function signInWithGoogle(): Promise<void> {
  await signInWith((mod) => new mod.GoogleAuthProvider());
}

/** Through the OIDC provider created for this customer's Entra tenant;
 *  the hint saves the person retyping their address at Microsoft. */
export async function signInWithMicrosoft(
  providerId: string,
  email: string,
): Promise<void> {
  await signInWith((mod) => {
    const provider = new mod.OAuthProvider(providerId);
    provider.setCustomParameters({ login_hint: email });
    return provider;
  });
}
```

If `typeof client` does not type-check that way, type `make` as `(mod: typeof import("firebase/auth")) => import("firebase/auth").AuthProvider` — `client()` returns the module as `mod`; read its definition at the top of `auth.ts`.

- [ ] **Step 4: `SignIn.tsx`.** Import `signInWithMicrosoft` and `api, ApiError` (from `@/lib/api`); add state `const [msEmail, setMsEmail] = useState<string | null>(null);` and:

```tsx
  async function microsoft() {
    if (!msEmail) return;
    setBusy(true);
    setProblem(null);
    try {
      const { provider_id } = await api<{ provider_id: string }>(
        `/api/sso/microsoft?email=${encodeURIComponent(msEmail)}`,
      );
      await signInWithMicrosoft(provider_id, msEmail);
      onSignedIn();
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }
```

Replace the disabled Microsoft button block with:

```tsx
      {process.env.NEXT_PUBLIC_SSO_MICROSOFT === "1" ? (
        msEmail === null ? (
          <button
            type="button"
            className="btn-secondary"
            style={{ width: "100%", marginTop: 10 }}
            disabled={busy}
            onClick={() => setMsEmail("")}
          >
            Sign in with Microsoft
          </button>
        ) : (
          <form
            className="stack"
            style={{ gap: 8, marginTop: 10 }}
            onSubmit={(e) => {
              e.preventDefault();
              void microsoft();
            }}
          >
            <input
              className="input-field"
              type="email"
              aria-label="Microsoft work email"
              placeholder="you@company.com"
              value={msEmail}
              onChange={(e) => setMsEmail(e.target.value)}
              required
            />
            <button type="submit" className="btn-secondary" disabled={busy}>
              {busy ? "Opening Microsoft…" : "Continue with Microsoft"}
            </button>
          </form>
        )
      ) : null}
```

Check `ApiError`/`api` export names in `frontend/src/lib/api.ts` (`ApiError` has `friendly`); a 404's `friendly` is its detail sentence.

- [ ] **Step 5: Admin page.** Remove the `microsoft` `<option>`; make `newProvider` a constant `"google"` (delete its `useState` and the `onChange`, keep the select showing one option or replace it by text "Google Workspace"); in the table cell show:

```tsx
                  <td>
                    {d.provider === "microsoft"
                      ? "Microsoft 365 — set up by Ledgerly support"
                      : "Google Workspace"}
                  </td>
```

In `api.ts`, extend `SsoDomain` with `idp_provider_id?: string | null;`.

- [ ] **Step 6: Run** the Step 2 command; then `cd frontend && npm run lint && npx tsc --noEmit && npm run test:e2e` (move any `frontend/.env.local` aside first so the e2e build matches CI). Expected: contract tests PASS, lint/tsc clean, e2e 38/38 (an e2e that selected the Microsoft option would fail — update it to the new text if one exists).
- [ ] **Step 7: Commit** "Sign in with Microsoft through the customer's own provider".

---

### Task 6: Documents

**Files:** `SECURITY.md`; create `deployment/MICROSOFT_SIGN_IN.md`.

- [ ] **Step 1: `SECURITY.md`** — add before `## Data lifecycle`:

```markdown
## Sign-in

Identity Platform verifies every token's signature and audience; the users
row then decides role and active status on every request. A person with no
tenant yet joins one only through a domain binding:

- **Google:** a verified address at a domain the tenant's admin bound (an
  admin may bind only their own sign-in domain).
- **Microsoft:** through the Identity Platform OIDC provider created for the
  customer's own Entra tenant, *and* at the bound domain. The provider is
  the proof of tenant: another Entra tenant can give a user any address, but
  signs in through its own provider, which no other customer's binding
  accepts. Only the operator binds a Microsoft domain
  (`provision bind-microsoft`; deployment/MICROSOFT_SIGN_IN.md). The generic
  `microsoft.com` provider admits nobody.

The login page asks `GET /api/sso/microsoft?email=` which provider a work
address uses. It answers before sign-in, so it reveals whether a domain uses
Ledgerly through Microsoft; it is not rate limited (a known gap).
```

- [ ] **Step 2: `deployment/MICROSOFT_SIGN_IN.md`:**

```markdown
# Connecting a customer's Microsoft 365

One OIDC provider per customer Entra tenant. Policy: SECURITY.md, "Sign-in".

1. **From the customer:** their Entra tenant id (Entra → Overview) and the
   email domain their people sign in with.
2. **Entra app registration** (the operator's, multi-tenant or one per
   customer): redirect URI `https://<auth-domain>/__/auth/handler`, a client
   secret, and the customer's admin consent if their tenant requires it.
3. **Identity Platform → Providers → Add a provider → OpenID Connect:**
   name `entra-<customer>` (its id becomes `oidc.entra-<customer>`),
   issuer `https://login.microsoftonline.com/<tenant id>/v2.0`, the app's
   client id and secret, **code flow**.
4. **Bind:**

       python -m invoice_processing.admin.provision bind-microsoft \
           --tenant <slug> --domain <domain> --provider oidc.entra-<customer> \
           --entra-tenant <tenant id> --role clerk --operator <you>

5. **Verify:** `GET /api/sso/microsoft?email=someone@<domain>` returns the
   provider id; a person from that tenant signs in with "Sign in with
   Microsoft" and lands in the tenant as the default role.

To disconnect: the tenant's admin removes the binding on the Admin page, or
the operator deletes the provider (which stops every sign-in through it).
```

- [ ] **Step 3: Commit** "Write down how Microsoft sign-in is trusted and how a customer is connected".

---

### Task 7: Verification and the live test

- [ ] **Step 1:** Full suite, ruff, the CI Postgres list on a scratch `pg-x` container (add nothing new to the list: the touched DB test files are already on it — check `tests/test_provisioning.py`; if it is not on the list and runs DB code, leave it, as today), the 9 gates.
- [ ] **Step 2: Live test (user drives the console and the Microsoft account).** User creates the OIDC provider on `rasayan` per `deployment/MICROSOFT_SIGN_IN.md` and gives the provider id, Entra tenant id and domain. I: scratch DB `benchmark/runs/live-signin/app.db`, `create-tenant`, `bind-microsoft`; `frontend/.env.local` from the project config plus `NEXT_PUBLIC_SSO_MICROSOFT=1`; the `live-*` launch configs as on 2026-10-14 (production build). User signs in with Microsoft; I verify the users row, the `user_provisioned_by_sso` audit row, the account's claims (tenant and role only), the dashboard, and an empty console under the enforced CSP. If the user has an account in another Entra tenant: it is refused with `MICROSOFT_NOT_CONNECTED`.
- [ ] **Step 3:** Clean up the live setup as on 2026-10-14 (servers, scratch DB, `.env.local`, launch configs, the account's claims), asking about the `rasayan` provider.
- [ ] **Step 4:** Whole-branch review on `fable`; one fix pass; all minors fixed.
