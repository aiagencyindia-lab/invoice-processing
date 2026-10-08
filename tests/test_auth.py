"""Authentication and tenant scoping.

The property under test is one sentence: **the tenant comes from the signed
token and from nowhere else**. Everything here is an attempt to break that.

The tests that matter most are the ones asserting a *refusal*: a token with no
tenant claim, a caller trying to name their own tenant, a dev bypass in a
production environment. A leak in this module is not a bug report, it is a
disclosure, so the failure modes get more coverage than the happy path.

No test touches the network or a real Google key; the verifier is a Protocol and
these inject a fake.
"""

import dataclasses
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from invoice_processing.api import auth
from invoice_processing.api.auth import (
    AuthError,
    Principal,
    current_principal,
    dev_principal,
    principal_from_claims,
    require_role,
)


class FakeVerifier:
    """Returns whatever claims it was seeded with, or rejects."""

    def __init__(self, claims_by_token: dict):
        self.claims_by_token = claims_by_token

    def verify(self, token: str) -> dict:
        if token not in self.claims_by_token:
            raise AuthError("Invalid token: unknown")
        return self.claims_by_token[token]


ACME = {
    "sub": "u-1",
    "email": "clerk@acme.test",
    "email_verified": True,
    "tenant_id": "tenant-acme",
    "role": "clerk",
}
GLOBEX = {
    "sub": "u-2",
    "email": "clerk@globex.test",
    "email_verified": True,
    "tenant_id": "tenant-globex",
    "role": "clerk",
}


@pytest.fixture(autouse=True)
def no_dev_bypass(monkeypatch):
    """Every test runs with the bypass off unless it turns it on itself."""
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.delenv("ENV", raising=False)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(
        auth,
        "_verifier",
        FakeVerifier({"acme-token": ACME, "globex-token": GLOBEX}),
    )
    application = FastAPI()

    @application.get("/whoami")
    def whoami(principal: Annotated[Principal, Depends(current_principal)]):
        return {"email": principal.email, "tenant_id": principal.tenant_id}

    @application.post("/approve")
    def approve(
        principal: Annotated[Principal, Depends(require_role("approver"))],
    ):
        return {"ok": True}

    return TestClient(application)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# The core rule
# ---------------------------------------------------------------------------


class TestTenantComesFromTheToken:
    def test_tenant_is_read_from_the_claim(self, app):
        body = app.get("/whoami", headers=bearer("acme-token")).json()
        assert body["tenant_id"] == "tenant-acme"

    def test_a_token_with_no_tenant_claim_is_refused(self):
        """Defaulting here would let a misprovisioned user read a real tenant."""
        with pytest.raises(AuthError) as exc:
            principal_from_claims({"sub": "x", "email": "a@b.c"})
        assert exc.value.status_code == 403
        assert "tenant_id" in exc.value.detail

    def test_caller_cannot_choose_a_tenant_via_header(self, app):
        """A header is the caller's assertion; the token is Google's."""
        headers = {**bearer("acme-token"), "X-Tenant-Id": "tenant-globex"}
        assert (
            app.get("/whoami", headers=headers).json()["tenant_id"]
            == "tenant-acme"
        )

    def test_caller_cannot_choose_a_tenant_via_query(self, app):
        body = app.get(
            "/whoami?tenant_id=tenant-globex", headers=bearer("acme-token")
        )
        assert body.json()["tenant_id"] == "tenant-acme"

    def test_two_tokens_resolve_to_two_tenants(self, app):
        acme = app.get("/whoami", headers=bearer("acme-token")).json()
        globex = app.get("/whoami", headers=bearer("globex-token")).json()
        assert acme["tenant_id"] != globex["tenant_id"]

    def test_unverified_email_is_refused(self):
        """Our audit trail is keyed on email; an unverified one is a claim."""
        with pytest.raises(AuthError):
            principal_from_claims({**ACME, "email_verified": False})

    def test_missing_email_is_refused(self):
        claims = {k: v for k, v in ACME.items() if k != "email"}
        with pytest.raises(AuthError):
            principal_from_claims(claims)


class TestRejection:
    def test_no_header_is_401(self, app):
        assert app.get("/whoami").status_code == 401

    def test_garbage_token_is_401(self, app):
        assert app.get("/whoami", headers=bearer("nope")).status_code == 401

    @pytest.mark.parametrize(
        "header", ["", "Bearer", "Bearer ", "Basic acme-token", "acme-token"]
    )
    def test_malformed_authorization_headers_are_401(self, app, header):
        response = app.get("/whoami", headers={"Authorization": header})
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


