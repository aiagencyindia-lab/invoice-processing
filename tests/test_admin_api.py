"""Tenant and user administration over the API.

Provisioning was CLI-only, so onboarding a colleague required shell access.
Exposing it introduces one boundary worth defending carefully: an admin here is
a *customer's* administrator, not the platform operator.

That distinction drives most of these tests. Managing users is a tenant-admin
action and is always scoped to the caller's own tenant. Creating tenants is a
platform action — if it were open, any customer admin could mint namespaces,
inbound-email addresses and storage prefixes inside the system — so it is gated
behind an environment flag that is off by default.

Claim writing is dry-run unless explicitly enabled, mirroring the CLI: granting
real access should not happen because a form was submitted.
"""

import json

import pytest
from fastapi.testclient import TestClient

from invoice_processing.adapters import JsonlAuditSink
from invoice_processing.admin.provision import DryRunClaims, Provisioner
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.db.models import Base
from invoice_processing.db.session import get_engine


class FakeVerifier:
    """Token format: `<role>@<tenant-id>`."""

    def verify(self, token):
        role, _, tenant = token.partition("@")
        if role not in ("viewer", "clerk", "approver", "admin"):
            raise auth.AuthError("Invalid token")
        return {
            "sub": f"u-{token}",
            "email": f"{role}@x.test",
            "email_verified": True,
            "tenant_id": tenant,
            "role": role,
        }


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A real SQLite database with two provisioned tenants."""
    url = f"sqlite:///{tmp_path / 'admin.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    Base.metadata.create_all(get_engine(url))

    claims = DryRunClaims()
    provisioner = Provisioner(claims=claims, url=url)
    acme = provisioner.create_tenant("acme", "Acme Pty Ltd")
    globex = provisioner.create_tenant("globex", "Globex Inc")

    monkeypatch.setattr(service, "_claims_writer", claims)
    monkeypatch.setattr(
        service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.delenv("ALLOW_TENANT_PROVISIONING", raising=False)
    monkeypatch.delenv("APPLY_IDENTITY_CLAIMS", raising=False)

    return {
        "acme": acme,
        "globex": globex,
        "claims": claims,
        "provisioner": provisioner,
        "client": lambda role, tenant=acme["id"]: TestClient(
            app, headers={"Authorization": f"Bearer {role}@{tenant}"}
        ),
    }


# ---------------------------------------------------------------------------
# The privilege boundary
# ---------------------------------------------------------------------------


class TestTenantCreationIsAPlatformAction:
    def test_refused_by_default(self, env):
        """An open endpoint lets any customer admin mint namespaces."""
        response = env["client"]("admin").post(
            "/api/admin/tenants", json={"slug": "sneaky", "name": "Sneaky Ltd"}
        )
        assert response.status_code == 403
        assert "platform operation" in response.json()["detail"]

    def test_the_refusal_says_how_to_do_it_properly(self, env):
        detail = (
            env["client"]("admin")
            .post("/api/admin/tenants", json={"slug": "x", "name": "X"})
            .json()["detail"]
        )
        assert "provision create-tenant" in detail

    def test_allowed_when_the_deployment_opts_in(self, env, monkeypatch):
        monkeypatch.setenv("ALLOW_TENANT_PROVISIONING", "1")
        response = env["client"]("admin").post(
            "/api/admin/tenants", json={"slug": "newco", "name": "NewCo"}
        )
        assert response.status_code == 200
        assert response.json()["tenant"]["slug"] == "newco"

    def test_an_invalid_slug_is_still_refused_when_enabled(
        self, env, monkeypatch
    ):
        monkeypatch.setenv("ALLOW_TENANT_PROVISIONING", "1")
        response = env["client"]("admin").post(
            "/api/admin/tenants", json={"slug": "Bad Slug", "name": "X"}
        )
        assert response.status_code == 400

    def test_the_flag_is_reported_so_the_ui_can_hide_the_button(self, env):
        body = env["client"]("admin").get("/api/admin/tenants").json()
        assert body["can_create_tenants"] is False


class TestOwnTenantOnly:
    def test_returns_the_callers_tenant(self, env):
        body = env["client"]("admin").get("/api/admin/tenants").json()
        assert body["tenant"]["slug"] == "acme"

    def test_never_returns_a_listing_of_all_tenants(self, env):
        """A list of every tenant is a customer list."""
        body = env["client"]("admin").get("/api/admin/tenants").json()
        assert "globex" not in str(body)

    def test_shows_the_inbound_address(self, env, monkeypatch):
        monkeypatch.setenv("INBOUND_EMAIL_DOMAIN", "invoices.example.com")
        body = env["client"]("admin").get("/api/admin/tenants").json()
        assert body["tenant"]["inbox"] == "acme@invoices.example.com"

    def test_a_tenant_with_no_record_is_404_not_a_fallback(self, env):
        response = env["client"]("admin", tenant="tenant-does-not-exist").get(
            "/api/admin/tenants"
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class TestUsers:
    def test_create_then_list(self, env):
        client = env["client"]("admin")
        client.post(
            "/api/admin/users", json={"email": "ap@acme.test", "role": "clerk"}
        )
        body = client.get("/api/admin/users").json()
        assert body["count"] == 1
        assert body["users"][0]["email"] == "ap@acme.test"

    def test_the_tenant_comes_from_the_token_not_the_body(self, env):
        """The rule the whole API follows: no endpoint accepts a tenant."""
        env["client"]("admin", tenant=env["globex"]["id"]).post(
            "/api/admin/users",
            json={
                "email": "spy@globex.test",
                "role": "admin",
                "tenant_id": env["acme"]["id"],
            },
        )
        acme_users = env["client"]("admin").get("/api/admin/users").json()
        assert acme_users["count"] == 0

    def test_users_do_not_leak_between_tenants(self, env):
        env["client"]("admin").post(
            "/api/admin/users", json={"email": "ap@acme.test"}
        )
        globex = env["client"]("admin", tenant=env["globex"]["id"])
        assert globex.get("/api/admin/users").json()["count"] == 0

    def test_a_user_without_a_uid_is_warned_about(self, env):
        """The row exists but grants nothing -- the safe half-state."""
        body = (
            env["client"]("admin")
            .post("/api/admin/users", json={"email": "ap@acme.test"})
            .json()
        )
        assert body["claims_written"] is False
        assert "cannot sign in yet" in body["warning"]

    def test_a_uid_writes_claims(self, env):
        body = (
            env["client"]("admin")
            .post(
                "/api/admin/users",
                json={
                    "email": "ap@acme.test",
                    "role": "approver",
                    "uid": "fb-1",
                },
            )
            .json()
        )
        assert body["claims_written"] is True
        assert env["claims"].written[0][1]["role"] == "approver"

    def test_claims_carry_the_tenant_id_not_the_slug(self, env):
        env["client"]("admin").post(
            "/api/admin/users", json={"email": "ap@acme.test", "uid": "fb-2"}
        )
        assert env["claims"].written[0][1]["tenant_id"] == env["acme"]["id"]

    @pytest.mark.parametrize("role", ["viewer", "clerk", "approver"])
    def test_only_an_admin_may_manage_users(self, env, role):
        assert env["client"](role).get("/api/admin/users").status_code == 403
        assert (
            env["client"](role)
            .post("/api/admin/users", json={"email": "x@y.test"})
            .status_code
            == 403
        )

    def test_an_unknown_role_is_refused(self, env):
        """A typo would create a user who logs in and can do nothing."""
        response = env["client"]("admin").post(
            "/api/admin/users",
            json={"email": "ap@acme.test", "role": "supervisor"},
        )
        assert response.status_code == 400
        assert "Unknown role" in response.json()["detail"]

    def test_an_invalid_email_is_refused(self, env):
        response = env["client"]("admin").post(
            "/api/admin/users", json={"email": "not-an-email"}
        )
        assert response.status_code == 400

    def test_a_duplicate_email_is_refused(self, env):
        client = env["client"]("admin")
        client.post("/api/admin/users", json={"email": "ap@acme.test"})
        response = client.post(
            "/api/admin/users", json={"email": "ap@acme.test"}
        )
        assert response.status_code == 400

    def test_the_same_email_may_exist_in_two_tenants(self, env):
        """A consultant working for two customers is not a conflict."""
        env["client"]("admin").post(
            "/api/admin/users", json={"email": "consultant@x.test"}
        )
        other = env["client"]("admin", tenant=env["globex"]["id"]).post(
            "/api/admin/users", json={"email": "consultant@x.test"}
        )
        assert other.status_code == 200

    def test_creation_is_audited(self, env, tmp_path):

        env["client"]("admin").post(
            "/api/admin/users", json={"email": "ap@acme.test"}
        )
        last = json.loads(
            (tmp_path / "audit.jsonl").read_text().strip().splitlines()[-1]
        )
        assert last["event_type"] == "user_created"


class TestClaims:
    def test_links_an_account_to_an_existing_user(self, env):
        client = env["client"]("admin")
        client.post(
            "/api/admin/users", json={"email": "ap@acme.test", "role": "admin"}
        )
        body = client.post(
            "/api/admin/users/claims",
            json={"email": "ap@acme.test", "uid": "fb-9"},
        ).json()
        assert body["claims"]["role"] == "admin"

    def test_the_role_comes_from_the_stored_user(self, env):
        """Taking it from the body would let a caller grant a role never assigned."""
        client = env["client"]("admin")
        client.post(
            "/api/admin/users", json={"email": "ap@acme.test", "role": "viewer"}
        )
        body = client.post(
            "/api/admin/users/claims",
            json={"email": "ap@acme.test", "uid": "fb-9", "role": "admin"},
        ).json()
        assert body["claims"]["role"] == "viewer"

    def test_dry_run_is_reported_honestly(self, env):
        """Reporting success while granting nothing is the worst outcome."""
        client = env["client"]("admin")
        client.post("/api/admin/users", json={"email": "ap@acme.test"})
        body = client.post(
            "/api/admin/users/claims",
            json={"email": "ap@acme.test", "uid": "fb-9"},
        ).json()
        assert body["applied"] is False
        assert "Dry run" in body["note"]

    def test_an_unknown_user_is_404(self, env):
        response = env["client"]("admin").post(
            "/api/admin/users/claims",
            json={"email": "ghost@acme.test", "uid": "fb-9"},
        )
        assert response.status_code == 404

    def test_a_clerk_cannot_grant_access(self, env):
        response = env["client"]("clerk").post(
            "/api/admin/users/claims",
            json={"email": "ap@acme.test", "uid": "fb-9"},
        )
        assert response.status_code == 403


class TestClaimsWriterResolution:
    def test_dry_run_by_default(self, monkeypatch):
        """Granting real access should not happen because a form was submitted."""
        monkeypatch.delenv("APPLY_IDENTITY_CLAIMS", raising=False)
        monkeypatch.setattr(service, "_claims_writer", None)
        assert isinstance(service.get_claims_writer(), DryRunClaims)
        assert service.claims_are_live() is False

    def test_unconfigured_project_falls_back_rather_than_failing(
        self, monkeypatch
    ):
        """An admin should still be able to create records and see access was
        not granted, instead of getting an unexplained error."""
        monkeypatch.setenv("APPLY_IDENTITY_CLAIMS", "1")
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.setattr(service, "_claims_writer", None)
        assert isinstance(service.get_claims_writer(), DryRunClaims)


class TestNoDatabaseConfigured:
    """The state a fresh local install is actually in.

    With neither DATABASE_URL nor CLOUD_SQL_INSTANCE set the app falls back to
    in-memory SQLite, which has no schema. These endpoints raised a bare 500,
    so the admin screen said "Internal Server Error" and gave no hint that the
    cause was configuration rather than a fault.
    """

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        monkeypatch.setattr(auth, "_verifier", FakeVerifier())
        monkeypatch.setattr(
            service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
        )
        return TestClient(app, headers={"Authorization": "Bearer admin@t-1"})

    def test_reading_the_tenant_explains_itself(self, client):
        response = client.get("/api/admin/tenants")
        assert response.status_code == 503
        assert "No database is configured" in response.json()["detail"]

    def test_listing_users_explains_itself(self, client):
        response = client.get("/api/admin/users")
        assert response.status_code == 503
        assert "DATABASE_URL" in response.json()["detail"], (
            "the message must name the setting that fixes it"
        )
