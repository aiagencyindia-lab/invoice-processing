"""Per-tenant API keys (PRD §9, FR-30): create, resolve, revoke."""

import hashlib
import json

import pytest
from fresh_schema import fresh_schema
from sqlalchemy import select, text

from invoice_processing.adapters import JsonlAuditSink
from invoice_processing.api.auth import AuthError
from invoice_processing.db import models
from invoice_processing.db.session import get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust import apikeys
from tests.pg_roles import bootstrapped_database, pg_only

ACME, GLOBEX = "tenant-acme", "tenant-globex"
ADMIN = RunContext(tenant_id=ACME, case_id="", run_id="r", actor="a@acme.test")


@pytest.fixture
def env(scratch_db, monkeypatch, tmp_path):
    url = scratch_db("keys.db")
    fresh_schema(url)
    for tid, slug in ((ACME, "acme"), (GLOBEX, "globex")):
        with tenant_session(tid, url) as s:
            s.add(models.Tenant(id=tid, name=slug, slug=slug, status="active"))
    monkeypatch.setenv("DATABASE_URL", url)
    audit = JsonlAuditSink(tmp_path / "audit.jsonl")
    return url, audit, tmp_path / "audit.jsonl"


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_create_shows_the_key_once_and_keeps_only_its_hash(env):
    url, audit, _ = env
    row, key = apikeys.create(ADMIN, "erp", "clerk", audit=audit, url=url)
    assert key.startswith(f"ik_{row['prefix']}_")
    assert len(row["prefix"]) == 8
    secret = key[len(f"ik_{row['prefix']}_") :]
    with tenant_session(ACME, url) as s:
        stored = s.scalars(select(models.ApiKey)).one()
    assert stored.key_hash == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in json.dumps(row, default=str)
    assert "key_hash" not in row
    assert row["role"] == "clerk" and row["name"] == "erp"
    assert row["created_by"] == "a@acme.test"


def test_an_admin_key_is_refused(env):
    url, audit, _ = env
    with pytest.raises(ValueError):
        apikeys.create(ADMIN, "root", "admin", audit=audit, url=url)
    with pytest.raises(ValueError):
        apikeys.create(ADMIN, "x", "superuser", audit=audit, url=url)
    with pytest.raises(ValueError):
        apikeys.create(ADMIN, "  ", "viewer", audit=audit, url=url)


def test_a_new_key_cannot_take_a_registered_agents_name(env):
    """Agent control knows a key by `{name}@api-key.{tenant}`; a second key
    with that name would write as the registered agent (Wave 4 audit)."""
    url, audit, _ = env
    with tenant_session(ACME, url) as s:
        s.add(
            models.Agent(
                tenant_id=ACME,
                name="bot",
                owner="ops@acme.test",
                principal_id="api-key:k1",
                principal_email=f"bot@api-key.{ACME}",
                kind="api_key",
            )
        )
    with pytest.raises(ValueError, match="agent"):
        apikeys.create(ADMIN, "BOT", "approver", audit=audit, url=url)
    # Another tenant's agent does not reserve the name here.
    apikeys.create(
        RunContext(tenant_id=GLOBEX, case_id="", run_id="", actor="g@x.test"),
        "bot",
        "approver",
        audit=audit,
        url=url,
    )


def test_resolve_returns_the_keys_principal_and_stamps_use(env):
    url, audit, _ = env
    row, key = apikeys.create(ADMIN, "erp", "approver", audit=audit, url=url)
    p = apikeys.resolve(key)
    assert p.user_id == f"api-key:{row['id']}"
    assert p.email == f"erp@api-key.{ACME}"
    assert p.tenant_id == ACME and p.role == "approver"
    assert apikeys.list_keys(ACME, url=url)[0]["last_used_at"] is not None


@pytest.mark.parametrize(
    "token",
    [
        "",
        "ik_",
        "ik_x",
        "ik_abcd1234_",
        "ik_abcd1234",
        "ik__secret",
        "ik_ÿÿÿÿÿÿÿÿ_secret",
        "nope",
    ],
)
def test_a_malformed_key_is_none(env, token):
    assert apikeys.resolve(token) is None


