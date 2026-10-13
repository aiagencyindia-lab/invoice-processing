# Data Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A tenant can be suspended (every path refuses it at once), reactivated, and after 30 days purged of every row, document and audit event, with a restore check and written policies as SOC 2 evidence.

**Architecture:** A new `invoice_processing/offboarding.py` holds the state changes (`suspend`, `reactivate`, `purge`) behind three new `admin/provision.py` subcommands. Enforcement sits at the shared choke points that already exist: `sso._refuse_inactive` / `enforce_active` / `resolve_principal` for every API request, `Worker._handle` for jobs, `PgApprovalRepository.tenant_ids` for the escalation sweep, `Provisioner._tenant_by_slug` for access grants. A new migration adds the one DELETE policy `audit_events` will ever have, usable only by the owner role. `invoice_processing/db/restore_check.py` verifies any restored database.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, Postgres 16 (pg8000) / SQLite, FastAPI TestClient, pytest.

**Spec:** `docs/superpowers/specs/2026-10-13-data-lifecycle-design.md`

## Global Constraints

- States: `active → suspended → purged`; `reactivate` is `suspended → active`; nothing leaves `purged`.
- `GRACE_DAYS = 30`, counted from the latest `suspended_at`.
- Suspension refusal text, exactly: `This organisation's account is suspended. Contact support to restore it.` (403).
- A tenant status of `None` (no tenants row at all) keeps today's behaviour; any other value but `active` is refused.
- Nothing writes an audit row into a `purged` tenant (it would restart the trail the purge erased).
- The purge never runs as the app role on Postgres: it needs `MIGRATE_DATABASE_URL` or `MIGRATE_CLOUD_SQL_USER`.
- Purge order: blobs (then re-list, must be empty) → one DB transaction (deletes, re-count must be zero, then `status=purged`) → print the record as one JSON line.
- After a purge the tenants row keeps id, slug, name, region; `settings` becomes only `{"offboarding": {...}}`.
- Audit purge policy: `CREATE POLICY audit_events_purge ON audit_events FOR DELETE USING (tenant_id = current_setting('app.purge_tenant', true));`
- Backup targets: RPO minutes (PITR), RTO 4 hours, drill every 6 months and after any production restore.
- Logging: `logger.info("event", extra={...})`; never pass `request_id`/`tenant_id`/`document_id` in `extra`. Never log an email.
- `ruff` line length 80. Every `# noqa` carries its reason. Comments say why.
- Run Python as `./.venv/bin/python`. Never `git commit -a` (stage explicit paths; `.impeccable/hook.cache.json` must never be committed).
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Rulings made while planning (spec deviations)

- **The purge policy is not added to `rls.enable_rls_statements`.** Migrations call that function, so adding it there would make the initial migration create the policy and the new migration then fail with "already exists". Instead `rls.py` gains `audit_purge_policy_statements()` / `drop_audit_purge_policy_statements()`, used only by the new migration. Cost if wrong: none found; fresh databases still get the policy through the chain.
- **The restore check is `invoice_processing/db/restore_check.py`** (run as `python -m invoice_processing.db.restore_check`), not `scripts/restore_check.py`: it reuses `TENANT_TABLES`/`rls`, and it ships in the image so it can run in a release container against a restored instance. Cost if wrong: a path in the runbook.
- **A missing tenant status means "no tenants row", not refusal.** On the joined `find` path the FK makes `None` impossible; on the no-users-row path `None` means the tenant does not exist and no write can reach it (FK). Refusing it would break dev and test principals that have no tenants row. Cost if wrong: a token for a never-created tenant still reads empty lists, as today.
- **Stale tokens for a purged tenant are refused even without a users row** (`directory.tenant_status`), because today a tenant-claim token with no users row is admitted with its claim role — after a purge that would let it write fresh rows into the purged tenant. Not in the spec; found while planning.
- **The escalation sweep skips non-active tenants** (`tenant_ids` filters on `status == "active"`): a suspended tenant's overdue approvals must not keep notifying. Not in the spec; one line.
- **Purged tenants' worker messages are dead-lettered without an audit row** (`_dead_letter(..., record=False)`), same rule as sign-in.

## Review Focus

1. A stale Identity Platform token whose tenant was purged (no users row left) → 403, and no audit row is written into the purged tenant. Pinned in Task 2.
2. A queued job for a purged tenant → dead-lettered, and no `document_dead_lettered` row is written. Pinned in Task 3.
3. A tenant's pipeline settings in `tenants.settings` survive suspend and reactivate untouched (only the `offboarding` key changes). Pinned in Task 1.
4. Suspend → reactivate → suspend again: the 30 days count from the second suspension, not the first. Pinned in Task 4.
5. Overdue approvals of a suspended tenant are not escalated by the worker sweep. Pinned in Task 3.

---

## File Structure

| File | Responsibility |
|---|---|
| Create `invoice_processing/offboarding.py` | State changes and the purge; no CLI, no HTTP |
| Modify `invoice_processing/admin/provision.py` | Three subcommands; `_tenant_by_slug` refuses non-active tenants |
| Modify `invoice_processing/db/repositories.py` | `find`/`domain` carry `tenant_status`; `PgUserDirectory.tenant_status`; `tenant_ids` active only |
| Modify `invoice_processing/sso.py` | Refuse closed tenants on every path |
| Modify `invoice_processing/worker/runner.py`, `worker/__main__.py` | Refuse closed tenants' jobs |
| Modify `invoice_processing/adapters.py` | `LocalBlobStore.list_keys` / `delete` |
| Modify `invoice_processing/db/migrate.py` | `use_owner_role()` shared with the purge |
| Modify `invoice_processing/db/rls.py` | Purge policy statements |
| Create `invoice_processing/db/migrations/versions/d1a2b3c4e5f6_audit_purge_policy.py` | The policy |
| Create `invoice_processing/db/restore_check.py` | Post-restore verification |
| Create `tests/test_offboarding.py`, `tests/test_restore_check.py`, `tests/pg_roles.py` | Tests and the Postgres bootstrap helper |
| Modify `tests/test_sso.py`, `tests/test_sso_repository.py`, `tests/test_worker.py`, `tests/test_provisioning.py`, `tests/test_gcs_and_migrations.py`, `tests/test_db_repositories.py` | Tests |
| Modify `SECURITY.md`; create `deployment/OFFBOARDING.md`, `deployment/BACKUP_RESTORE.md` | Policy and runbooks |
| Modify `.github/workflows/ci.yml` | Postgres list |

---

### Task 1: Suspend and reactivate

**Files:**
- Create: `invoice_processing/offboarding.py`
- Modify: `invoice_processing/admin/provision.py` (`_tenant_by_slug` ~line 231, `build_parser` ~451, `main` ~486)
- Test: `tests/test_offboarding.py`, `tests/test_provisioning.py`

**Interfaces:**
- Produces: `offboarding.OffboardingError(RuntimeError)`; `offboarding.GRACE_DAYS = 30`; constants `ACTIVE`, `SUSPENDED`, `PURGED`; `offboarding.suspend(slug, *, operator, reason, url=None, now=None) -> dict`; `offboarding.reactivate(slug, *, operator, url=None, now=None) -> dict`. Both return `{"tenant_id", "slug", "status", "offboarding": {...}}`. Helpers `_now()`, `_required(value, what)`, `_tenant_id(slug, url)` are reused by Task 4.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_offboarding.py`:

```python
"""Suspending, reactivating and purging a tenant.

The purge is the one place this product deletes evidence, so the tests weigh
most on what it refuses and on what it must leave behind: nothing of the
tenant, everything of everyone else.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from invoice_processing import offboarding
from invoice_processing.admin.provision import Provisioner
from invoice_processing.db.models import Base, Tenant
from invoice_processing.db.repositories import PgAuditSink
from invoice_processing.db.session import create_all, get_engine, tenant_session

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def url(scratch_db):
    target = scratch_db("offboarding.db")
    engine = get_engine(target)
    Base.metadata.drop_all(engine)
    create_all(target)
    return target


@pytest.fixture
def acme(url):
    return Provisioner(url=url).create_tenant("acme", "Acme Pty Ltd")["id"]


def _tenant(url, tenant_id):
    with tenant_session("", url) as session:
        tenant = session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        session.expunge(tenant)
        return tenant


def _actions(url, tenant_id):
    return [e.action for e in PgAuditSink(url).query(tenant_id)]


class TestSuspend:
    def test_suspends_records_and_audits(self, url, acme):
        result = offboarding.suspend(
            "acme", operator="vineet", reason="contract ended", url=url, now=NOW
        )
        assert result["status"] == "suspended"
        tenant = _tenant(url, acme)
        assert tenant.status == "suspended"
        assert tenant.settings["offboarding"] == {
            "suspended_at": NOW.isoformat(),
            "suspended_by": "vineet",
            "reason": "contract ended",
        }
        assert _actions(url, acme) == ["tenant_suspended"]

    def test_keeps_the_tenants_own_settings(self, url, acme):
        with tenant_session("", url) as session:
            tenant = session.get(Tenant, acme)
            tenant.settings = {"currency": "AUD", "tolerance": 5}
        offboarding.suspend(
            "acme", operator="vineet", reason="r", url=url, now=NOW
        )
        offboarding.reactivate("acme", operator="vineet", url=url, now=NOW)
        settings = _tenant(url, acme).settings
        assert settings["currency"] == "AUD"
        assert settings["tolerance"] == 5

    def test_refuses_a_tenant_that_is_not_active(self, url, acme):
        offboarding.suspend(
            "acme", operator="vineet", reason="r", url=url, now=NOW
        )
        with pytest.raises(offboarding.OffboardingError, match="suspended"):
            offboarding.suspend(
                "acme", operator="vineet", reason="r", url=url, now=NOW
            )

    def test_refuses_an_unknown_slug(self, url):
        with pytest.raises(offboarding.OffboardingError, match="No tenant"):
            offboarding.suspend("nobody", operator="v", reason="r", url=url)

    @pytest.mark.parametrize(("operator", "reason"), [("", "r"), ("v", " ")])
    def test_needs_an_operator_and_a_reason(self, url, acme, operator, reason):
        with pytest.raises(offboarding.OffboardingError, match="required"):
            offboarding.suspend(
                "acme", operator=operator, reason=reason, url=url
            )
        assert _tenant(url, acme).status == "active"


class TestReactivate:
    def test_reactivates_and_keeps_the_history(self, url, acme):
        offboarding.suspend(
            "acme", operator="vineet", reason="r", url=url, now=NOW
        )
        later = NOW + timedelta(days=2)
        offboarding.reactivate("acme", operator="ops", url=url, now=later)
        tenant = _tenant(url, acme)
        assert tenant.status == "active"
        record = tenant.settings["offboarding"]
        assert record["suspended_at"] == NOW.isoformat()
        assert record["reactivated_at"] == later.isoformat()
        assert record["reactivated_by"] == "ops"
        # sorted: two rows in one second have no reliable order
        assert sorted(_actions(url, acme)) == [
            "tenant_reactivated",
            "tenant_suspended",
        ]

    def test_refuses_an_active_tenant(self, url, acme):
        with pytest.raises(offboarding.OffboardingError, match="active"):
            offboarding.reactivate("acme", operator="ops", url=url)
```

Append to `tests/test_provisioning.py` (inside the module, after the existing add-user tests):

```python
class TestClosedTenants:
    @pytest.mark.parametrize("status", ["suspended", "purged"])
    def test_no_user_is_added_to_a_closed_tenant(
        self, provisioner, url, status
    ):
        from invoice_processing.db.models import Tenant  # noqa: PLC0415
        from invoice_processing.db.session import (  # noqa: PLC0415
            tenant_session,
        )

        tenant_id = provisioner.create_tenant("acme", "Acme")["id"]
        with tenant_session("", url) as session:
            session.get(Tenant, tenant_id).status = status
        with pytest.raises(ProvisioningError, match=status):
            provisioner.add_user("acme", "ap@acme.test", "clerk", "uid-1")
        with pytest.raises(ProvisioningError, match=status):
            provisioner.set_claims("acme", "ap@acme.test", "uid-1")
```

Append a CLI test to `tests/test_offboarding.py`:

```python
class TestCli:
    def test_suspend_and_reactivate_from_the_command_line(
        self, url, acme, monkeypatch, capsys
    ):
        from invoice_processing.admin import provision  # noqa: PLC0415

        monkeypatch.setenv("DATABASE_URL", url)
        assert (
            provision.main(
                [
                    "suspend-tenant",
                    "--tenant",
                    "acme",
                    "--operator",
                    "vineet",
                    "--reason",
                    "contract ended",
                ]
            )
            == 0
        )
        assert _tenant(url, acme).status == "suspended"
        assert (
            provision.main(
                ["reactivate-tenant", "--tenant", "acme", "--operator", "v"]
            )
            == 0
        )
        assert _tenant(url, acme).status == "active"
        assert (
            provision.main(
                ["reactivate-tenant", "--tenant", "acme", "--operator", "v"]
            )
            == 1
        )
        assert "active" in capsys.readouterr().err
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_offboarding.py tests/test_provisioning.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'invoice_processing.offboarding'` and the closed-tenant test fails with `DID NOT RAISE`.

- [ ] **Step 3: Write `invoice_processing/offboarding.py`**

```python
"""The end of a customer's data: suspend, wait, purge.