class TestRoles:
    def test_insufficient_role_is_403_not_401(self, app):
        """The caller is authenticated; a 401 would send them to re-login."""
        response = app.post("/approve", headers=bearer("acme-token"))
        assert response.status_code == 403

    def test_sufficient_role_passes(self, app, monkeypatch):
        monkeypatch.setattr(
            auth,
            "_verifier",
            FakeVerifier({"boss": {**ACME, "role": "approver"}}),
        )
        assert app.post("/approve", headers=bearer("boss")).status_code == 200

    def test_unknown_role_grants_nothing(self):
        """A typo in a claim must lock a user out, never promote them."""
        principal = principal_from_claims({**ACME, "role": "supervisor"})
        assert principal.rank == -1
        assert not principal.can("viewer")

    def test_role_defaults_to_the_weakest(self):
        claims = {k: v for k, v in ACME.items() if k != "role"}
        assert principal_from_claims(claims).role == "viewer"

    @pytest.mark.parametrize(
        ("role", "required", "expected"),
        [
            ("admin", "approver", True),
            ("approver", "clerk", True),
            ("clerk", "approver", False),
            ("viewer", "clerk", False),
        ],
    )
    def test_rank_ordering(self, role, required, expected):
        assert Principal("u", "e@x.y", "t", role).can(required) is expected


# ---------------------------------------------------------------------------
# The dev bypass, which is the one thing that could disable all of the above
# ---------------------------------------------------------------------------


class TestDevBypass:
    def test_off_by_default(self):
        assert dev_principal() is None

    @pytest.mark.parametrize(
        "env", ["production", "prod", "staging", "PRODUCTION"]
    )
    def test_refuses_to_activate_in_production(self, monkeypatch, env):
        """If this setting ever escapes, it must fail closed."""
        monkeypatch.setenv("AUTH_DISABLED", "1")
        monkeypatch.setenv("ENV", env)
        assert dev_principal() is None

    def test_activates_locally(self, monkeypatch):
        monkeypatch.setenv("AUTH_DISABLED", "1")
        monkeypatch.setenv("ENV", "local")
        principal = dev_principal()
        assert principal is not None and principal.tenant_id == "default"

    def test_requires_an_explicit_truthy_value(self, monkeypatch):
        monkeypatch.setenv("AUTH_DISABLED", "0")
        assert dev_principal() is None

    def test_a_real_token_still_wins_over_the_bypass(self, app, monkeypatch):
        """Otherwise local testing would never exercise the real identity."""
        monkeypatch.setenv("AUTH_DISABLED", "1")
        body = app.get("/whoami", headers=bearer("globex-token")).json()
        assert body["email"] == "clerk@globex.test"


class TestVerifierConfiguration:
    def test_missing_project_id_fails_loudly(self, monkeypatch):
        """An unset audience would accept tokens from any Google project."""
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        with pytest.raises(ValueError, match="project id"):
            auth.IdentityPlatformVerifier()


class TestRunContext:
    def test_context_carries_tenant_and_actor(self):
        ctx = Principal("u", "a@b.c", "tenant-acme", "clerk").context(
            "case_005"
        )
        assert (ctx.tenant_id, ctx.case_id, ctx.actor) == (
            "tenant-acme",
            "case_005",
            "a@b.c",
        )

    def test_principal_is_immutable(self):
        """A handler that could rewrite tenant_id would defeat the point."""
        with pytest.raises(dataclasses.FrozenInstanceError):
            Principal("u", "a@b.c", "t").tenant_id = "other"


class TestDevIdentityHeader:
    """In local dev mode a browser test needs to be more than one person: the
    two-person bank-change flow cannot be exercised by a single fixed admin.
    The header picks who the dev principal is. It grants nothing the mode does
    not already hand out, and outside the mode it is never read."""

    HEADER = "X-Dev-Principal"

    @pytest.fixture
    def local(self, monkeypatch):
        monkeypatch.setenv("AUTH_DISABLED", "1")
        monkeypatch.setenv("ENV", "local")

    def test_the_header_sets_email_and_role(self, app, local):
        as_clerk = {self.HEADER: "alice@e2e.test:clerk"}
        assert app.get("/whoami", headers=as_clerk).json()["email"] == (
            "alice@e2e.test"
        )
        assert app.post("/approve", headers=as_clerk).status_code == 403
        as_approver = {self.HEADER: "bob@e2e.test:approver"}
        assert app.post("/approve", headers=as_approver).status_code == 200

    def test_without_the_header_the_dev_admin_is_unchanged(self, app, local):
        assert app.get("/whoami").json()["email"] == "dev@localhost"
        assert app.post("/approve").status_code == 200

    @pytest.mark.parametrize(
        "value", ["alice@e2e.test:owner", "alice@e2e.test", "alice:clerk", ""]
    )
    def test_a_malformed_header_is_refused_not_ignored(self, app, local, value):
        """Silently falling back to admin would let a typo in a test pass as
        the wrong person."""
        response = app.get("/whoami", headers={self.HEADER: value or " "})
        assert response.status_code == 400

    def test_ignored_when_the_bypass_is_off(self, app, monkeypatch):
        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        response = app.get(
            "/whoami", headers={self.HEADER: "alice@e2e.test:admin"}
        )
        assert response.status_code == 401

    def test_ignored_outside_a_local_environment(self, app, monkeypatch):
        monkeypatch.setenv("AUTH_DISABLED", "1")
        monkeypatch.setenv("ENV", "production")
        response = app.get(
            "/whoami", headers={self.HEADER: "alice@e2e.test:admin"}
        )
        assert response.status_code == 401
