"""Self-service sign-up.

The endpoint hands an account its first organisation, which is the one power
the rest of this service deliberately withholds from callers -- so the tests
weigh most heavily on what it refuses.
"""

import pytest
from fastapi.testclient import TestClient
from fresh_schema import fresh_schema
from sqlalchemy import select
from sqlalchemy.orm import Session

from invoice_processing import sso
from invoice_processing.adapters import JsonlAuditSink
from invoice_processing.admin.provision import DryRunClaims, ProvisioningError
from invoice_processing.api import auth, service, signup
from invoice_processing.api.service import app
from invoice_processing.db.models import AuditEvent, Tenant, User
from invoice_processing.db.repositories import PgSsoDomainRepository
from invoice_processing.db.session import get_engine, tenant_session
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
    fresh_schema(url)

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
    """Written in the sign-up's own transaction, so it is in the database."""
    with Session(get_engine(env["url"])) as session:
        return bool(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "tenant_self_provisioned"
                )
            ).first()
        )


def users(url):
    with Session(get_engine(url)) as session:
        return session.scalars(select(User)).all()


class RefusingClaims:
    """Identity Platform refusing the claim update, as it did live."""

    def __init__(self, failures=1):
        self.failures, self.written = failures, []

    def set_claims(self, uid, claims):
        if self.failures:
            self.failures -= 1
            raise ProvisioningError(
                f"Identity Platform refused the claim update for {uid}: 403 "
                "accessNotConfigured"
            )
        self.written.append((uid, claims))


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


def test_sign_up_logs_the_tenant_not_the_person(env, caplog):
    """Token format is `<email>|<uid>|<tenant>|<verified>`; a blank verified
    means True (see this file's token helper)."""
    with caplog.at_level("INFO", logger="invoice_processing.api.signup"):
        response = client("a@b.test|uid-1").post(
            "/api/signup", json={"organisation": "Acme"}
        )
    assert response.status_code in (200, 201), response.text
    assert "uid-1" in caplog.text
    assert "@" not in caplog.text


class TestAllOrNothing:
    """Organisation, admin row, audit row and claims land together or not at
    all. Live, a refused claim update left an organisation with no record
    and a person with no access, and a retry made a second one."""

    def test_a_claims_failure_leaves_nothing_behind(self, env, monkeypatch):
        monkeypatch.setattr(service, "get_claims_writer", RefusingClaims)
        response = client("owner@acme.test|uid-1").post(
            "/api/signup", json={"organisation": "Acme"}
        )
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert detail == signup.CLAIMS_UNAVAILABLE
        assert "Identity Platform" not in detail and "403" not in detail
        assert slugs(env["url"]) == set()
        assert users(env["url"]) == []
        assert not audited(env)

    def test_a_retry_after_a_claims_failure_makes_one_organisation(
        self, env, monkeypatch
    ):
        writer = RefusingClaims(failures=1)
        monkeypatch.setattr(service, "get_claims_writer", lambda: writer)
        caller = client("owner@acme.test|uid-1")
        assert (
            caller.post("/api/signup", json={"organisation": "Acme"})
        ).status_code == 503
        retried = caller.post("/api/signup", json={"organisation": "Acme"})
        assert retried.status_code in (200, 201), retried.text
        assert slugs(env["url"]) == {"acme"}
        tenant_id = retried.json()["tenant"]["id"]
        assert writer.written == [
            ("uid-1", {"tenant_id": tenant_id, "role": "admin"})
        ]

    def test_a_commit_that_fails_after_the_claims_clears_them(
        self, env, monkeypatch
    ):
        """The other order of failure: the token must not be left pointing
        at an organisation that was never stored."""
        from sqlalchemy.orm import Session as OrmSession  # noqa: PLC0415

        writer = RefusingClaims(failures=0)
        monkeypatch.setattr(service, "get_claims_writer", lambda: writer)
        real_commit = OrmSession.commit

        def commit(session):
            if writer.written:
                raise RuntimeError("connection lost at commit")
            return real_commit(session)

        monkeypatch.setattr(OrmSession, "commit", commit)
        response = TestClient(app, raise_server_exceptions=False).post(
            "/api/signup",
            json={"organisation": "Acme"},
            headers={"Authorization": "Bearer owner@acme.test|uid-1"},
        )
        monkeypatch.setattr(OrmSession, "commit", real_commit)
        assert response.status_code in (500, 503)
        assert writer.written[-1] == ("uid-1", {})
        assert slugs(env["url"]) == set()

    def test_the_audit_row_commits_with_the_organisation(self, env):
        response = client("owner@acme.test|uid-1").post(
            "/api/signup", json={"organisation": "Acme"}
        )
        tenant_id = response.json()["tenant"]["id"]
        with Session(get_engine(env["url"])) as session:
            row = session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "tenant_self_provisioned"
                )
            ).one()
        assert row.tenant_id == tenant_id
        assert row.after["slug"] == "acme"


