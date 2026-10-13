"""Tenant and user provisioning.

Provisioning is what grants access, so the tests that matter are about ordering
and refusal:

* claims are written **last**, so a partial failure leaves a user who cannot log
  in rather than one who logs in to a tenant that isn't there;
* a slug that would break inbound email routing is refused before anything is
  written -- and that constraint is verified against `ingest.tenant_for_recipient`
  itself, not restated, so the two cannot drift;
* an unknown role is refused rather than defaulted, because the authorization
  layer would silently deny such a user everything.

Runs against SQLite. No Google credentials: the claims writer is a Protocol.
"""

import pytest

from invoice_processing.admin import provision
from invoice_processing.admin.provision import (
    DryRunClaims,
    Provisioner,
    ProvisioningError,
    validate_email,
    validate_role,
    validate_slug,
)
from invoice_processing.api.auth import principal_from_claims
from invoice_processing.api.ingest import tenant_for_recipient
from invoice_processing.db import session as db_session
from invoice_processing.db.models import Base


class RecordingClaims:
    def __init__(self, fail=False):
        self.written = []
        self.fail = fail

    def set_claims(self, uid, claims):
        if self.fail:
            raise RuntimeError("Identity Platform is down")
        self.written.append((uid, claims))


@pytest.fixture
def url(tmp_path):
    """A fresh SQLite file per test, with the schema created."""
    target = f"sqlite:///{tmp_path / 'provision.db'}"
    engine = db_session.get_engine(target)
    Base.metadata.create_all(engine)
    return target


@pytest.fixture
def claims():
    return RecordingClaims()


@pytest.fixture
def provisioner(claims, url):
    return Provisioner(claims=claims, url=url)


# ---------------------------------------------------------------------------
# Slugs, which are load-bearing for email routing
# ---------------------------------------------------------------------------


class TestSlugValidation:
    @pytest.mark.parametrize("slug", ["acme", "acme-corp", "a1", "globex-au-2"])
    def test_valid_slugs(self, slug):
        assert validate_slug(slug) == slug

    @pytest.mark.parametrize(
        "slug",
        [
            "",
            "A",  # too short and uppercase
            "ACME",  # uppercase would not match the lowercased recipient
            "acme+scans",  # '+' is truncated as sub-addressing
            "acme corp",  # a space is not a valid local part
            "-acme",  # leading hyphen
            "acme@corp",
            "acme/corp",  # would become a path segment in object keys
            "a" * 64,
        ],
    )
    def test_invalid_slugs_are_refused(self, slug):
        with pytest.raises(ProvisioningError):
            validate_slug(slug)

    @pytest.mark.parametrize("slug", ["acme", "acme-corp", "globex2"])
    def test_a_valid_slug_round_trips_through_email_routing(
        self, slug, monkeypatch
    ):
        """The real cross-module check: provisioning and ingestion must agree.

        Restating the rule in both modules is how they drift. This asserts the
        actual function ingestion uses.
        """
        monkeypatch.setenv("INBOUND_EMAIL_DOMAIN", "invoices.example.com")
        validate_slug(slug)
        assert tenant_for_recipient(f"{slug}@invoices.example.com") == slug

    def test_the_refused_slugs_are_refused_for_a_real_reason(self, monkeypatch):
        """`acme+x` would have routed to `acme`, silently, had we allowed it."""
        monkeypatch.setenv("INBOUND_EMAIL_DOMAIN", "invoices.example.com")
        assert tenant_for_recipient("acme+scans@invoices.example.com") == "acme"
        with pytest.raises(ProvisioningError):
            validate_slug("acme+scans")


class TestRoleValidation:
    @pytest.mark.parametrize("role", ["viewer", "clerk", "approver", "admin"])
    def test_known_roles(self, role):
        assert validate_role(role) == role

    @pytest.mark.parametrize("role", ["supervisor", "Admin", "", "root"])
    def test_unknown_roles_are_refused(self, role):
        """Otherwise we create a user who logs in and is denied everything."""
        with pytest.raises(ProvisioningError, match="Unknown role"):
            validate_role(role)