def test_a_wrong_secret_or_unknown_prefix_is_none(env):
    url, audit, _ = env
    _, key = apikeys.create(ADMIN, "erp", "viewer", audit=audit, url=url)
    assert apikeys.resolve(key + "x") is None
    assert apikeys.resolve(f"ik_00000000_{key.split('_', 2)[2]}") is None


def test_a_revoked_key_is_none_and_both_changes_are_audited(env):
    url, audit, path = env
    row, key = apikeys.create(ADMIN, "erp", "viewer", audit=audit, url=url)
    out = apikeys.revoke(ADMIN, row["id"], audit=audit, url=url)
    assert out["revoked_at"] is not None
    assert apikeys.resolve(key) is None
    kinds = [(e["event_type"], e["payload"]) for e in _events(path)]
    assert kinds == [
        (
            "api_key_created",
            {"name": "erp", "role": "viewer", "prefix": row["prefix"]},
        ),
        ("api_key_revoked", {"name": "erp", "prefix": row["prefix"]}),
    ]


def test_revoking_twice_or_another_tenants_key_is_none(env):
    url, audit, _ = env
    row, _ = apikeys.create(ADMIN, "erp", "viewer", audit=audit, url=url)
    other = RunContext(tenant_id=GLOBEX, case_id="", run_id="r", actor="g")
    assert apikeys.revoke(other, row["id"], audit=audit, url=url) is None
    assert apikeys.revoke(ADMIN, row["id"], audit=audit, url=url) is not None
    assert apikeys.revoke(ADMIN, row["id"], audit=audit, url=url) is None


def test_a_key_of_a_suspended_tenant_is_none(env):
    url, audit, _ = env
    _, key = apikeys.create(ADMIN, "erp", "viewer", audit=audit, url=url)
    with tenant_session(ACME, url) as s:
        s.get(models.Tenant, ACME).status = "suspended"
    assert apikeys.resolve(key) is None


def test_the_same_prefix_in_two_tenants_resolves_by_secret(env, monkeypatch):
    url, audit, _ = env
    monkeypatch.setattr(apikeys, "_prefix", lambda: "deadbeef")
    _, a = apikeys.create(ADMIN, "a", "viewer", audit=audit, url=url)
    other = RunContext(tenant_id=GLOBEX, case_id="", run_id="r", actor="g")
    _, g = apikeys.create(other, "g", "viewer", audit=audit, url=url)
    assert apikeys.resolve(a).tenant_id == ACME
    assert apikeys.resolve(g).tenant_id == GLOBEX


def test_list_keys_is_per_tenant(env):
    url, audit, _ = env
    apikeys.create(ADMIN, "erp", "viewer", audit=audit, url=url)
    assert [k["name"] for k in apikeys.list_keys(ACME, url=url)] == ["erp"]
    assert apikeys.list_keys(GLOBEX, url=url) == []


def test_an_unreachable_database_is_a_503_not_a_401(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:////nonexistent/dir/x.db")
    with pytest.raises(AuthError) as err:
        apikeys.resolve("ik_abcd1234_" + "s" * 43)
    assert err.value.status_code == 503


@pg_only
def test_a_key_resolves_as_the_production_app_role(monkeypatch):
    """`api_keys` forces RLS and the app role cannot bypass it: the lookup
    must work through tenant sessions, not an unscoped one."""
    with bootstrapped_database() as db:
        owner = db.owner_url.render_as_string(hide_password=False)
        app = db.app_url.render_as_string(hide_password=False)
        with tenant_session(ACME, owner) as s:
            s.add(models.Tenant(id=ACME, name="a", slug="a", status="active"))
        _, key = apikeys.create(ADMIN, "erp", "viewer", url=app)
        monkeypatch.setenv("DATABASE_URL", app)
        p = apikeys.resolve(key)
        assert p is not None and p.tenant_id == ACME
        # An unscoped session really does see nothing as this role.
        with get_engine(app).connect() as c:
            assert (
                c.execute(text("select count(*) from api_keys")).scalar() == 0
            )


def test_a_failed_use_stamp_does_not_refuse_the_key(env, monkeypatch):
    url, audit, _ = env
    _, key = apikeys.create(ADMIN, "erp", "viewer", audit=audit, url=url)

    def broken(*a, **kw):
        raise RuntimeError("stamp failed")

    monkeypatch.setattr(apikeys, "_stamp", broken)
    assert apikeys.resolve(key).tenant_id == ACME