@pytest.fixture(autouse=True)
def _fresh_lookup_limit():
    """The lookup's limiter is process state; each test starts empty."""
    signup.LOOKUP_LIMIT.clear()
    yield
    signup.LOOKUP_LIMIT.clear()


class TestLookupLimit:
    """A public, unauthenticated lookup: per address and overall caps."""

    def _ask(self, ip):
        return TestClient(app).post(
            "/api/sso/microsoft",
            json={"email": "pat@unbound.com"},
            headers={"x-forwarded-for": ip},
        )

    def test_one_caller_is_limited_per_minute(self, env):
        for _ in range(signup.LOOKUPS_PER_IP):
            assert self._ask("203.0.113.7").status_code == 404
        r = self._ask("203.0.113.7")
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) >= 1
        assert self._ask("198.51.100.9").status_code == 404

    def test_the_window_moves_on(self, env, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(signup.time, "monotonic", lambda: now[0])
        for _ in range(signup.LOOKUPS_PER_IP):
            self._ask("203.0.113.7")
        assert self._ask("203.0.113.7").status_code == 429
        now[0] += 61
        assert self._ask("203.0.113.7").status_code == 404

    def test_many_addresses_still_meet_an_overall_cap(self, env, monkeypatch):
        """Rotating the forwarded address does not get past the total."""
        monkeypatch.setattr(signup, "LOOKUPS_OVERALL", 5)
        for i in range(5):
            assert self._ask(f"203.0.113.{i}").status_code == 404
        assert self._ask("203.0.113.99").status_code == 429


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
        # POST, so the address never sits in a URL that proxies log.
        return TestClient(app).post("/api/sso/microsoft", json={"email": email})

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
            ("pat@" + "a" * 330 + ".com", "that address"),
        ],
    )
    def test_anything_else_is_not_set_up(self, env, email, domain):
        self._bind(env)
        response = self._get(email)
        assert response.status_code == 404
        assert response.json()["detail"] == signup.MICROSOFT_NOT_SET_UP.format(
            domain=domain
        )

    def test_an_unfinished_microsoft_binding_is_not_set_up(self, env):
        """A Microsoft row with no provider (made before the operator flow)
        answers like any miss, not with a null the login page chokes on."""
        self._bind(env, provider_id=None)
        assert self._get("pat@acme.com").status_code == 404

    def test_a_google_binding_is_not_set_up_for_microsoft(self, env):
        self._bind(env, provider="google")
        assert self._get("pat@acme.com").status_code == 404


def test_a_lookup_with_no_address_answers_like_any_other(env):
    """One response shape for every miss: no pydantic list on screen."""
    response = TestClient(app).post("/api/sso/microsoft", json={})
    assert response.status_code == 404
    assert response.json()["detail"] == signup.MICROSOFT_NOT_SET_UP.format(
        domain="that address"
    )