Three states and one direction: `active -> suspended -> purged`, with
`reactivate` as the only way back and only from `suspended`. Suspension is
what every request path checks (sign-in, the worker, provisioning); the
purge is the one place this product deletes evidence, so it is refused far
more often than it runs.

The tenants row is the surviving record. Its `settings` keep the tenant's
own configuration while it is suspended, and become only the
`offboarding` record once it is purged.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from .db.models import Tenant
from .db.repositories import audit_event_row
from .db.session import tenant_session
from .ports import RunContext

# A recovery window, not an export window: a suspended tenant cannot sign in,
# so customers export before their subscription ends (SECURITY.md).
GRACE_DAYS = 30
ACTIVE, SUSPENDED, PURGED = "active", "suspended", "purged"


class OffboardingError(RuntimeError):
    """Refused before anything was changed."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _required(value: str | None, what: str) -> str:
    if not (value or "").strip():
        raise OffboardingError(f"{what} is required.")
    return value.strip()


def _tenant_id(slug: str, url: str | None) -> str:
    with tenant_session("", url) as session:
        tenant_id = session.scalar(select(Tenant.id).where(Tenant.slug == slug))
    if tenant_id is None:
        raise OffboardingError(f"No tenant {slug!r}.")
    return tenant_id


def _move(
    slug: str,
    *,
    expected: str,
    new: str,
    event: str,
    operator: str,
    stamp: dict,
    url: str | None,
) -> dict:
    """One state change, its record and its audit row in one transaction."""
    tenant_id = _tenant_id(slug, url)
    # The tenant's own session, so the audit insert passes its RLS policy.
    with tenant_session(tenant_id, url) as session:
        tenant = session.scalar(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )
        if tenant.status != expected:
            raise OffboardingError(
                f"{slug} is {tenant.status}; it must be {expected} to "
                f"become {new}."
            )
        settings = dict(tenant.settings or {})
        record = {**settings.get("offboarding", {}), **stamp}
        tenant.status = new
        # A new dict, so the JSON column notices the change.
        tenant.settings = {**settings, "offboarding": record}
        session.add(
            audit_event_row(
                RunContext(tenant_id=tenant_id, actor=operator),
                event,
                {"slug": slug, **stamp},
            )
        )
    return {
        "tenant_id": tenant_id,
        "slug": slug,
        "status": new,
        "offboarding": record,
    }


def suspend(
    slug: str,
    *,
    operator: str,
    reason: str,
    url: str | None = None,
    now: datetime | None = None,
) -> dict:
    operator = _required(operator, "An operator")
    reason = _required(reason, "A reason")
    return _move(
        slug,
        expected=ACTIVE,
        new=SUSPENDED,
        event="tenant_suspended",
        operator=operator,
        stamp={
            "suspended_at": (now or _now()).isoformat(),
            "suspended_by": operator,
            "reason": reason,
        },
        url=url,
    )


def reactivate(
    slug: str,
    *,
    operator: str,
    url: str | None = None,
    now: datetime | None = None,
) -> dict:
    operator = _required(operator, "An operator")
    return _move(
        slug,
        expected=SUSPENDED,
        new=ACTIVE,
        event="tenant_reactivated",
        operator=operator,
        stamp={
            "reactivated_at": (now or _now()).isoformat(),
            "reactivated_by": operator,
        },
        url=url,
    )
```

(`tenant_reactivated` / `tenant_suspended` already map to entity type `tenant` through the `"tenant"` prefix in `repositories._ENTITY_TYPES`.)

- [ ] **Step 4: Refuse closed tenants in `Provisioner._tenant_by_slug`**

In `invoice_processing/admin/provision.py`, replace `_tenant_by_slug`:

```python
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
```

- [ ] **Step 5: Add the subcommands**

In `build_parser`, before `subparsers.add_parser("list")`:

```python
    suspend = subparsers.add_parser("suspend-tenant")
    suspend.add_argument("--tenant", required=True)
    suspend.add_argument("--operator", required=True)
    suspend.add_argument("--reason", required=True)

    reactivate = subparsers.add_parser("reactivate-tenant")
    reactivate.add_argument("--tenant", required=True)
    reactivate.add_argument("--operator", required=True)
```

In `main`, add the import at the top of the function body (`from .. import offboarding  # noqa: PLC0415 - keeps the provisioning import light`) and these branches before `elif args.command == "list":`:

```python
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
```

and widen the handler: `except (ProvisioningError, offboarding.OffboardingError) as exc:`.

Also add the new commands to the module docstring's usage block:

```
    python -m invoice_processing.admin.provision suspend-tenant \\
        --tenant acme --operator <you> --reason "contract ended"
    python -m invoice_processing.admin.provision reactivate-tenant \\
        --tenant acme --operator <you>
```

- [ ] **Step 6: Run the tests**

Run: `./.venv/bin/python -m pytest tests/test_offboarding.py tests/test_provisioning.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add invoice_processing/offboarding.py invoice_processing/admin/provision.py tests/test_offboarding.py tests/test_provisioning.py
git commit -m "Let an operator suspend and reactivate a tenant, and stop granting access to one that is closed

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Sign-in refuses closed tenants

**Files:**
- Modify: `invoice_processing/db/repositories.py` (`_user_dict` ~3057, `PgUserDirectory.find` ~3085, `PgUserDirectory.domain` ~3213; add `tenant_status`)
- Modify: `invoice_processing/sso.py` (`_refuse_inactive` ~74, `enforce_active` ~119, `resolve_principal` ~134)
- Test: `tests/test_sso.py`, `tests/test_sso_repository.py`, `tests/test_offboarding.py`

**Interfaces:**
- Produces: `PgUserDirectory.tenant_status(tenant_id: str) -> str | None`; rows from `find` carry `"tenant_status"`; `domain()` results carry `"tenant_status"`; `sso.SUSPENDED` (the refusal sentence); `sso.OPEN = (None, "active")`.
- Task 3 reuses `PgUserDirectory.tenant_status` and the `OPEN` rule.

- [ ] **Step 1: Teach the fake directory tenant status**

In `tests/test_sso.py` `FakeDirectory.__init__` add `self.tenant_statuses = {}`. Change `find` to return the status, and add `tenant_status`; in `domain` add the status:

```python
    def find(self, tenant_id, email):
        if self.fail:
            raise RuntimeError("db down")
        u = self.users.get((tenant_id, email.lower()))
        if not u:
            return None
        return dict(
            u,
            tenant_name="Acme Pty Ltd",
            tenant_status=self.tenant_statuses.get(tenant_id, "active"),
        )

    def tenant_status(self, tenant_id):
        if self.fail:
            raise RuntimeError("db down")
        return self.tenant_statuses.get(tenant_id, "active")

    def domain(self, domain):
        if self.fail:
            raise RuntimeError("db down")
        bound = self.domains.get(domain)
        if bound is None:
            return None
        return dict(
            bound,
            tenant_status=self.tenant_statuses.get(
                bound["tenant_id"], "active"
            ),
        )
```

- [ ] **Step 2: Write the failing sso tests**

Look at the existing helpers in `tests/test_sso.py` (`services(...)`, token-claim builders used by `test_a_deactivated_user_is_recorded_as_refused` and `test_a_bound_domain_provisions_the_default_role`) and reuse them. Append:

```python
class TestClosedTenants:
    """A suspended or purged tenant is refused on every path in."""

    def _user(self, active=True):
        return {
            "id": "u1",
            "tenant_id": ACME,
            "email": "ap@acme.test",
            "role": "clerk",
            "is_active": active,
            "uid": "uid-1",
            "last_login_at": NOW,
        }

    def _audit(self):
        class Audit:
            def __init__(self):
                self.events = []

            def emit(self, ctx, event, payload, before=None):
                self.events.append((ctx.tenant_id, event, payload))

        return Audit()

    def _claims(self):
        return {
            "sub": "uid-1",
            "user_id": "uid-1",
            "email": "ap@acme.test",
            "email_verified": True,
            "tenant_id": ACME,
            "role": "clerk",
        }

    def test_a_suspended_tenants_user_is_refused_and_recorded(self):
        directory = FakeDirectory(users=[self._user()])
        directory.tenant_statuses[ACME] = "suspended"
        audit = self._audit()
        services = sso.SsoServices(directory, FakeClaims(), audit)
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(self._claims(), services, now=NOW)
        assert exc.value.status_code == 403
        assert exc.value.detail == sso.SUSPENDED
        assert audit.events == [
            (
                ACME,
                "sign_in_refused",
                {
                    "email": "ap@acme.test",
                    "user_id": "u1",
                    "reason": "tenant_suspended",
                },
            )
        ]

    def test_suspension_outranks_deactivation(self):
        directory = FakeDirectory(users=[self._user(active=False)])
        directory.tenant_statuses[ACME] = "suspended"
        services = sso.SsoServices(directory, FakeClaims(), self._audit())
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(self._claims(), services, now=NOW)
        assert exc.value.detail == sso.SUSPENDED

    def test_a_dev_identity_in_a_suspended_tenant_is_refused(self):
        directory = FakeDirectory(users=[self._user()])
        directory.tenant_statuses[ACME] = "suspended"
        services = sso.SsoServices(directory, FakeClaims(), self._audit())
        principal = Principal(
            user_id="dev", email="ap@acme.test", tenant_id=ACME, role="admin"
        )
        with pytest.raises(AuthError) as exc:
            sso.enforce_active(principal, services)
        assert exc.value.status_code == 403

    def test_a_token_with_no_row_in_a_purged_tenant_is_refused_silently(self):
        """The stale token a purge leaves behind. Admitting it with its claim
        role would let it write fresh rows into the erased tenant; recording
        the refusal would restart the trail the purge removed."""
        directory = FakeDirectory()
        directory.tenant_statuses[ACME] = "purged"
        audit = self._audit()
        services = sso.SsoServices(directory, FakeClaims(), audit)
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(self._claims(), services, now=NOW)
        assert exc.value.detail == sso.SUSPENDED
        assert audit.events == []

    def test_a_dev_identity_with_no_row_in_a_suspended_tenant_is_refused(
        self,
    ):
        directory = FakeDirectory()
        directory.tenant_statuses[ACME] = "suspended"
        audit = self._audit()
        services = sso.SsoServices(directory, FakeClaims(), audit)
        principal = Principal(
            user_id="dev", email="ap@acme.test", tenant_id=ACME, role="admin"
        )
        with pytest.raises(AuthError):
            sso.enforce_active(principal, services)
        assert [e[2]["reason"] for e in audit.events] == ["tenant_suspended"]

    def test_a_tenant_with_no_tenants_row_keeps_todays_behaviour(self):
        directory = FakeDirectory()
        directory.tenant_statuses[ACME] = None
        services = sso.SsoServices(directory, FakeClaims(), self._audit())
        assert sso.resolve_principal(self._claims(), services).role == "clerk"

    def test_a_status_lookup_that_fails_is_a_503(self):
        directory = FakeDirectory()
        services = sso.SsoServices(directory, FakeClaims(), self._audit())
        directory.fail = True
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(self._claims(), services, now=NOW)
        assert exc.value.status_code == 503

    def test_a_bound_domain_of_a_suspended_tenant_provisions_no_one(self):
        directory = FakeDirectory(
            domains=[
                {
                    "id": "d1",
                    "tenant_id": ACME,
                    "tenant_name": "Acme Pty Ltd",
                    "domain": "acme.test",
                    "provider": "google",
                    "default_role": "clerk",
                }
            ]
        )
        directory.tenant_statuses[ACME] = "suspended"
        services = sso.SsoServices(directory, FakeClaims(), self._audit())
        claims = {
            "sub": "uid-9",
            "user_id": "uid-9",
            "email": "new@acme.test",
            "email_verified": True,
            "firebase": {"sign_in_provider": "google.com"},
        }
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(claims, services, now=NOW)
        assert exc.value.detail == sso.SUSPENDED
        assert directory.provisioned == []
```

- [ ] **Step 3: Write the failing repository test**

Append to `tests/test_sso_repository.py` (it is on the CI Postgres list; reuse its existing `url`/directory fixtures — read the top of the file and use the same fixture names):

```python
class TestTenantStatus:
    def test_find_domain_and_tenant_status_carry_it(self, url):
        from invoice_processing.db.models import (  # noqa: PLC0415
            SsoDomain,
            Tenant,
            User,
        )
        from invoice_processing.db.repositories import (  # noqa: PLC0415
            PgUserDirectory,
        )
        from invoice_processing.db.session import (  # noqa: PLC0415
            tenant_session,
        )

        tid = "33333333-3333-3333-3333-333333333333"
        with tenant_session(tid, url) as session:
            session.add(
                Tenant(id=tid, slug="closed", name="Closed", status="suspended")
            )
            session.flush()
            session.add(
                User(tenant_id=tid, email="ap@closed.test", role="clerk")
            )
            session.add(
                SsoDomain(
                    tenant_id=tid,
                    domain="closed.test",
                    provider="google",
                    default_role="clerk",
                    created_by="admin@closed.test",
                )
            )
        directory = PgUserDirectory(url)
        assert directory.find(tid, "ap@closed.test")["tenant_status"] == (
            "suspended"
        )
        assert directory.domain("closed.test")["tenant_status"] == "suspended"
        assert directory.tenant_status(tid) == "suspended"
        assert directory.tenant_status("no-such-tenant") is None
```

- [ ] **Step 4: Write the failing API test**

Append to `tests/test_offboarding.py`:

```python
class TestTheApiRefusesASuspendedTenant:
    @pytest.fixture
    def api(self, url, tmp_path, monkeypatch):
        from invoice_processing.adapters import (  # noqa: PLC0415
            JsonlAuditSink,
        )
        from invoice_processing.api import auth, service  # noqa: PLC0415
        from tests.test_signup_api import FakeVerifier  # noqa: PLC0415

        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        monkeypatch.setattr(auth, "_verifier", FakeVerifier())
        monkeypatch.setattr(
            service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
        )

    def _get(self, tenant_id):
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from invoice_processing.api.service import app  # noqa: PLC0415

        token = f"ap@acme.test|uid-1|{tenant_id}|"
        return TestClient(app).get(
            "/api/invoices", headers={"Authorization": f"Bearer {token}"}
        )

    @pytest.mark.parametrize("with_row", [True, False])
    def test_every_request_is_refused_once_suspended(
        self, url, acme, api, with_row
    ):
        if with_row:
            Provisioner(url=url).add_user(
                "acme", "ap@acme.test", "admin", "uid-1"
            )
        assert self._get(acme).status_code == 200
        offboarding.suspend("acme", operator="v", reason="r", url=url)
        refused = self._get(acme)
        assert refused.status_code == 403
        assert "suspended" in refused.json()["detail"]
        offboarding.reactivate("acme", operator="v", url=url)
        assert self._get(acme).status_code == 200
```

- [ ] **Step 5: Run them to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_sso.py tests/test_sso_repository.py tests/test_offboarding.py -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: module 'invoice_processing.sso' has no attribute 'SUSPENDED'`, `KeyError: 'tenant_status'`, `AttributeError: 'PgUserDirectory' object has no attribute 'tenant_status'`, and the API test gets 200 after suspension. (If `/api/invoices` returns something other than 200 before suspension, read the response and pick the simplest authenticated GET route that returns 200 for an empty tenant, e.g. one probed in `tests/test_tenant_isolation_api.py`.)

- [ ] **Step 6: Implement the repository side**

In `invoice_processing/db/repositories.py`:

```python
def _user_dict(
    u: User, tenant_name: str | None, tenant_status: str | None = None
) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "role": u.role,
        "is_active": bool(u.is_active),
        "uid": u.uid,
        "display_name": u.display_name,
        "last_login_at": u.last_login_at,
        "tenant_name": tenant_name,
        "tenant_status": tenant_status,
    }
```

In `PgUserDirectory.find`, select the status in the same statement (it runs on every request; one statement is the point):

```python
            found = session.execute(
                select(User, Tenant.name, Tenant.status)
                .outerjoin(Tenant, Tenant.id == User.tenant_id)
                ...
            ).first()
            if found is None:
                return None
            return _user_dict(found[0], found[1], found[2])
```

Update the class docstring sentence to "the row and its tenant's name and status together".

Add to `PgUserDirectory`, after `find`:

```python
    def tenant_status(self, tenant_id: str) -> str | None:
        """The tenant's status, for a caller with no users row to join it
        through. None when there is no tenants row at all."""
        with tenant_session(tenant_id, self.url) as session:
            return session.scalar(
                select(Tenant.status).where(Tenant.id == tenant_id)
            )
```

In `domain`, add to the returned dict:

```python
                "tenant_status": session.scalar(
                    select(Tenant.status).where(Tenant.id == row.tenant_id)
                ),
```

- [ ] **Step 7: Implement the sso side**

In `invoice_processing/sso.py`, after `_unavailable`, replace `_refuse_inactive` with:

```python
SUSPENDED = (
    "This organisation's account is suspended. Contact support to restore it."
)
# None: no tenants row at all, which no write can reach (the foreign key).
OPEN = (None, "active")


def _record_refusal(services: SsoServices, ctx, payload: dict) -> None:
    # ponytail: no database means no sink, and the refusal goes unrecorded;
    # the same rule as the rest of sign-in.
    if services.audit is None:
        return
    try:
        services.audit.emit(ctx, "sign_in_refused", payload)
    except Exception as exc:  # a refusal nobody can see is the real failure
        raise _unavailable(exc) from exc


def _refuse_closed_tenant(
    status: str | None,
    tenant_id: str,
    email: str,
    uid: str,
    role: str,
    services: SsoServices,
) -> None:
    """Refuse a suspended or purged tenant when there is no users row."""
    if status in OPEN:
        return
    # A purged tenant's trail is gone; writing to it would start a new one.
    if status != "purged":
        ctx = Principal(
            user_id=uid, email=email, tenant_id=tenant_id, role=role
        ).context()
        _record_refusal(
            services, ctx, {"email": email, "reason": "tenant_suspended"}
        )
    raise AuthError(SUSPENDED, status_code=403)


def _tenant_status(services: SsoServices, tenant_id: str) -> str | None:
    try:
        return services.directory.tenant_status(tenant_id)
    except Exception as exc:
        raise _unavailable(exc) from exc


def _refuse_inactive(
    row: dict, tenant_id: str, email: str, services: SsoServices
) -> None:
    status = row.get("tenant_status")
    if status not in OPEN:
        reason = "tenant_suspended"
    elif row.get("is_active") is False:
        reason = "deactivated"
    else:
        return
    if status != "purged":
        ctx = Principal(
            user_id=str(row.get("uid") or ""),
            email=email,
            tenant_id=tenant_id,
            role=str(row.get("role") or "viewer"),
        ).context()
        _record_refusal(
            services,
            ctx,
            {"email": email, "user_id": row["id"], "reason": reason},
        )
    if reason == "tenant_suspended":
        raise AuthError(SUSPENDED, status_code=403)
    raise AuthError(
        "This account has been deactivated. Ask an admin of "
        f"{row.get('tenant_name') or 'your organisation'} to reactivate it.",
        status_code=403,
    )
```

In `enforce_active`, after the `find`:

```python
    if row is not None:
        _refuse_inactive(row, principal.tenant_id, principal.email, services)
    else:
        _refuse_closed_tenant(
            _tenant_status(services, principal.tenant_id),
            principal.tenant_id,
            principal.email,
            principal.user_id,
            principal.role,
            services,
        )
    return principal
```

In `resolve_principal`, tenant-claim path, at the start of the `if row is None:` block (before the warning):

```python
        if row is None:
            _refuse_closed_tenant(
                _tenant_status(services, base.tenant_id),
                base.tenant_id,
                base.email,
                base.user_id,
                base.role,
                services,
            )
            # Deactivation cannot reach a person with no row; say so.
```

In the domain path, right after `role = str(bound["default_role"])` and before `created = False`:

```python
    # Before any provisioning: a suspended tenant gains no one.
    _refuse_closed_tenant(
        bound.get("tenant_status"), tenant_id, email, uid, role, services
    )
```

- [ ] **Step 8: Run the tests, then the whole sso/signup/isolation group**

Run: `./.venv/bin/python -m pytest tests/test_sso.py tests/test_sso_repository.py tests/test_offboarding.py tests/test_signup_api.py tests/test_tenant_isolation_api.py -q -p no:cacheprovider`
Expected: PASS. If an existing test's fake directory lacks `tenant_status`, add the method to that fake (returning `"active"`), not a `getattr` fallback in `sso.py` — a fallback would fail open.

- [ ] **Step 9: Commit**

```bash
git add invoice_processing/db/repositories.py invoice_processing/sso.py tests/test_sso.py tests/test_sso_repository.py tests/test_offboarding.py
git commit -m "Refuse every sign-in to a suspended or purged tenant, stale tokens included

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The worker refuses closed tenants

**Files:**
- Modify: `invoice_processing/worker/runner.py` (`Worker.__init__` ~841, `_handle` ~905, `_handle_rematch` ~969, `_dead_letter` ~1162)
- Modify: `invoice_processing/worker/__main__.py` (the `Worker(...)` construction)
- Modify: `invoice_processing/db/repositories.py` (`PgApprovalRepository.tenant_ids` ~1760)
- Test: `tests/test_worker.py` (on the CI Postgres list)

**Interfaces:**
- Consumes: `PgUserDirectory.tenant_status(tenant_id) -> str | None` (Task 2); `sso.OPEN`.
- Produces: `Worker(..., tenant_status: Callable[[str], str | None] | None = None)`; `Worker._dead_letter(message, reason, event=..., record=True)`; outcome strings `"dead-lettered:tenant-closed"` and `"retry"` (status lookup failure).

- [ ] **Step 1: Write the failing tests**

Read `tests/test_worker.py` around `class Handler` (~213), `class FakeAudit` (~228) and the `audit` fixture (~236) and reuse them. Append:

```python
class TestClosedTenants:
    def _worker(self, queue, claims, audit, statuses, handler=None):
        def lookup(tenant_id):
            status = statuses.get(tenant_id, "active")
            if isinstance(status, Exception):
                raise status
            return status

        return Worker(
            queue=queue,
            claims=claims,
            handler=handler or Handler(),
            audit=audit,
            tenant_status=lookup,
        )

    def test_a_suspended_tenants_document_is_dead_lettered_not_run(
        self, queue, claims, audit
    ):
        handler = Handler()
        worker = self._worker(
            queue, claims, audit, {"tenant-acme": "suspended"}, handler
        )
        queue.publish(work_message())
        assert worker.handle(queue.pull()[0]) == "dead-lettered:tenant-closed"
        assert handler.calls == []
        assert queue.dead_letters()[0]["reason"] == "tenant suspended"
        assert [e[1] for e in audit.events] == ["document_dead_lettered"]

    def test_a_purged_tenants_document_writes_no_audit_row(
        self, queue, claims, audit
    ):
        worker = self._worker(queue, claims, audit, {"tenant-acme": "purged"})
        queue.publish(work_message())
        assert worker.handle(queue.pull()[0]) == "dead-lettered:tenant-closed"
        assert audit.events == []

    def test_a_suspended_tenants_rematch_is_dead_lettered(
        self, queue, claims, audit
    ):
        worker = self._worker(
            queue, claims, audit, {"tenant-acme": "suspended"}
        )
        queue.publish(
            {"type": runner.REMATCH, "tenant_id": "tenant-acme", "case_id": "c1"}
        )
        assert worker.handle(queue.pull()[0]) == "dead-lettered:tenant-closed"

    def test_a_failed_status_lookup_retries(self, queue, claims, audit):
        worker = self._worker(
            queue, claims, audit, {"tenant-acme": RuntimeError("db down")}
        )
        queue.publish(work_message())
        assert worker.handle(queue.pull()[0]) == "retry"
        assert queue.depth() == 1

    def test_no_lookup_means_no_check(self, queue, claims, audit):
        """A worker without a database has no tenants to close."""
        worker = Worker(queue=queue, claims=claims, handler=Handler())
        queue.publish(work_message())
        assert worker.handle(queue.pull()[0]) == "done"


class TestEscalationSweepSkipsClosedTenants:
    def test_only_active_tenants_are_swept(self, scratch_db):
        url = scratch_db("sweep.db")
        engine = get_engine(url)
        models.Base.metadata.drop_all(engine)
        create_all(url)
        with tenant_session("", url) as session:
            session.add(models.Tenant(id="t-open", slug="open", name="Open"))
            session.add(
                models.Tenant(
                    id="t-shut", slug="shut", name="Shut", status="suspended"
                )
            )
        assert PgApprovalRepository(url).tenant_ids() == ["t-open"]
```

Adjust to the real names in the file if they differ: the handler's recorded-calls attribute, the audit fake's list (`events` here), `queue.depth()` and `queue.dead_letters()` (both exist on `LocalQueue`). Check them by reading the file; do not invent new fakes.

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_worker.py -q -p no:cacheprovider -k "Closed"`
Expected: FAIL — `TypeError: Worker.__init__() got an unexpected keyword argument 'tenant_status'`, and the sweep returns both ids.

- [ ] **Step 3: Implement**

`Worker.__init__` gains a keyword parameter (after `clock`):

```python
        tenant_status: Callable[[str], str | None] | None = None,
```

and in the body:

```python
        # None without a database: no tenants rows, nothing to close.
        self.tenant_status = tenant_status
```

Add a method after `handle`:

```python
    def _refuse_closed_tenant(self, message: Message) -> str | None:
        """Dead-letter a suspended or purged tenant's job; None to go on.

        Dead-lettered rather than dropped, so a tenant reactivated by mistake
        loses nothing. A purged tenant gets no audit row: its trail is gone.
        """
        tenant_id = str(message.payload.get("tenant_id") or "")
        if self.tenant_status is None or not tenant_id:
            return None
        from ..sso import OPEN  # noqa: PLC0415 - sso imports the API stack

        try:
            status = self.tenant_status(tenant_id)
        except Exception:
            logger.exception("Tenant status lookup failed; retrying the job.")
            self.queue.nack(message)
            return "retry"
        if status in OPEN:
            return None
        self._dead_letter(
            message,
            "tenant suspended",
            event=(
                "rematch_dead_lettered"
                if message.payload.get("type") == REMATCH
                else "document_dead_lettered"
            ),
            record=status != "purged",
        )
        return "dead-lettered:tenant-closed"
```

At the top of `_handle`, before the `REMATCH` branch:

```python
        refused = self._refuse_closed_tenant(message)
        if refused is not None:
            return refused
```

`_dead_letter` gains `record: bool = True` and wraps the `self._emit(...)` call in `if record:`.

In `PgApprovalRepository.tenant_ids`:

```python
    def tenant_ids(self) -> list[str]:
        """Every active tenant id. The one deliberately cross-tenant read
        here, like `PgDocumentRepository.tenant_id_for_slug`: the SLA sweep
        has to visit each tenant to find its overdue steps, and `tenants` has
        no RLS. A suspended tenant's approvals stop escalating."""
        with tenant_session("", self.url) as session:
            return list(
                session.scalars(
                    select(Tenant.id).where(Tenant.status == "active")
                )
            )
