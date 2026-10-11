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


def _events(tmp_path):
    return [
        json.loads(x)
        for x in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]


class TestSsoDomains:
    def test_add_list_remove(self, env, tmp_path):
        client = env["client"]("admin")
        r = client.post(
            "/api/admin/sso-domains",
            json={
                "domain": " Acme.COM ",
                "provider": "google",
                "default_role": "clerk",
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["domain"] == "acme.com"
        listed = client.get("/api/admin/sso-domains").json()["domains"]
        assert [d["domain"] for d in listed] == ["acme.com"]
        assert (
            client.delete(
                f"/api/admin/sso-domains/{listed[0]['id']}"
            ).status_code
            == 204
        )
        assert client.get("/api/admin/sso-domains").json()["domains"] == []
        names = [e["event_type"] for e in _events(tmp_path)]
        assert "sso_domain_added" in names and "sso_domain_removed" in names

    @pytest.mark.parametrize(
        "domain", ["gmail.com", "outlook.com", "not a domain", ""]
    )
    def test_public_and_malformed_domains_are_refused(self, env, domain):
        r = env["client"]("admin").post(
            "/api/admin/sso-domains",
            json={
                "domain": domain,
                "provider": "google",
                "default_role": "viewer",
            },
        )
        assert r.status_code == 422

    def test_bad_provider_or_role_is_refused(self, env):
        client = env["client"]("admin")
        assert (
            client.post(
                "/api/admin/sso-domains",
                json={
                    "domain": "acme.com",
                    "provider": "okta",
                    "default_role": "viewer",
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/admin/sso-domains",
                json={
                    "domain": "acme.com",
                    "provider": "google",
                    "default_role": "admin",
                },
            ).status_code
            == 422
        )

    def test_a_domain_held_by_another_tenant_is_refused_without_naming_it(
        self, env
    ):
        acme = env["client"]("admin")
        globex = env["client"]("admin", env["globex"]["id"])
        assert (
            acme.post(
                "/api/admin/sso-domains",
                json={
                    "domain": "acme.com",
                    "provider": "google",
                    "default_role": "viewer",
                },
            ).status_code
            == 201
        )
        r = globex.post(
            "/api/admin/sso-domains",
            json={
                "domain": "acme.com",
                "provider": "google",
                "default_role": "viewer",
            },
        )
        assert r.status_code == 409
        assert "another organisation" in r.json()[
            "detail"
        ] and "acme" not in r.json()["detail"].lower().replace("acme.com", "")

    def test_only_admins_manage_domains(self, env):
        assert (
            env["client"]("approver").get("/api/admin/sso-domains").status_code
            == 403
        )

    def test_removing_a_domain_keeps_existing_users(self, env):
        client = env["client"]("admin")
        d = client.post(
            "/api/admin/sso-domains",
            json={
                "domain": "acme.com",
                "provider": "google",
                "default_role": "viewer",
            },
        ).json()
        client.post(
            "/api/admin/users", json={"email": "pat@acme.com", "role": "clerk"}
        )
        assert (
            client.delete(f"/api/admin/sso-domains/{d['id']}").status_code
            == 204
        )
        emails = [
            u["email"] for u in client.get("/api/admin/users").json()["users"]
        ]
        assert "pat@acme.com" in emails


class TestRoleAndActive:
    def _user(self, env, email="pat@acme.com", role="clerk"):
        client = env["client"]("admin")
        client.post(
            "/api/admin/users",
            json={"email": email, "role": role, "uid": "fb-pat"},
        )
        return next(
            u
            for u in client.get("/api/admin/users").json()["users"]
            if u["email"] == email
        )

    def test_change_role_rewrites_claims_and_audits(self, env, tmp_path):
        pat = self._user(env)
        client = env["client"]("admin")
        r = client.patch(
            f"/api/admin/users/{pat['id']}", json={"role": "approver"}
        )
        assert r.status_code == 200 and r.json()["role"] == "approver"
        assert (
            "fb-pat",
            {"tenant_id": env["acme"]["id"], "role": "approver"},
        ) in env["claims"].written
        assert "user_role_changed" in [
            e["event_type"] for e in _events(tmp_path)
        ]

    def test_deactivate_and_reactivate(self, env, tmp_path):
        pat = self._user(env)
        client = env["client"]("admin")
        assert (
            client.patch(
                f"/api/admin/users/{pat['id']}", json={"is_active": False}
            ).json()["is_active"]
            is False
        )
        listed = next(
            u
            for u in client.get("/api/admin/users").json()["users"]
            if u["id"] == pat["id"]
        )
        assert listed["is_active"] is False and listed["uid"] == "fb-pat"
        assert (
            client.patch(
                f"/api/admin/users/{pat['id']}", json={"is_active": True}
            ).json()["is_active"]
            is True
        )
        names = [e["event_type"] for e in _events(tmp_path)]
        assert "user_deactivated" in names and "user_reactivated" in names

    def test_an_admin_cannot_demote_or_deactivate_themself(self, env):
        client = env["client"]("admin")
        # The admin token's email is admin@x.test; give it a row.
        client.post(
            "/api/admin/users", json={"email": "admin@x.test", "role": "admin"}
        )
        me = next(
            u
            for u in client.get("/api/admin/users").json()["users"]
            if u["email"] == "admin@x.test"
        )
        assert (
            client.patch(
                f"/api/admin/users/{me['id']}", json={"role": "clerk"}
            ).status_code
            == 409
        )
        assert (
            client.patch(
                f"/api/admin/users/{me['id']}", json={"is_active": False}
            ).status_code
            == 409
        )

    def test_an_unknown_role_is_refused(self, env):
        pat = self._user(env)
        assert (
            env["client"]("admin")
            .patch(f"/api/admin/users/{pat['id']}", json={"role": "boss"})
            .status_code
            == 422
        )

    def test_users_list_carries_the_new_fields(self, env):
        pat = self._user(env)
        assert (
            pat["uid"] == "fb-pat"
            and pat["is_active"] is True
            and "last_login_at" in pat
        )


class TestPartialFailures:
    def test_a_failing_claims_write_still_reports_the_change(
        self, env, tmp_path, monkeypatch
    ):
        client = env["client"]("admin")
        client.post(
            "/api/admin/users",
            json={"email": "pat@acme.com", "role": "clerk", "uid": "fb-pat"},
        )
        pat = next(
            u
            for u in client.get("/api/admin/users").json()["users"]
            if u["email"] == "pat@acme.com"
        )

        def boom(uid, claims):
            raise RuntimeError("identity platform down")

        monkeypatch.setattr(env["claims"], "set_claims", boom)
        r = client.patch(
            f"/api/admin/users/{pat['id']}", json={"role": "approver"}
        )
        assert r.status_code == 200, r.text
        assert (
            r.json()["role"] == "approver"
            and r.json()["claims_applied"] is False
        )
        assert [e["event_type"] for e in _events(tmp_path)].count(
            "user_role_changed"
        ) == 1

    def test_claims_applied_is_true_when_written(self, env):
        client = env["client"]("admin")
        client.post(
            "/api/admin/users",
            json={"email": "pat@acme.com", "role": "clerk", "uid": "fb-pat"},
        )
        pat = next(
            u
            for u in client.get("/api/admin/users").json()["users"]
            if u["email"] == "pat@acme.com"
        )
        r = client.patch(
            f"/api/admin/users/{pat['id']}", json={"role": "approver"}
        )
        assert r.json()["claims_applied"] is True

    def test_set_claims_stores_the_uid(self, env):
        client = env["client"]("admin")
        client.post(
            "/api/admin/users", json={"email": "pat@acme.com", "role": "clerk"}
        )
        assert (
            client.post(
                "/api/admin/users/claims",
                json={"email": "pat@acme.com", "uid": "fb-pat"},
            ).status_code
            == 200
        )
        pat = next(
            u
            for u in client.get("/api/admin/users").json()["users"]
            if u["email"] == "pat@acme.com"
        )
        assert pat["uid"] == "fb-pat"

    @pytest.mark.parametrize("who", ["pat@acme.com", "admin@x.test"])
    def test_a_refused_combined_patch_changes_nothing(self, env, tmp_path, who):
        client = env["client"]("admin")
        client.post(
            "/api/admin/users", json={"email": "admin@x.test", "role": "admin"}
        )
        client.post(
            "/api/admin/users", json={"email": "pat@acme.com", "role": "clerk"}
        )
        target = next(
            u
            for u in client.get("/api/admin/users").json()["users"]
            if u["email"] == who
        )
        body = {
            "manager_email": "admin@x.test",
            "role": "boss" if who == "pat@acme.com" else "clerk",
        }
        r = client.patch(f"/api/admin/users/{target['id']}", json=body)
        assert r.status_code == (422 if who == "pat@acme.com" else 409)
        after = next(
            u
            for u in client.get("/api/admin/users").json()["users"]
            if u["id"] == target["id"]
        )
        assert after["manager_email"] is None
        assert "user_updated" not in [
            e["event_type"] for e in _events(tmp_path)
        ]


def test_re_adding_your_own_domain_says_so(env):
    client = env["client"]("admin")
    body = {
        "domain": "acme.com",
        "provider": "google",
        "default_role": "viewer",
    }
    assert client.post("/api/admin/sso-domains", json=body).status_code == 201
    r = client.post("/api/admin/sso-domains", json=body)
    assert r.status_code == 409 and "your organisation" in r.json()["detail"]