class TestEmailValidation:
    def test_normalised_to_lowercase(self):
        assert validate_email("  AP@Acme.Test ") == "ap@acme.test"

    @pytest.mark.parametrize("bad", ["", "no-at-sign", "@acme.test", "a@b@c"])
    def test_invalid_addresses_are_refused(self, bad):
        with pytest.raises(ProvisioningError):
            validate_email(bad)


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------


class TestCreateTenant:
    def test_creates_a_tenant(self, provisioner):
        record = provisioner.create_tenant("acme", "Acme Pty Ltd", "au")
        assert record["slug"] == "acme"
        assert record["id"]

    def test_slug_is_unique(self, provisioner):
        provisioner.create_tenant("acme", "Acme Pty Ltd")
        with pytest.raises(ProvisioningError, match="already exists"):
            provisioner.create_tenant("acme", "Acme Again")

    def test_an_invalid_slug_writes_nothing(self, provisioner):
        with pytest.raises(ProvisioningError):
            provisioner.create_tenant("ACME", "Acme Pty Ltd")
        assert provisioner.list_tenants() == []

    def test_a_blank_name_is_refused(self, provisioner):
        with pytest.raises(ProvisioningError, match="name is required"):
            provisioner.create_tenant("acme", "   ")

    def test_two_tenants_get_distinct_ids(self, provisioner):
        a = provisioner.create_tenant("acme", "Acme")
        b = provisioner.create_tenant("globex", "Globex")
        assert a["id"] != b["id"]

    def test_inbox_address(self, provisioner, monkeypatch):
        monkeypatch.setenv("INBOUND_EMAIL_DOMAIN", "invoices.example.com")
        assert provisioner.inbox_for("acme") == "acme@invoices.example.com"


# ---------------------------------------------------------------------------
# Users, and the ordering that keeps failures safe
# ---------------------------------------------------------------------------


class TestAddUser:
    def test_creates_the_user_and_writes_claims(self, provisioner, claims):
        tenant = provisioner.create_tenant("acme", "Acme Pty Ltd")
        record = provisioner.add_user(
            "acme", "ap@acme.test", "clerk", uid="fb-1"
        )

        assert record["claims_written"] is True
        assert claims.written == [
            ("fb-1", {"tenant_id": tenant["id"], "role": "clerk"})
        ]

    def test_claims_carry_the_tenant_id_not_the_slug(self, provisioner, claims):
        """Auth compares the claim against stored tenant_ids, not slugs."""
        tenant = provisioner.create_tenant("acme", "Acme Pty Ltd")
        provisioner.add_user("acme", "ap@acme.test", uid="fb-1")
        assert claims.written[0][1]["tenant_id"] == tenant["id"] != "acme"

    def test_no_uid_means_no_claims(self, provisioner, claims):
        """The row exists but grants nothing: the safe half-state."""
        provisioner.create_tenant("acme", "Acme Pty Ltd")
        record = provisioner.add_user("acme", "ap@acme.test")
        assert record["claims_written"] is False
        assert claims.written == []

    def test_a_failed_claim_write_still_leaves_a_locked_out_user(self, url):
        """Claims last: the failure direction must always be 'no access'."""
        provisioner = Provisioner(claims=RecordingClaims(fail=True), url=url)
        provisioner.create_tenant("acme", "Acme Pty Ltd")

        with pytest.raises(RuntimeError):
            provisioner.add_user("acme", "ap@acme.test", uid="fb-1")

        # The user row exists, but with no claims that user cannot authenticate.
        again = Provisioner(claims=RecordingClaims(), url=url)
        with pytest.raises(ProvisioningError, match="already exists"):
            again.add_user("acme", "ap@acme.test", uid="fb-1")

    def test_unknown_tenant_is_refused(self, provisioner):
        with pytest.raises(ProvisioningError, match="No tenant"):
            provisioner.add_user("nope", "ap@acme.test")

    def test_duplicate_email_within_a_tenant_is_refused(self, provisioner):
        provisioner.create_tenant("acme", "Acme Pty Ltd")
        provisioner.add_user("acme", "ap@acme.test")
        with pytest.raises(ProvisioningError, match="already exists"):
            provisioner.add_user("acme", "ap@acme.test")

    def test_the_same_email_may_exist_in_two_tenants(self, provisioner):
        """A consultant working for two customers is not a conflict."""
        provisioner.create_tenant("acme", "Acme")
        provisioner.create_tenant("globex", "Globex")
        a = provisioner.add_user("acme", "consultant@x.test")
        b = provisioner.add_user("globex", "consultant@x.test")
        assert a["tenant_id"] != b["tenant_id"]

    def test_an_invalid_role_writes_neither_row_nor_claims(
        self, provisioner, claims
    ):
        provisioner.create_tenant("acme", "Acme Pty Ltd")
        with pytest.raises(ProvisioningError):
            provisioner.add_user(
                "acme", "ap@acme.test", "supervisor", uid="fb-1"
            )
        assert claims.written == []
        # And the address is still free, so nothing was half-written.
        provisioner.add_user("acme", "ap@acme.test", "clerk")


