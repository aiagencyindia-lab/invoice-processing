"""Self-service sign-up.

The endpoint hands an account its first organisation, which is the one power
the rest of this service deliberately withholds from callers -- so the tests
weigh most heavily on what it refuses.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from invoice_processing import sso
from invoice_processing.adapters import JsonlAuditSink
from invoice_processing.admin.provision import DryRunClaims
from invoice_processing.api import auth, service, signup
from invoice_processing.api.service import app
from invoice_processing.db.models import Base, Tenant
from invoice_processing.db.repositories import PgSsoDomainRepository
from invoice_processing.db.session import create_all, get_engine, tenant_session
from tests.test_sso import FakeDirectory


class FakeVerifier:
    """Token format: `<email>|<uid>|<tenant or blank>|<verified>`.

    `verified` is optional: blank means True, `no` False, `absent` omits the
    claim, `str` sends the string "true".
    """

    def verify(self, token):
        email, uid, tenant, verified = [*token.split("|"), "", "", ""][:4]
        claims = {"sub": uid, "user_id": uid, "email": email}
        if verified == "":
            claims["email_verified"] = True
        elif verified == "no":
            claims["email_verified"] = False
        elif verified == "str":
            claims["email_verified"] = "true"
        if tenant:
            claims["tenant_id"] = tenant
            claims["role"] = "admin"
        return claims


@pytest.fixture
def env(scratch_db, tmp_path, monkeypatch):
    url = scratch_db("signup.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
    monkeypatch.setenv("ALLOW_SELF_SIGNUP", "1")
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    engine = get_engine(url)
    Base.metadata.drop_all(engine)
    create_all(url)

    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.setattr(
        service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    writer = DryRunClaims()
    monkeypatch.setattr(service, "get_claims_writer", lambda: writer)
    return {"url": url, "tmp": tmp_path}


def client(token: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


class TestItRefuses:
    def test_an_unauthenticated_caller(self, env):
        response = TestClient(app).post(
            "/api/signup", json={"organisation": "Acme"}
        )
        assert response.status_code in (401, 403)

    def test_when_the_deployment_has_not_opted_in(self, env, monkeypatch):
        """Off by default: it hands out the power the rest of the service
        withholds, so a deployment has to say yes on purpose."""
        monkeypatch.delenv("ALLOW_SELF_SIGNUP", raising=False)
        response = client("a@b.test|uid-1").post(
            "/api/signup", json={"organisation": "Acme"}
        )
        assert response.status_code == 403
        assert "not open" in response.json()["detail"]

    def test_an_account_that_already_has_an_organisation(self, env):
        """A second organisation would strand every invoice reviewed in the
        first one."""
        response = client("a@b.test|uid-1|tenant-xyz").post(
            "/api/signup", json={"organisation": "Another"}
        )
        assert response.status_code == 409

    def test_an_empty_organisation_name(self, env):
        response = client("a@b.test|uid-1").post(
            "/api/signup", json={"organisation": " "}
        )
        assert response.status_code == 400


def bind(url, domain, provider="google"):
    """Bind `domain` to another organisation, as its admin would."""
    with tenant_session("t-held", url) as session:
        session.add(Tenant(id="t-held", slug="held", name="Held Co"))
    PgSsoDomainRepository(url).add(
        "t-held",
        domain=domain,
        provider=provider,
        default_role="viewer",
        created_by=f"admin@{domain}",
    )


def slugs(url):
    with Session(get_engine(url)) as session:
        return set(session.scalars(select(Tenant.slug)))


def audited(env):
    path = env["tmp"] / "audit.jsonl"
    return path.exists() and "tenant_self_provisioned" in path.read_text()


class TestItRefusesUnprovenIdentity:
    @pytest.mark.parametrize("verified", ["no", "absent", "str"])
    def test_an_unverified_address(self, env, verified):
        """Anyone who can type an address could otherwise found an
        organisation in its name."""
        response = client(f"a@b.test|uid-1||{verified}").post(
            "/api/signup", json={"organisation": "Acme"}
        )
        assert response.status_code == 403
        assert response.json()["detail"] == (
            "Verify your email address before creating an organisation."
        )
        assert "acme" not in slugs(env["url"])

    @pytest.mark.parametrize("provider", ["google", "microsoft"])
    def test_an_address_on_a_bound_domain(self, env, provider):
        """The domain belongs to an organisation already; a rival one on it
        would split that company's invoices across two tenants."""
        bind(env["url"], "acme.test", provider)
        response = client("ann@acme.test|uid-1").post(
            "/api/signup", json={"organisation": "Acme"}
        )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert "Sign in with Google" in detail
        assert "Held Co" not in detail
        assert slugs(env["url"]) == {"held"}
        assert not audited(env)

    def test_the_bound_domain_check_ignores_case(self, env):
        bind(env["url"], "acme.test")
        response = client("Ann@ACME.test|uid-1").post(
            "/api/signup", json={"organisation": "Acme"}
        )
        assert response.status_code == 403

    def test_an_unreachable_directory_refuses(self, env, monkeypatch):
        """No lookup, no signup: the check fails closed like sign-in."""
        monkeypatch.setattr(
            auth,
            "_sso_services",
            lambda: sso.SsoServices(FakeDirectory(fail=True), None),
        )
        response = client("ann@acme.test|uid-1").post(
            "/api/signup", json={"organisation": "Acme"}
        )
        assert response.status_code == 503
        assert "acme" not in slugs(env["url"])

    def test_an_unbound_domain_still_signs_up(self, env):
        bind(env["url"], "other.test")
        response = client("ann@acme.test|uid-1").post(
            "/api/signup", json={"organisation": "Acme"}
        )
        assert response.status_code == 200
        assert "acme" in slugs(env["url"])