```

In `worker/__main__.py`, inside the `Worker(...)` call:

```python
        # Only with a database: there are no tenants rows to close without one.
        tenant_status=(
            PgUserDirectory().tenant_status
            if service._database_configured()
            else None
        ),
```

with `from ..db.repositories import PgUserDirectory` added to the imports.

- [ ] **Step 4: Run the worker tests**

Run: `./.venv/bin/python -m pytest tests/test_worker.py tests/test_worker_logging.py tests/test_rematch.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add invoice_processing/worker/runner.py invoice_processing/worker/__main__.py invoice_processing/db/repositories.py tests/test_worker.py
git commit -m "Dead-letter a closed tenant's jobs and stop escalating its approvals

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The purge

**Files:**
- Modify: `invoice_processing/adapters.py` (`LocalBlobStore`, ~line 34)
- Modify: `invoice_processing/db/migrate.py` (`main` ~136)
- Modify: `invoice_processing/offboarding.py`
- Modify: `invoice_processing/admin/provision.py`
- Test: `tests/test_offboarding.py`, `tests/test_db_repositories.py`

**Interfaces:**
- Consumes: Task 1 helpers `_now`, `_required`, `_tenant_id`, constants.
- Produces: `LocalBlobStore.list_keys() -> list[str]`, `LocalBlobStore.delete(key) -> None`; `migrate.use_owner_role() -> bool`; `offboarding.require_owner_role() -> None`; `offboarding.leftovers(tenant_id, blobs, url=None) -> dict[str, int]`; `offboarding.purge(slug, *, operator, confirm, blobs_for, skip_grace=False, reason=None, url=None, now=None) -> dict` (the record, including `tenant_id`, `slug`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db_repositories.py` `class TestSchema`:

```python
    def test_every_table_with_a_tenant_id_is_a_tenant_table(self, db):
        """The reverse of the test above. The purge and RLS both work from
        TENANT_TABLES, so a table missing from it keeps a purged tenant's
        rows and has no isolation."""
        for name, table in Base.metadata.tables.items():
            if "tenant_id" in table.columns:
                assert name in TENANT_TABLES, name
```

Append to `tests/test_offboarding.py`:

```python
import json
import uuid
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, func, insert

from invoice_processing.adapters import LocalBlobStore
from invoice_processing.db.models import TENANT_TABLES


def seed_every_tenant_table(url, tenant_id):
    """One row in every tenant table, built from the schema itself.

    So a table added later is seeded, and so checked by the purge test,
    without anyone remembering to.
    """
    ids = {}
    with tenant_session(tenant_id, url) as session:
        for table in Base.metadata.sorted_tables:
            if table.name not in TENANT_TABLES:
                continue
            row = {}
            for col in table.columns:
                fk = next(iter(col.foreign_keys), None)
                if col.name == "tenant_id":
                    row[col.name] = tenant_id
                elif fk is not None:
                    parent = fk.column.table.name
                    if parent in ids:
                        row[col.name] = ids[parent]
                    elif not col.nullable:
                        raise AssertionError(f"{table.name}.{col.name}")
                elif col.primary_key and not isinstance(col.type, Integer):
                    row[col.name] = str(uuid.uuid4())
                elif col.nullable or col.default or col.server_default:
                    continue
                elif isinstance(col.type, Boolean):
                    row[col.name] = False
                elif isinstance(col.type, Integer):
                    row[col.name] = 1
                elif isinstance(col.type, Numeric):
                    row[col.name] = Decimal("1")
                elif isinstance(col.type, DateTime):
                    row[col.name] = NOW
                elif isinstance(col.type, Date):
                    row[col.name] = NOW.date()
                elif col.type.__class__.__name__ in ("JSON", "JSONB"):
                    row[col.name] = {}
                else:
                    length = getattr(col.type, "length", None) or 64
                    row[col.name] = f"{col.name}-{uuid.uuid4().hex}"[:length]
            result = session.execute(insert(table).values(row))
            ids[table.name] = row.get("id", result.inserted_primary_key[0])


def _count(url, tenant_id, table):
    model = Base.metadata.tables[table]
    with tenant_session(tenant_id, url) as session:
        return session.scalar(
            select(func.count()).select_from(model).where(
                model.c.tenant_id == tenant_id
            )
        )


@pytest.fixture
def stores(tmp_path):
    made = {}

    def blobs_for(tenant_id):
        return made.setdefault(
            tenant_id, LocalBlobStore(tmp_path / "blobs" / tenant_id)
        )

    return blobs_for


@pytest.fixture
def two_tenants(url, stores):
    """Acme (to be purged) and Globex (to be left alone), each with a row in
    every tenant table and two documents."""
    p = Provisioner(url=url)
    acme = p.create_tenant("acme", "Acme Pty Ltd")["id"]
    globex = p.create_tenant("globex", "Globex")["id"]
    for tenant_id in (acme, globex):
        seed_every_tenant_table(url, tenant_id)
        stores(tenant_id).put("documents/a.pdf", b"%PDF a")
        stores(tenant_id).put("cases/c1/page-1.png", b"png")
    offboarding.suspend(
        "acme", operator="vineet", reason="contract ended", url=url, now=NOW
    )
    return acme, globex


def _purge(url, stores, *, now=NOW + timedelta(days=31), **kwargs):
    kwargs.setdefault("operator", "vineet")
    kwargs.setdefault("confirm", "acme")
    return offboarding.purge(
        "acme", blobs_for=stores, url=url, now=now, **kwargs
    )


class TestLocalBlobStore:
    def test_lists_and_deletes_inside_its_root(self, tmp_path):
        store = LocalBlobStore(tmp_path / "t1")
        assert store.list_keys() == []
        store.put("documents/a.pdf", b"a")
        store.put("cases/c1/p.png", b"p")
        assert store.list_keys() == ["cases/c1/p.png", "documents/a.pdf"]
        store.delete("documents/a.pdf")
        assert store.list_keys() == ["cases/c1/p.png"]

    def test_delete_refuses_a_key_that_escapes(self, tmp_path):
        (tmp_path / "other.pdf").write_bytes(b"x")
        with pytest.raises(ValueError):
            LocalBlobStore(tmp_path / "t1").delete("../other.pdf")
        assert (tmp_path / "other.pdf").exists()


class TestPurge:
    def test_removes_every_row_and_document_of_the_tenant(
        self, url, stores, two_tenants
    ):
        acme, _ = two_tenants
        record = _purge(url, stores)
        for table in TENANT_TABLES:
            assert _count(url, acme, table) == 0, table
        assert stores(acme).list_keys() == []
        assert record["blobs_deleted"] == 2
        assert set(record["rows_deleted"]) == set(TENANT_TABLES)
        # suspend wrote one audit row on top of the seeded one
        assert record["rows_deleted"]["audit_events"] == 2
        assert all(n >= 1 for n in record["rows_deleted"].values())

    def test_leaves_every_other_tenant_alone(self, url, stores, two_tenants):
        _, globex = two_tenants
        _purge(url, stores)
        for table in TENANT_TABLES:
            assert _count(url, globex, table) >= 1, table
        assert len(stores(globex).list_keys()) == 2

    def test_keeps_the_tenants_row_as_the_record(
        self, url, stores, two_tenants
    ):
        acme, _ = two_tenants
        later = NOW + timedelta(days=31)
        record = _purge(url, stores, now=later)
        tenant = _tenant(url, acme)
        assert (tenant.slug, tenant.name, tenant.status) == (
            "acme",
            "Acme Pty Ltd",
            "purged",
        )
        assert list(tenant.settings) == ["offboarding"]
        kept = tenant.settings["offboarding"]
        assert kept["suspended_at"] == NOW.isoformat()
        assert kept["purged_at"] == later.isoformat()
        assert kept["purged_by"] == "vineet"
        assert kept["skip_grace"] is False
        assert kept == {
            k: v for k, v in record.items() if k not in ("tenant_id", "slug")
        }

    def test_refuses_inside_the_recovery_window(self, url, stores, two_tenants):
        acme, _ = two_tenants
        with pytest.raises(offboarding.OffboardingError, match="30 days"):
            _purge(url, stores, now=NOW + timedelta(days=29))
        assert _tenant(url, acme).status == "suspended"
        assert len(stores(acme).list_keys()) == 2

    def test_the_window_counts_from_the_latest_suspension(
        self, url, stores, two_tenants
    ):
        offboarding.reactivate("acme", operator="v", url=url, now=NOW)
        again = NOW + timedelta(days=20)
        offboarding.suspend(
            "acme", operator="v", reason="r", url=url, now=again
        )
        with pytest.raises(offboarding.OffboardingError, match="30 days"):
            _purge(url, stores, now=NOW + timedelta(days=31))
        _purge(url, stores, now=again + timedelta(days=30))

    def test_skip_grace_needs_a_reason_and_is_recorded(
        self, url, stores, two_tenants
    ):
        with pytest.raises(offboarding.OffboardingError, match="reason"):
            _purge(url, stores, now=NOW, skip_grace=True)
        record = _purge(
            url,
            stores,
            now=NOW,
            skip_grace=True,
            reason="customer asked in writing, ticket 12",
        )
        assert record["skip_grace"] is True
        assert record["skip_reason"] == "customer asked in writing, ticket 12"

    def test_refuses_a_wrong_confirmation(self, url, stores, two_tenants):
        with pytest.raises(offboarding.OffboardingError, match="confirm"):
            _purge(url, stores, confirm="acme2")

    def test_refuses_an_active_tenant(self, url, stores, two_tenants):
        offboarding.reactivate("acme", operator="v", url=url)
        with pytest.raises(offboarding.OffboardingError, match="active"):
            _purge(url, stores)

    def test_refuses_a_purged_tenant(self, url, stores, two_tenants):
        _purge(url, stores)
        with pytest.raises(offboarding.OffboardingError, match="purged"):
            _purge(url, stores)

    def test_a_failure_after_the_documents_reruns_to_completion(
        self, url, stores, two_tenants, monkeypatch
    ):
        acme, _ = two_tenants
        real = offboarding._delete_rows

        def crash(*args, **kwargs):
            raise RuntimeError("connection lost")

        monkeypatch.setattr(offboarding, "_delete_rows", crash)
        with pytest.raises(RuntimeError):
            _purge(url, stores)
        assert _tenant(url, acme).status == "suspended"
        assert stores(acme).list_keys() == []
        monkeypatch.setattr(offboarding, "_delete_rows", real)
        record = _purge(url, stores)
        assert record["blobs_deleted"] == 0
        assert _count(url, acme, "invoices") == 0

    def test_rows_that_survive_the_deletes_abort_the_purge(
        self, url, stores, two_tenants, monkeypatch
    ):
        """What a missing DELETE policy looks like on Postgres: the delete
        removes nothing and says nothing. The re-count catches it."""
        acme, _ = two_tenants
        monkeypatch.setattr(
            offboarding, "_delete_rows", lambda session, tenant_id: {}
        )
        with pytest.raises(offboarding.OffboardingError, match="still holds"):
            _purge(url, stores)
        assert _tenant(url, acme).status == "suspended"


class TestOwnerRole:
    def test_refuses_postgres_without_the_owner_url(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+pg8000://app@h/db")
        monkeypatch.delenv("MIGRATE_DATABASE_URL", raising=False)
        monkeypatch.delenv("MIGRATE_CLOUD_SQL_USER", raising=False)
        with pytest.raises(offboarding.OffboardingError, match="owner"):
            offboarding.require_owner_role()

    def test_switches_to_the_owner_url(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+pg8000://app@h/db")
        monkeypatch.setenv(
            "MIGRATE_DATABASE_URL", "postgresql+pg8000://owner@h/db"
        )
        offboarding.require_owner_role()
        import os  # noqa: PLC0415

        assert os.environ["DATABASE_URL"].startswith(
            "postgresql+pg8000://owner"
        )

    def test_sqlite_needs_no_owner(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'x.db'}")
        monkeypatch.delenv("MIGRATE_DATABASE_URL", raising=False)
        monkeypatch.delenv("MIGRATE_CLOUD_SQL_USER", raising=False)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        offboarding.require_owner_role()


class TestPurgeCli:
    def test_prints_one_json_record(
        self, url, two_tenants, monkeypatch, capsys, tmp_path
    ):
        from invoice_processing import paths  # noqa: PLC0415
        from invoice_processing.admin import provision  # noqa: PLC0415

        acme, _ = two_tenants
        monkeypatch.setenv("DATABASE_URL", url)
        # The owner URL is the same database here; on CI's Postgres the
        # command would otherwise refuse to run as the app role.
        monkeypatch.setenv("MIGRATE_DATABASE_URL", url)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        monkeypatch.delenv("GCS_BUCKET", raising=False)
        monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
        code = provision.main(
            [
                "purge-tenant",
                "--tenant",
                "acme",
                "--operator",
                "vineet",
                "--confirm",
                "acme",
                "--skip-grace",
                "--reason",
                "test",
            ]
        )
        assert code == 0
        line = capsys.readouterr().out.strip().splitlines()[-1]
        record = json.loads(line)
        assert record["tenant_id"] == acme
        assert record["slug"] == "acme"
```

(`TestPurgeCli` uses `paths.DATA_DIR / "blobs" / <tenant>` because the CLI resolves stores through `service.get_blobs`; the fixture's documents live elsewhere, so `blobs_deleted` is 0 there and that is fine — the store wiring is what this test pins.)

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_offboarding.py tests/test_db_repositories.py -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: 'LocalBlobStore' object has no attribute 'list_keys'`, `module 'invoice_processing.offboarding' has no attribute 'purge'`. The schema test may already pass (it documents a property that holds today); that is acceptable for a guard test — note it in the ledger.

- [ ] **Step 3: `LocalBlobStore.list_keys` / `delete`**

In `invoice_processing/adapters.py`, add to `LocalBlobStore` after `signed_url`:

```python
    def list_keys(self) -> list[str]:
        """Every key under this store's root, as `put` was given it."""
        if not self.root.exists():
            return []
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if p.is_file()
        )

    def delete(self, key: str) -> None:
        """Only for tenant offboarding, like `GcsBlobStore.delete`.

        ponytail: empty directories stay behind; they hold no data, and
        removing them races a concurrent `put` for nothing.
        """
        self._path(key).unlink()
```

- [ ] **Step 4: `migrate.use_owner_role`**

In `invoice_processing/db/migrate.py`, add above `main` and use it there:

```python
def use_owner_role() -> bool:
    """Point this process at the owner's connection; True when one is set.

    The release step and the tenant purge are the two things that run as
    the owner. MIGRATE_* name its connection, so the app's own settings stay
    the least-privilege ones.
    """
    found = False
    for source, target in (
        ("MIGRATE_DATABASE_URL", "DATABASE_URL"),
        ("MIGRATE_CLOUD_SQL_USER", "CLOUD_SQL_USER"),
    ):
        if os.getenv(source):
            os.environ[target] = os.environ[source]
            found = True
    return found
```

and in `main` replace the `for source, target in (...)` loop with `use_owner_role()`.

- [ ] **Step 5: The purge**

Add to `invoice_processing/offboarding.py` (extend the imports to `from datetime import datetime, timedelta, timezone`, `import os`, `from sqlalchemy import delete, func, select, text, update`, `from .db.models import TENANT_TABLES, Base, Tenant`, `from .db.rls import PURGE_SETTING`, `from .db.session import is_postgres, tenant_session`; and define `PURGE_SETTING = "app.purge_tenant"` in `rls.py` next to `TENANT_SETTING`, which Task 5's policy reads):

```python
def require_owner_role() -> None:
    """Run the purge as the owner, or not at all on Postgres.

    The app role cannot delete an audit row (PR #18's REVOKE), which is the
    point; the purge is the one deletion that needs it, so it borrows the
    release step's connection rather than widening the app's.
    """
    from .db.migrate import use_owner_role  # noqa: PLC0415 - alembic is heavy
    from .db.session import cloud_sql_instance, database_url  # noqa: PLC0415

    if use_owner_role():
        return
    if cloud_sql_instance() or database_url().startswith("postgresql"):
        raise OffboardingError(
            "The purge runs as the owner role: set MIGRATE_DATABASE_URL (or "
            "MIGRATE_CLOUD_SQL_USER), the release step's connection."
        )


def _tenant_tables():
    """Children before parents, so every foreign key is gone before its row."""
    return [
        t for t in reversed(Base.metadata.sorted_tables)
        if t.name in TENANT_TABLES
    ]


def _delete_rows(session, tenant_id: str) -> dict[str, int]:
    return {
        table.name: session.execute(
            delete(table).where(table.c.tenant_id == tenant_id)
        ).rowcount
        for table in _tenant_tables()
    }


def _remaining_rows(session, tenant_id: str) -> dict[str, int]:
    counts = {
        table.name: session.scalar(
            select(func.count())
            .select_from(table)
            .where(table.c.tenant_id == tenant_id)
        )
        for table in _tenant_tables()
    }
    return {name: n for name, n in counts.items() if n}


def leftovers(tenant_id: str, blobs, url: str | None = None) -> dict[str, int]:
    """What of a tenant is still here: rows per table, and documents."""
    with tenant_session(tenant_id, url) as session:
        left = _remaining_rows(session, tenant_id)
    documents = len(blobs.list_keys())
    return {**left, **({"documents": documents} if documents else {})}


def purge(
    slug: str,
    *,
    operator: str,
    confirm: str,
    blobs_for,
    skip_grace: bool = False,
    reason: str | None = None,
    url: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Delete every row and document of a suspended tenant.

    Documents first, then the rows in one transaction that also marks the
    tenant purged. A crash at any point leaves it `suspended`, so the record
    never claims a purge over data that is still there, and a re-run picks
    up where it stopped.
    """
    operator = _required(operator, "An operator")
    if confirm != slug:
        raise OffboardingError(
            "--confirm must repeat the tenant slug exactly."
        )
    if skip_grace:
        reason = _required(reason, "A reason for --skip-grace")
    now = now or _now()
    tenant_id = _tenant_id(slug, url)
    with tenant_session(tenant_id, url) as session:
        tenant = session.get(Tenant, tenant_id)
        status = tenant.status
        record = dict((tenant.settings or {}).get("offboarding") or {})
    if status != SUSPENDED:
        raise OffboardingError(
            f"{slug} is {status}; only a suspended tenant can be purged."
        )
    waited = now - datetime.fromisoformat(record["suspended_at"])
    if not skip_grace and waited < timedelta(days=GRACE_DAYS):
        raise OffboardingError(
            f"{slug} was suspended {waited.days} days ago; the recovery "
            f"window is {GRACE_DAYS} days. Wait, or pass --skip-grace with "
            "the customer's written request as --reason."
        )

    blobs = blobs_for(tenant_id)
    documents = blobs.list_keys()
    for key in documents:
        blobs.delete(key)
    if blobs.list_keys():
        raise OffboardingError(f"{slug}'s documents could not all be deleted.")

    with tenant_session(tenant_id, url) as session:
        if is_postgres(session.get_bind()):
            # The only setting `audit_events_purge` answers to; the app role
            # cannot use it, having no DELETE on the table at all.
            session.execute(
                text("SELECT set_config(:name, :tenant_id, true)"),
                {"name": PURGE_SETTING, "tenant_id": tenant_id},
            )
        rows = _delete_rows(session, tenant_id)
        left = _remaining_rows(session, tenant_id)
        if left:
            raise OffboardingError(
                f"{slug} still holds rows after the deletes: {left}. "
                "Nothing was committed."
            )
        record.update(
            purged_at=now.isoformat(),
            purged_by=operator,
            skip_grace=skip_grace,
            skip_reason=reason if skip_grace else None,
            rows_deleted=rows,
            blobs_deleted=len(documents),
        )
        session.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(status=PURGED, settings={"offboarding": record})
        )
    return {"tenant_id": tenant_id, "slug": slug, **record}
```

Note `record` read in the first session may contain `reactivated_at/by` from an earlier cycle; it is kept, which is the history the spec asks for.

Remove the unused `os` import if ruff flags it.

- [ ] **Step 6: The `purge-tenant` subcommand**

In `build_parser`:

```python
    purge = subparsers.add_parser("purge-tenant")
    purge.add_argument("--tenant", required=True)
    purge.add_argument("--operator", required=True)
    purge.add_argument(
        "--confirm", required=True, help="The tenant slug again."
    )
    purge.add_argument(
        "--skip-grace",
        action="store_true",
        help="Purge inside the 30-day window; needs --reason.",
    )
    purge.add_argument("--reason")
```

In `main`, before the `try:`, so the switch happens before any engine exists:

```python
    if args.command == "purge-tenant":
        try:
            offboarding.require_owner_role()
        except offboarding.OffboardingError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
```

and inside the `try`:

```python
        elif args.command == "purge-tenant":
            from ..api import service  # noqa: PLC0415 - the app's blob wiring

            record = offboarding.purge(
                args.tenant,
                operator=args.operator,
                confirm=args.confirm,
                blobs_for=service.get_blobs,
                skip_grace=args.skip_grace,
                reason=args.reason,
            )
            # One line: the operator files it in the offboarding register,
            # the copy that survives a restore from before this purge.
            print(json.dumps(record, sort_keys=True))
```

Add the usage line to the module docstring:

```
    python -m invoice_processing.admin.provision purge-tenant \\
        --tenant acme --operator <you> --confirm acme
```

- [ ] **Step 7: Run the tests**

Run: `./.venv/bin/python -m pytest tests/test_offboarding.py tests/test_db_repositories.py tests/test_gcs_and_migrations.py -q -p no:cacheprovider`
Expected: PASS. If `seed_every_tenant_table` trips on a column (an `AssertionError` naming `table.column`, or an IntegrityError), fix the seeder's type handling, not the schema.

- [ ] **Step 8: Commit**

```bash
git add invoice_processing/adapters.py invoice_processing/db/migrate.py invoice_processing/db/rls.py invoice_processing/offboarding.py invoice_processing/admin/provision.py tests/test_offboarding.py tests/test_db_repositories.py
git commit -m "Purge a suspended tenant's rows and documents, documents first, and keep the record

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The audit purge policy, proven on Postgres

**Files:**
- Modify: `invoice_processing/db/rls.py`
- Create: `invoice_processing/db/migrations/versions/d1a2b3c4e5f6_audit_purge_policy.py`
- Create: `tests/pg_roles.py`
- Modify: `tests/test_gcs_and_migrations.py` (refactor `test_app_role_cannot_rewrite_the_audit_trail` onto the helper; parametrize list; new test)

**Interfaces:**
- Consumes: `rls.PURGE_SETTING` (Task 4).
- Produces: `rls.audit_purge_policy_statements() -> list[str]`, `rls.drop_audit_purge_policy_statements() -> list[str]`; `tests.pg_roles.bootstrapped_database()` context manager yielding a `SimpleNamespace(url, owner_url, app_url, owner, app, alembic)` where `url` is the superuser URL of a fresh database, `alembic(command) -> CompletedProcess` runs Alembic as the owner. Task 6 reuses the helper.

- [ ] **Step 1: Extract the helper**

Create `tests/pg_roles.py` by moving the setup out of `test_app_role_cannot_rewrite_the_audit_trail` (`tests/test_gcs_and_migrations.py` ~795–870):

```python
"""A throwaway Postgres database built the way production is.

bootstrap.sql §1 as a superuser, migrations as the owner role, §2 again as
the superuser — the order deployment/README.md gives — with two fresh
NOSUPERUSER NOBYPASSRLS roles. CI's own user is a superuser and bypasses
RLS, so a claim about policies has to be proven as these roles.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

REPO = Path(__file__).resolve().parent.parent

pg_only = pytest.mark.skipif(
    not (os.getenv("TEST_DATABASE_URL") or "").startswith("postgresql"),
    reason="roles, grants and policies exist only on Postgres",
)


def _bootstrap_parts(owner: str, app: str) -> tuple[list[str], list[str]]:
    sql = (REPO / "invoice_processing" / "db" / "bootstrap.sql").read_text()
    # psql variables, literals before identifiers.
    for var, value in (
        (":'owner_pw'", "'pw'"),
        (":'app_pw'", "'pw'"),
        (":'owner'", f"'{owner}'"),
        (":'app'", f"'{app}'"),
        (':"owner"', owner),
        (':"app"', app),
    ):
        sql = sql.replace(var, value)
    part1, part2 = sql.split("\n-- §2")  # the marker line, as sed finds it

    def statements(part):
        code = "\n".join(
            line
            for line in part.splitlines()
            if not line.strip().startswith("--")
        )
        return [
            s
            for s in code.split(";")
            if s.strip() and "has_table_privilege" not in s
        ]

    return statements(part1), statements(part2)


@contextmanager
def bootstrapped_database():
    server = make_url(os.environ["TEST_DATABASE_URL"])
    suffix = uuid.uuid4().hex[:12]
    name = f"roles_{suffix}"
    owner, app = f"owner_{suffix}", f"app_{suffix}"
    part1, part2 = _bootstrap_parts(owner, app)
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    url = server.set(database=name)
    owner_url = url.set(username=owner, password="pw")

    def alembic(command: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *command.split()],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
            env={
                **os.environ,
                "DATABASE_URL": owner_url.render_as_string(
                    hide_password=False
                ),
            },
        )

    try:
        with admin.connect() as c:
            c.execute(text(f"CREATE DATABASE {name}"))
        su = create_engine(url, isolation_level="AUTOCOMMIT")
        try:
            with su.connect() as c:
                for s in part1:
                    c.execute(text(s))
            up = alembic("upgrade head")
            assert up.returncode == 0, up.stderr[-2000:]
            with su.connect() as c:
                for s in part2:
                    c.execute(text(s))
        finally:
            su.dispose()
        yield SimpleNamespace(
            url=url,
            owner_url=owner_url,
            app_url=url.set(username=app, password="pw"),
            owner=owner,
            app=app,
            alembic=alembic,
        )
    finally:
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            c.execute(text(f"DROP ROLE IF EXISTS {app}"))
            c.execute(text(f"DROP ROLE IF EXISTS {owner}"))
        admin.dispose()
```

Rewrite `test_app_role_cannot_rewrite_the_audit_trail` to open `with bootstrapped_database() as db:`, create `su = create_engine(db.url, isolation_level="AUTOCOMMIT")` for the tenant insert and `has_table_privilege` checks (using `db.app` as the role name), `app = create_engine(db.app_url)`, and keep its assertions and `forbidden` list unchanged; dispose `su` and `app` in its own `finally`. Import with `from tests.pg_roles import bootstrapped_database, pg_only`.

- [ ] **Step 2: Prove the refactor kept the test**

Start Postgres (§6 of the handoff: `docker run -d --rm --name pg-x -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=t -p 55432:5432 postgres:16`), then:
Run: `TEST_DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/t ./.venv/bin/python -m pytest "tests/test_gcs_and_migrations.py::TestMigration::test_app_role_cannot_rewrite_the_audit_trail" -q -p no:cacheprovider`
Expected: PASS (it passed before the refactor; it must still).

- [ ] **Step 3: Write the failing policy test**

Add to `TestMigration` in `tests/test_gcs_and_migrations.py`:

```python
    @pg_only
    def test_only_the_owner_can_purge_and_only_the_named_tenant(self):
        from sqlalchemy import create_engine, text  # noqa: PLC0415
        from sqlalchemy.exc import DBAPIError  # noqa: PLC0415

        from invoice_processing.db import rls  # noqa: PLC0415

        a = "44444444-4444-4444-4444-444444444444"
        b = "55555555-5555-5555-5555-555555555555"
        with bootstrapped_database() as db:
            su = create_engine(db.url, isolation_level="AUTOCOMMIT")
            owner = create_engine(db.owner_url)
            app = create_engine(db.app_url)
            try:
                with su.connect() as c:
                    for tid, slug in ((a, "a"), (b, "b")):
                        c.execute(
                            text(
                                "insert into tenants (id, slug, name, region, "
                                "status, settings, created_at, updated_at) "
                                f"values ('{tid}', '{slug}', '{slug}', 'au', "
                                "'suspended', '{}', now(), now())"
                            )
                        )
                        c.execute(
                            text(
                                "insert into audit_events (id, tenant_id, "
                                "occurred_at, actor_type, action) values "
                                f"('e-{slug}', '{tid}', now(), 'user', 'x')"
                            )
                        )
                scope = text(rls.set_tenant_statement())
                purge = text("select set_config(:name, :t, true)")

                with app.connect() as c:
                    with pytest.raises(DBAPIError) as refused, c.begin():
                        c.execute(scope, {"tenant_id": a})
                        c.execute(purge, {"name": rls.PURGE_SETTING, "t": a})
                        c.execute(text("delete from audit_events"))
                    assert refused.value.orig.args[0]["C"] == "42501"

                with owner.connect() as c, c.begin():
                    c.execute(scope, {"tenant_id": a})
                    gone = c.execute(text("delete from audit_events"))
                    assert gone.rowcount == 0  # no purge setting, no policy

                with owner.connect() as c, c.begin():
                    c.execute(scope, {"tenant_id": a})
                    c.execute(purge, {"name": rls.PURGE_SETTING, "t": a})
                    gone = c.execute(text("delete from audit_events"))
                    assert gone.rowcount == 1

                with su.connect() as c:
                    left = c.execute(
                        text("select tenant_id from audit_events")
                    ).scalars()
                    assert list(left) == [b]

                down = db.alembic("downgrade -1")
                assert down.returncode == 0, down.stderr[-2000:]
                with su.connect() as c:
                    names = c.execute(
                        text(
                            "select policyname from pg_policies "
                            "where tablename = 'audit_events'"
                        )
                    ).scalars()
                    assert "audit_events_purge" not in set(names)
            finally:
                for engine in (su, owner, app):
                    engine.dispose()
```

Add `("d1a2b3c4e5f6", 0),` to the `test_migration_protects_exactly_the_tables_it_creates` parameter list, and add the new test's node id to the CI Postgres list in Task 7.

- [ ] **Step 4: Run it to verify it fails**

Run: `TEST_DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/t ./.venv/bin/python -m pytest "tests/test_gcs_and_migrations.py::TestMigration::test_only_the_owner_can_purge_and_only_the_named_tenant" -q -p no:cacheprovider`
Expected: FAIL at `assert gone.rowcount == 1` (0: there is no DELETE policy yet). The parametrized migration test fails with `IndexError` (no file for `d1a2b3c4e5f6`).

- [ ] **Step 5: The statements and the migration**

`PURGE_SETTING` already exists in `invoice_processing/db/rls.py` (Task 4). Give it its comment if it has none: `# Set only by the offboarding purge, read only by audit_events_purge.`

Change the module docstring sentence about `audit_events` to: "`audit_events` additionally has no UPDATE policy, and its one DELETE policy answers only to `app.purge_tenant`, which the offboarding purge sets as the owner role: the app role has no DELETE on the table at all, so for the service the trail is append-only."

Add after `revoke_audit_mutation_statements`:

```python
def audit_purge_policy_statements() -> list[str]:
    """The one way an audit row is ever deleted: a tenant's whole trail, by
    its offboarding purge (offboarding.purge).

    Not part of `enable_rls_statements`, which migrations replay as history:
    adding it there would make the initial migration create a policy the
    later one then fails to create again.
    """
    return [
        "CREATE POLICY audit_events_purge ON audit_events FOR DELETE "
        f"USING (tenant_id = current_setting('{PURGE_SETTING}', true));"
    ]


def drop_audit_purge_policy_statements() -> list[str]:
    return ["DROP POLICY IF EXISTS audit_events_purge ON audit_events;"]
```

Create `invoice_processing/db/migrations/versions/d1a2b3c4e5f6_audit_purge_policy.py`:

```python
"""audit_events_purge: the offboarding purge's DELETE policy

The audit trail had no DELETE policy, so with RLS forced even the owner
deleted nothing. Offboarding must erase a tenant's whole trail; this lets
the owner role do that for the one tenant named in app.purge_tenant. The
app role still has no DELETE privilege on the table.

Revision ID: d1a2b3c4e5f6
Revises: c9d5e2f3a4b6
Create Date: 2026-10-13
"""

from collections.abc import Sequence

from alembic import op

from invoice_processing.db.rls import (
    audit_purge_policy_statements,
    drop_audit_purge_policy_statements,
)

revision: str = "d1a2b3c4e5f6"
down_revision: str | None = "c9d5e2f3a4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RLS_TABLES: list[str] = []


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in audit_purge_policy_statements():
        op.execute(statement)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for statement in drop_audit_purge_policy_statements():
        op.execute(statement)
```

- [ ] **Step 6: Run the migration tests on both dialects**

Run: `./.venv/bin/python -m pytest tests/test_gcs_and_migrations.py -q -p no:cacheprovider`
Then: `TEST_DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/t ./.venv/bin/python -m pytest tests/test_gcs_and_migrations.py tests/test_offboarding.py -q -p no:cacheprovider`
Expected: PASS on both. On Postgres, `tests/test_offboarding.py` runs as CI's superuser, which bypasses RLS — that is why the policy itself is proven by the role test above.

- [ ] **Step 7: Commit**

```bash
git add invoice_processing/db/rls.py invoice_processing/db/migrations/versions/d1a2b3c4e5f6_audit_purge_policy.py tests/pg_roles.py tests/test_gcs_and_migrations.py
git commit -m "Give the audit trail one DELETE policy, for the owner's purge of one named tenant

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: The restore check

**Files:**
- Create: `invoice_processing/db/restore_check.py`
- Test: `tests/test_restore_check.py`

**Interfaces:**
- Consumes: `tests.pg_roles.bootstrapped_database`, `pg_only` (Task 5); `rls.set_tenant_statement`; `TENANT_TABLES`.
- Produces: `restore_check.expected_policies(table) -> set[str]`; `repo_head() -> str`; `check(conn, app_role) -> list[str]`; `counts(conn) -> tuple[dict[str, dict[str, int]], datetime | None]`; `compare(source, restored) -> list[str]`; `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_restore_check.py`:

```python
"""The check run after any restore, however the restore was made."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from invoice_processing.db import restore_check
from tests.pg_roles import bootstrapped_database, pg_only

TENANT = "66666666-6666-6666-6666-666666666666"


class TestCompare:
    def test_equal_counts_pass(self):
        rows = {TENANT: {"invoices": 3}}
        assert restore_check.compare(rows, rows) == []

    def test_a_difference_names_the_tenant_and_table(self):
        failures = restore_check.compare(
            {TENANT: {"invoices": 3}}, {TENANT: {"invoices": 2}}
        )
        assert failures == [f"{TENANT} invoices: source 3, restored 2"]

    def test_a_missing_tenant_is_a_difference(self):
        failures = restore_check.compare({TENANT: {"invoices": 1}}, {})
        assert failures == [f"{TENANT} invoices: source 1, restored 0"]


def test_the_head_is_the_newest_migration():
    assert restore_check.repo_head() == "d1a2b3c4e5f6"


@pg_only
class TestOnPostgres:
    @pytest.fixture
    def db(self):
        with bootstrapped_database() as db:
            su = create_engine(db.url, isolation_level="AUTOCOMMIT")
            with su.connect() as c:
                c.execute(
                    text(
                        "insert into tenants (id, slug, name, region, status, "
                        "settings, created_at, updated_at) values "
                        f"('{TENANT}', 'acme', 'Acme', 'au', 'suspended', "
                        "'{}', now(), now())"
                    )
                )
                c.execute(
                    text(
                        "insert into audit_events (id, tenant_id, "
                        "occurred_at, actor_type, action) values "
                        f"('e1', '{TENANT}', now(), 'user', 'x')"
                    )
                )
            db.su = su
            yield db
            su.dispose()

    def _check(self, db):
        owner = create_engine(db.owner_url)
        try:
            with owner.connect() as c:
                return restore_check.check(c, db.app)
        finally:
            owner.dispose()

    def test_a_correctly_built_database_passes(self, db):
        assert self._check(db) == []

    def test_counts_are_read_per_tenant_as_the_owner(self, db):
        owner = create_engine(db.owner_url)
        try:
            with owner.connect() as c:
                rows, newest = restore_check.counts(c)
        finally:
            owner.dispose()
        assert rows[TENANT]["audit_events"] == 1
        assert rows[TENANT]["invoices"] == 0
        assert newest is not None

    def test_rls_not_forced_fails(self, db):
        with db.su.connect() as c:
            c.execute(text("alter table invoices no force row level security"))
        assert any(f.startswith("invoices:") for f in self._check(db))

    def test_an_app_role_that_can_delete_audit_rows_fails(self, db):
        with db.su.connect() as c:
            c.execute(text(f"grant delete on audit_events to {db.app}"))
        assert f"app role {db.app} can DELETE audit_events" in self._check(db)

    def test_a_missing_purge_policy_fails(self, db):
        with db.su.connect() as c:
            c.execute(text("drop policy audit_events_purge on audit_events"))
        assert any("audit_events_purge" in f for f in self._check(db))

    def test_a_wrong_head_fails(self, db):
        with db.su.connect() as c:
            c.execute(text("update alembic_version set version_num = 'x'"))
        assert any(f.startswith("schema is at x") for f in self._check(db))

    def test_main_compares_and_reports(self, db, capsys):
        owner = db.owner_url.render_as_string(hide_password=False)
        code = restore_check.main(
            ["--restored", owner, "--source", owner, "--app-role", db.app]
        )
        out = capsys.readouterr().out
        assert code == 0, out
        assert "acme: suspended" in out
        assert out.strip().endswith("PASS")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_restore_check.py -q -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'restore_check'`.

- [ ] **Step 3: Write `invoice_processing/db/restore_check.py`**

```python
"""After a restore: is this the database we promised?

Run against a database restored by any means (a Cloud SQL backup or
point-in-time clone, or `pg_restore` in the local drill), as the owner
role, after bootstrap §2:

    python -m invoice_processing.db.restore_check --restored <owner-url> \\
        [--source <owner-url>] [--app-role ledgerly_app]

It checks what a restore can silently lose -- the schema version, forced
row-level security and every policy, and the app role's lack of power over
the audit trail (bootstrap §2, which a restore does not carry) -- and
prints row counts per tenant, the newest audit event (the effective
restore point) and every tenant that is not active, which after a restore
from before a purge is the cue to purge it again (deployment/BACKUP_RESTORE.md).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from sqlalchemy import create_engine, text

from .models import TENANT_TABLES
from .rls import set_tenant_statement


def expected_policies(table: str) -> set[str]:
    if table == "audit_events":
        return {
            "audit_events_select",
            "audit_events_insert",
            "audit_events_purge",
        }
    if table == "sso_domains":
        return {"sso_domains_isolation", "sso_domains_lookup"}
    return {f"{table}_isolation"}


def repo_head() -> str:
    from alembic.config import Config  # noqa: PLC0415 - only this needs it
    from alembic.script import ScriptDirectory  # noqa: PLC0415

    from .. import paths  # noqa: PLC0415

    config = Config()
    config.set_main_option(
        "script_location",
        str(paths.AGENT_ROOT / "invoice_processing" / "db" / "migrations"),
    )
    return ScriptDirectory.from_config(config).get_current_head()


def check(conn, app_role: str) -> list[str]:
    failures = []
    version = conn.execute(
        text("select version_num from alembic_version")
    ).scalar()
    head = repo_head()
    if version != head:
        failures.append(f"schema is at {version}, the code expects {head}")

    flags = {
        name: (enabled, forced)
        for name, enabled, forced in conn.execute(
            text(
                "select relname, relrowsecurity, relforcerowsecurity "
                "from pg_class where relkind = 'r' "
                "and relnamespace = 'public'::regnamespace"
            )
        )
    }
    policies: dict[str, set[str]] = {}
    for table, policy in conn.execute(
        text(
            "select tablename, policyname from pg_policies "
            "where schemaname = 'public'"
        )
    ):
        policies.setdefault(table, set()).add(policy)
    for table in TENANT_TABLES:
        if table not in flags:
            failures.append(f"{table}: missing")
            continue
        if flags[table] != (True, True):
            failures.append(
                f"{table}: row-level security is not enabled and forced"
            )
        missing = expected_policies(table) - policies.get(table, set())
        if missing:
            failures.append(f"{table}: missing policies {sorted(missing)}")

    role = conn.execute(
        text("select rolbypassrls from pg_roles where rolname = :r"),
        {"r": app_role},
    ).first()
    if role is None:
        failures.append(f"app role {app_role} does not exist")
        return failures
    if role[0]:
        failures.append(f"app role {app_role} bypasses row-level security")
    for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
        if conn.execute(
            text("select has_table_privilege(:r, 'audit_events', :p)"),
            {"r": app_role, "p": privilege},
        ).scalar():
            failures.append(f"app role {app_role} can {privilege} audit_events")
    return failures


def counts(conn) -> tuple[dict[str, dict[str, int]], datetime | None]:
    """Rows per tenant table per tenant, each read under that tenant's own
    setting (RLS is forced, so the owner sees nothing without one)."""
    rows: dict[str, dict[str, int]] = {}
    newest = None
    tenant_ids = conn.execute(text("select id from tenants order by id"))
    for tenant_id in list(tenant_ids.scalars()):
        conn.execute(text(set_tenant_statement()), {"tenant_id": tenant_id})
        rows[tenant_id] = {
            # Names from TENANT_TABLES, never from input.
            table: conn.execute(
                text(f"select count(*) from {table}")  # noqa: S608
            ).scalar()
            for table in TENANT_TABLES
        }
        latest = conn.execute(
            text("select max(occurred_at) from audit_events")
        ).scalar()
        if latest is not None and (newest is None or latest > newest):
            newest = latest
        conn.rollback()  # the setting is transaction-local; start clean
    return rows, newest


def compare(
    source: dict[str, dict[str, int]], restored: dict[str, dict[str, int]]
) -> list[str]:
    failures = []
    for tenant_id in sorted(set(source) | set(restored)):
        want, got = source.get(tenant_id, {}), restored.get(tenant_id, {})
        for table in sorted(set(want) | set(got)):
            a, b = want.get(table, 0), got.get(table, 0)
            if a != b:
                failures.append(
                    f"{tenant_id} {table}: source {a}, restored {b}"
                )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="restore_check")
    parser.add_argument("--restored", required=True, help="Owner-role URL.")
    parser.add_argument(
        "--source", help="Owner-role URL of a source that is not changing."
    )
    parser.add_argument("--app-role", default="ledgerly_app")
    args = parser.parse_args(argv)

    engine = create_engine(args.restored)
    try:
        with engine.connect() as conn:
            failures = check(conn, args.app_role)
            conn.rollback()
            rows, newest = counts(conn)
            closed = conn.execute(
                text(
                    "select slug, status from tenants "
                    "where status <> 'active' order by slug"
                )
            ).all()
    finally:
        engine.dispose()
    if args.source:
        source = create_engine(args.source)
        try:
            with source.connect() as conn:
                failures += compare(counts(conn)[0], rows)
        finally:
            source.dispose()

    print(json.dumps({"rows": rows}, indent=2, sort_keys=True))
    print(f"newest audit event: {newest}")
    for slug, status in closed:
        print(f"not active: {slug}: {status}")
    for failure in failures:
        print(f"FAIL {failure}")
    print(f"{len(failures)} failures" if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Fix the test's expected substring to the printed form `not active: acme: suspended` (the test asserts `"acme: suspended" in out`, which matches). If ruff does not enable the `S` rules, drop the `# noqa: S608` (ruff reports unused noqa as RUF100 when enabled) and keep the comment above it.

- [ ] **Step 4: Run the tests on both dialects**

Run: `./.venv/bin/python -m pytest tests/test_restore_check.py -q -p no:cacheprovider`
Then: `TEST_DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/t ./.venv/bin/python -m pytest tests/test_restore_check.py -q -p no:cacheprovider`
Expected: PASS (SQLite runs `TestCompare` and the head test; Postgres runs all).

- [ ] **Step 5: Commit**

```bash
git add invoice_processing/db/restore_check.py tests/test_restore_check.py
git commit -m "Check a restored database for what a restore can silently lose

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Policies, runbooks, CI, and the local drill

**Files:**
- Modify: `SECURITY.md` (new section before `## Known gap`)
- Create: `deployment/OFFBOARDING.md`, `deployment/BACKUP_RESTORE.md`
- Modify: `.github/workflows/ci.yml` (Postgres list, ~line 157)
- Modify: `docs/superpowers/specs/2026-10-13-data-lifecycle-design.md` (record the planning rulings)

- [ ] **Step 1: CI Postgres list**

In `.github/workflows/ci.yml`, add to the `DB-backed tests against Postgres` command, after `tests/test_signup_api.py \`:

```yaml
                        tests/test_offboarding.py \
                        tests/test_restore_check.py \
```

and after the last `test_gcs_and_migrations.py` node id (move the ` -q` to the new last line):

```yaml
                        "tests/test_gcs_and_migrations.py::TestMigration::test_only_the_owner_can_purge_and_only_the_named_tenant" -q
```

Check there is a test that greps the CI list against the files (`grep -rn "ci.yml" tests`); if one exists, run it.

- [ ] **Step 2: `SECURITY.md` → "Data lifecycle"**

Insert before `## Known gap`:

```markdown
## Data lifecycle

**Retention.** Customer data is kept for the life of the account. The
customer is the record-keeper under its own tax law (five years in
Australia, six to eight under India GST), so we do not delete by age.

**Offboarding.** Customers export what they need before their subscription
ends. Then an operator suspends the tenant: sign-in, the API, email intake,
the worker and approval escalations all refuse it from the next request.
For 30 days it can be reactivated. After that, or at once on the
customer's written request, the operator purges it: every row in every
tenant table, every document, and the tenant's audit trail. The tenants row
stays as the record (id, slug, name, region, who suspended and purged it,
when, and how many rows and documents went), and the slug stays reserved.
The purge runs as the database owner role; the service's own role cannot
delete an audit row. Steps: [deployment/OFFBOARDING.md](deployment/OFFBOARDING.md).

**What outlives a purge, and for how long.** Database backups and
point-in-time logs: up to 7 days. Deleted documents in Cloud Storage soft
delete: up to 7 days. Application logs on Railway: 7 days. Sentry events:
90 days. Logs and Sentry carry ids, never invoice content or email
addresses.

**Backup and restore.** Cloud SQL daily backups (7 kept) and
point-in-time recovery. Targets: data loss of minutes (RPO), service back
within 4 hours (RTO). A restore drill runs every 6 months and after any
production restore, checked by `python -m invoice_processing.db.restore_check`.
Steps and the drill log: [deployment/BACKUP_RESTORE.md](deployment/BACKUP_RESTORE.md).
```

- [ ] **Step 3: `deployment/OFFBOARDING.md`**

```markdown
# Offboarding a tenant

Policy: SECURITY.md, "Data lifecycle". Every command below runs from the
repo (or a release container) with the production environment.

1. **Confirm the request** in writing from an admin of the tenant, and that
   they have exported what they need (Activity → Export for the audit
   trail; the sheet and invoice exports).
2. **Suspend.** Sign-in, the API, email intake, the worker and escalations
   refuse the tenant from the next request.

       python -m invoice_processing.admin.provision suspend-tenant \
           --tenant <slug> --operator <you> --reason "<ticket / reason>"

   A mistake inside 30 days is undone with `reactivate-tenant --tenant
   <slug> --operator <you>`. Jobs dead-lettered meanwhile ("tenant
   suspended") can then be replayed.
3. **Wait 30 days**, or skip the wait only with the customer's written
   request for immediate deletion (`--skip-grace --reason "<the request>"`).
4. **Purge as the owner role.** `MIGRATE_DATABASE_URL` (or
   `MIGRATE_CLOUD_SQL_USER`) must be set, as for the release step; the
   command refuses the app role.

       python -m invoice_processing.admin.provision purge-tenant \
           --tenant <slug> --operator <you> --confirm <slug>

   It deletes the documents, then every row in one transaction, and checks
   nothing is left before marking the tenant purged. On any error the
   tenant stays suspended; fix the cause and run it again.
5. **File the record.** The command's last line is one JSON object. Add it
   to the offboarding register (the private document outside the
   database). After a restore from a backup older than the purge, that
   register is the only list of tenants to purge again
   (deployment/BACKUP_RESTORE.md).
6. **Delete the Identity Platform users** of that tenant in the Google
   Cloud console (Identity Platform → Users, filter by the tenant's email
   domain). Their tokens are already refused; this removes the accounts.
```

- [ ] **Step 4: `deployment/BACKUP_RESTORE.md`**

```markdown
# Backup and restore

Targets (SECURITY.md): RPO minutes, RTO 4 hours, a drill every 6 months
and after any production restore.

## What is backed up

- **Cloud SQL:** automated daily backups, 7 retained; point-in-time
  recovery with 7 days of transaction logs. Check both are on: Cloud SQL →
  instance → Backups.
- **Cloud Storage (documents):** soft delete, 7 days. Documents are written
  once and never modified, so soft delete covers accidental deletion; there
  is no object versioning.

## Restoring the database

1. Restore into a **new instance** (Backups → Restore, or point-in-time
   clone to the moment before the incident). Never over the live one.
2. Run `db/bootstrap.sql` §2 on it as the admin user (deployment/README.md
   gives the command): a restore carries the tables but a re-run is what
   guarantees the app role's grants and the audit REVOKE.
3. Check it, as the owner role:

       python -m invoice_processing.db.restore_check \
           --restored "<owner url of the new instance>"

   Exit 0 and `PASS`, or stop and read each `FAIL` line. Note the
   "newest audit event" (the effective restore point) and every
   "not active" tenant.
4. **Re-purge.** For every entry in the offboarding register whose
   `purged_at` is after the restore point, run
   `purge-tenant --tenant <slug> --operator <you> --confirm <slug>
   --skip-grace --reason "re-purge after restore"`.
5. Point `DATABASE_URL` and `MIGRATE_DATABASE_URL` (Railway, both
   services) at the new instance and redeploy.

## Restoring documents

Cloud Storage → bucket → Soft-deleted objects → restore. A purge's
deletions are also soft-deleted for 7 days; do not restore those.

## Local drill

Proves the procedure and the check without production. `pg_dump` and
`pg_restore` run inside the Postgres container (the dev machine has
none).

    docker run -d --rm --name drill-src -e POSTGRES_PASSWORD=postgres \
        -e POSTGRES_DB=ledgerly -p 55433:5432 postgres:16
    docker run -d --rm --name drill-dst -e POSTGRES_PASSWORD=postgres \
        -e POSTGRES_DB=ledgerly -p 55434:5432 postgres:16
    # source: bootstrap §1, migrate as owner, §2, then seed through the app
    # (see "Seeding the source" below)
    docker exec drill-src pg_dump -U postgres -Fc ledgerly -f /tmp/l.dump
    docker cp drill-src:/tmp/l.dump /tmp/l.dump
    docker cp /tmp/l.dump drill-dst:/tmp/l.dump
    # destination: bootstrap §1 only (the roles), then
    docker exec drill-dst pg_restore -U postgres -d ledgerly /tmp/l.dump
    # then bootstrap §2 on the destination, then
    ./.venv/bin/python -m invoice_processing.db.restore_check \
        --restored postgresql+pg8000://ledgerly_owner:<pw>@localhost:55434/ledgerly \
        --source postgresql+pg8000://ledgerly_owner:<pw>@localhost:55433/ledgerly
    docker stop drill-src drill-dst

## Drill log

| Date | Where | Restore point | Result | By |
|---|---|---|---|---|
```

During execution, replace the `# ...` comment lines with the exact commands actually used (the bootstrap `psql` invocation from `deployment/README.md` run through `docker exec -i drill-src psql -U postgres -d ledgerly ... -f - < invoice_processing/db/bootstrap.sql`, and the seeding commands), then run the drill end to end and add its row to the drill log with the `restore_check` result. Seed the source with: `create-tenant` × 2 via `provision` (owner URL as `DATABASE_URL`), the e2e seeding script if it targets a URL (`scripts/e2e_api.py` — read it first), or a short loop of `offboarding.suspend` / `Provisioner.add_user`; the point is non-zero counts in several tables and one suspended tenant.

- [ ] **Step 5: Record the planning rulings in the spec**

Append to the spec a `## Changes made while planning (2026-10-13)` section with the six rulings from this plan's "Rulings made while planning", one line each.

- [ ] **Step 6: Run the full suite and lint**

Run: `./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider`
Run: `./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format --check .`
Expected: all pass (previous baseline 2745 passed, 4 skipped; the new count is higher, Postgres-only tests skip).

- [ ] **Step 7: Commit**

```bash
git add SECURITY.md deployment/OFFBOARDING.md deployment/BACKUP_RESTORE.md .github/workflows/ci.yml docs/superpowers/specs/2026-10-13-data-lifecycle-design.md
git commit -m "Write down how long data lives, how a tenant leaves, and how a restore is checked

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Verification

- [ ] **Step 1: The CI Postgres list locally** (handoff §6 command; the awk reads the updated `ci.yml`). Expected: previous 826 plus the new tests, all passing.
- [ ] **Step 2: The gates:** `bash benchmark/harness/gates.sh benchmark/runs/gates-data-lifecycle`. Expected: 9/9.
- [ ] **Step 3: Manual stack.** Back up `.claude/launch.json`, add `check-api-8200` / `check-frontend-3200`, start them. Then, with `DATABASE_URL` pointing at the stack's SQLite DB:
  - `curl -H 'X-Dev-Principal: clerk@bench.test:clerk' http://127.0.0.1:8200/api/invoices` → 200.
  - `suspend-tenant --tenant bench-a ...` → same curl → 403 with the suspension sentence; the browser at :3200 shows the sentence (not a generic error). If the frontend's `friendly()` maps 403 to its own sentence and hides the detail, record it as a finding for the fix pass.
  - `reactivate-tenant` → 200 again.
  - Do **not** purge `bench-a` (the manual DB is reused across sessions). Create a scratch tenant, seed one document through the API, suspend it, `purge-tenant --skip-grace --reason test`, and confirm zero rows and an empty blob directory with `offboarding.leftovers`.
  - Restore `launch.json`.
- [ ] **Step 4: The local restore drill** from Task 7 Step 4, with its row in the drill log (commit the log update).
- [ ] **Step 5: Whole-branch review** on the `fable` model, one fix pass (each fix tested first), all minors fixed.