class TestSetClaims:
    def test_attaches_an_account_to_an_existing_user(self, provisioner, claims):
        tenant = provisioner.create_tenant("acme", "Acme Pty Ltd")
        provisioner.add_user("acme", "ap@acme.test", "approver")

        record = provisioner.set_claims("acme", "ap@acme.test", "fb-9")
        assert record["claims"] == {
            "tenant_id": tenant["id"],
            "role": "approver",
        }
        assert claims.written == [("fb-9", record["claims"])]

    def test_role_comes_from_the_stored_user(self, provisioner, claims):
        """Not from an argument: the database is the record of who is what."""
        provisioner.create_tenant("acme", "Acme Pty Ltd")
        provisioner.add_user("acme", "boss@acme.test", "admin")
        provisioner.set_claims("acme", "boss@acme.test", "fb-9")
        assert claims.written[0][1]["role"] == "admin"

    def test_unknown_user_is_refused(self, provisioner):
        provisioner.create_tenant("acme", "Acme Pty Ltd")
        with pytest.raises(ProvisioningError, match="No user"):
            provisioner.set_claims("acme", "ghost@acme.test", "fb-9")


# ---------------------------------------------------------------------------
# The claims the auth layer will actually read
# ---------------------------------------------------------------------------


class TestClaimsMatchWhatAuthExpects:
    def test_provisioned_claims_produce_a_valid_principal(
        self, provisioner, claims
    ):
        """End to end: what provisioning writes is what auth can consume."""

        tenant = provisioner.create_tenant("acme", "Acme Pty Ltd")
        provisioner.add_user("acme", "ap@acme.test", "approver", uid="fb-1")
        uid, written = claims.written[0]

        principal = principal_from_claims(
            {
                **written,
                "sub": uid,
                "email": "ap@acme.test",
                "email_verified": True,
            }
        )
        assert principal.tenant_id == tenant["id"]
        assert principal.can("approver")
        assert not principal.can("admin")


# ---------------------------------------------------------------------------
# Safety of the default
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_the_default_claims_writer_writes_nothing(self):
        """A mistyped command must not reach Identity Platform."""
        writer = Provisioner().claims
        assert isinstance(writer, DryRunClaims)

    def test_cli_defaults_to_dry_run(self, monkeypatch, capsys, url):
        recorded = {}

        class Spy(Provisioner):
            def __init__(self, claims=None, **kwargs):
                recorded["claims"] = claims
                super().__init__(claims=claims, url=url)

        monkeypatch.setattr(provision, "Provisioner", Spy)
        provision.main(["create-tenant", "--slug", "acme", "--name", "Acme"])
        assert isinstance(recorded["claims"], DryRunClaims)

    def test_cli_reports_an_error_without_a_traceback(
        self, monkeypatch, capsys, url
    ):
        monkeypatch.setattr(
            provision,
            "Provisioner",
            lambda claims=None, **kw: Provisioner(claims=claims, url=url),
        )
        code = provision.main(["create-tenant", "--slug", "BAD", "--name", "x"])
        assert code == 1
        assert "error:" in capsys.readouterr().err


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