class TestItProvisions:
    def test_creates_a_tenant_and_makes_the_caller_its_admin(self, env):
        body = (
            client("owner@acme.test|uid-1")
            .post("/api/signup", json={"organisation": "Acme Pty Ltd"})
            .json()
        )
        assert body["tenant"]["name"] == "Acme Pty Ltd"
        assert body["role"] == "admin"

    def test_the_slug_is_derived_not_accepted(self, env):
        """The slug becomes an inbound-email local part and a storage prefix.
        Letting a caller choose it is a way to collide with another tenant."""
        body = (
            client("owner@acme.test|uid-1")
            .post("/api/signup", json={"organisation": "Acme Pty Ltd"})
            .json()
        )
        assert body["tenant"]["slug"] == "acme-pty-ltd"

    def test_a_second_organisation_of_the_same_name_gets_its_own_slug(
        self, env
    ):
        first = (
            client("a@x.test|uid-1")
            .post("/api/signup", json={"organisation": "Acme"})
            .json()
        )
        second = (
            client("b@y.test|uid-2")
            .post("/api/signup", json={"organisation": "Acme"})
            .json()
        )
        assert first["tenant"]["slug"] != second["tenant"]["slug"]

    def test_the_caller_cannot_name_their_own_tenant_id_or_role(self, env):
        """Whatever the body says, identity comes from the verified token."""
        body = (
            client("owner@acme.test|uid-1")
            .post(
                "/api/signup",
                json={
                    "organisation": "Acme",
                    "tenant_id": "someone-else",
                    "role": "admin",
                    "email": "victim@elsewhere.test",
                },
            )
            .json()
        )
        assert body["tenant"]["id"] != "someone-else"


class TestSlugs:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Acme Pty Ltd", "acme-pty-ltd"),
            ("  Spaces  Everywhere  ", "spaces-everywhere"),
            ("Ünïcode & Symbols!!", "unicode-symbols"),
            # An accented letter must fold to its base, not act as a separator:
            # otherwise "Café Ltd" is caf-ltd.
            ("Café Ltd", "cafe-ltd"),
            # Nothing usable left: a name rather than an empty prefix.
            ("日本語", "org"),
            ("---", "org"),
        ],
    )
    def test_it_sanitises(self, name, expected):
        assert signup.slug_for(name, set()) == expected

    def test_it_avoids_collisions(self):
        assert signup.slug_for("Acme", {"acme"}) == "acme-2"
        assert signup.slug_for("Acme", {"acme", "acme-2"}) == "acme-3"
