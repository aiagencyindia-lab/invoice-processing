"""Who a verified token is, once the users table has a say."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from invoice_processing import sso
from invoice_processing.api.auth import AuthError, Principal

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
ACME = "tenant-acme"


class FakeDirectory:
    def __init__(self, users=None, domains=None, fail=False):
        self.users = {(u["tenant_id"], u["email"]): u for u in users or []}
        self.domains = {d["domain"]: d for d in domains or []}
        self.fail = fail
        self.touched, self.linked, self.provisioned = [], [], []
        self.audits = []
        self.provision_conflict, self.provision_conflict_row = False, None

    def find(self, tenant_id, email):
        if self.fail:
            raise RuntimeError("db down")
        u = self.users.get((tenant_id, email.lower()))
        return dict(u, tenant_name="Acme Pty Ltd") if u else None

    def touch_login(self, tenant_id, user_id, now):
        self.touched.append((user_id, now))

    def link_uid(self, tenant_id, user_id, uid):
        self.linked.append((user_id, uid))

    def provision(
        self, tenant_id, email, role, uid, display_name, *, audit=None, now=None
    ):
        if self.provision_conflict:
            # A concurrent first login inserted the row first.
            self.provision_conflict = False
            self.users[(tenant_id, email.lower())] = self.provision_conflict_row
            raise IntegrityError(
                "INSERT INTO users", {}, Exception("uq_user_email")
            )
        row = {
            "id": f"id-{email}",
            "tenant_id": tenant_id,
            "email": email.lower(),
            "role": role,
            "is_active": True,
            "uid": uid,
            "display_name": display_name,
            "last_login_at": now,
            "tenant_name": "Acme Pty Ltd",
        }
        self.users[(tenant_id, email.lower())] = row
        self.provisioned.append(row)
        if audit is not None:
            ctx, event, payload = audit
            self.audits.append(
                (ctx.tenant_id, event, {**payload, "user_id": row["id"]})
            )
        return dict(row)

    def domain(self, domain):
        if self.fail:
            raise RuntimeError("db down")
        return self.domains.get(domain)


class FakeClaims:
    def __init__(self, fail=False):
        self.written, self.fail = [], fail

    def set_claims(self, uid, claims):
        if self.fail:
            raise RuntimeError("identity platform down")
        self.written.append((uid, claims))


class FakeAudit:
    def __init__(self, fail=False):
        self.events, self.fail = [], fail

    def emit(self, ctx, event_type, payload, before=None):
        if self.fail:
            raise RuntimeError("audit down")
        self.events.append((ctx.tenant_id, ctx.actor, event_type, payload))


def services(**kw):
    return sso.SsoServices(
        kw.get("directory", FakeDirectory()),
        kw.get("claims", FakeClaims()),
        kw.get("audit", FakeAudit()),
    )


def claimed(**extra):
    return {
        "sub": "fb-1",
        "email": "pat@acme.com",
        "email_verified": True,
        "tenant_id": ACME,
        "role": "clerk",
        **extra,
    }


def unclaimed(**extra):
    return {
        "sub": "fb-9",
        "email": "new@acme.com",
        "email_verified": True,
        "name": "New Person",
        "firebase": {"sign_in_provider": "google.com"},
        **extra,
    }


ROW = {
    "id": "u-1",
    "tenant_id": ACME,
    "email": "pat@acme.com",
    "role": "approver",
    "is_active": True,
    "uid": "fb-1",
    "display_name": None,
    "last_login_at": None,
}
BOUND = {
    "id": "d-1",
    "tenant_id": ACME,
    "tenant_name": "Acme Pty Ltd",
    "domain": "acme.com",
    "provider": "google",
    "default_role": "viewer",
}


class TestDomains:
    def test_public_mailboxes_are_refused(self):
        for d in (
            "gmail.com",
            "Googlemail.com",
            "outlook.com",
            "hotmail.com",
            "live.com",
            "yahoo.com",
            "icloud.com",
            "proton.me",
            "protonmail.com",
        ):
            with pytest.raises(ValueError):
                sso.normalize_domain(d)

    def test_domains_are_lower_cased_and_validated(self):
        assert sso.normalize_domain(" Acme.COM ") == "acme.com"
        for bad in (
            "",
            "acme",
            "a b.com",
            "-acme.com",
            "acme.com/x",
            "x" * 250 + ".com",
        ):
            with pytest.raises(ValueError):
                sso.normalize_domain(bad)

    def test_domain_of_email(self):
        assert sso.domain_of("Pat+x@Acme.com") == "acme.com"


class TestClaimedTokens:
    def test_a_deactivated_user_is_recorded_as_refused(self):
        off = dict(ROW, is_active=False)
        audit = FakeAudit()
        with pytest.raises(AuthError):
            sso.resolve_principal(
                claimed(),
                services(directory=FakeDirectory([off]), audit=audit),
                now=NOW,
            )
        assert audit.events == [
            (
                ACME,
                "pat@acme.com",
                "sign_in_refused",
                {
                    "email": "pat@acme.com",
                    "user_id": "u-1",
                    "reason": "deactivated",
                },
            )
        ]

    def test_a_refusal_that_cannot_be_recorded_is_a_503(self):
        """The refusal stands either way; what must not happen is a silent gap."""
        off = dict(ROW, is_active=False)
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(
                claimed(),
                services(
                    directory=FakeDirectory([off]), audit=FakeAudit(fail=True)
                ),
                now=NOW,
            )
        assert exc.value.status_code == 503

    def test_a_deactivated_dev_identity_is_recorded_too(self):
        """enforce_active is the dev-bypass path; it must not be the quiet one."""
        off = dict(ROW, is_active=False)
        audit = FakeAudit()
        dev = Principal(
            user_id="dev", email="pat@acme.com", tenant_id=ACME, role="admin"
        )
        with pytest.raises(AuthError):
            sso.enforce_active(
                dev, services(directory=FakeDirectory([off]), audit=audit)
            )
        assert [e[2] for e in audit.events] == ["sign_in_refused"]

    def test_without_a_sink_a_refusal_is_still_a_refusal(self):
        off = dict(ROW, is_active=False)
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(
                claimed(),
                services(directory=FakeDirectory([off]), audit=None),
                now=NOW,
            )
        assert exc.value.status_code == 403

    def test_the_row_role_beats_the_claim(self):
        p = sso.resolve_principal(
            claimed(), services(directory=FakeDirectory([ROW])), now=NOW
        )
        assert p.role == "approver" and p.tenant_id == ACME

    def test_no_row_keeps_the_claim_role(self):
        p = sso.resolve_principal(claimed(), services(), now=NOW)
        assert p.role == "clerk"

    def test_no_row_is_logged_so_the_gap_is_visible(self, caplog):
        with caplog.at_level("WARNING", logger="invoice_processing.sso"):
            sso.resolve_principal(claimed(), services(), now=NOW)
        assert "has no users row in" in caplog.text
        assert "claim role clerk stands" in caplog.text

    def test_a_deactivated_user_is_refused_on_every_request(self):
        off = dict(ROW, is_active=False)
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(
                claimed(), services(directory=FakeDirectory([off])), now=NOW
            )
        assert exc.value.status_code == 403
        assert (
            "deactivated" in exc.value.detail
            and "Acme Pty Ltd" in exc.value.detail
        )

    def test_a_row_in_another_tenant_never_speaks_for_this_one(self):
        other = dict(ROW, tenant_id="tenant-globex", role="admin")
        p = sso.resolve_principal(
            claimed(), services(directory=FakeDirectory([other])), now=NOW
        )
        assert p.role == "clerk"

    def test_email_is_normalised_before_lookup_and_storage(self):
        d = FakeDirectory([ROW])
        p = sso.resolve_principal(
            claimed(email="Pat@Acme.com"), services(directory=d), now=NOW
        )
        assert p.role == "approver"

    def test_last_login_is_touched_at_most_every_fifteen_minutes(self):
        d = FakeDirectory([ROW])
        sso.resolve_principal(claimed(), services(directory=d), now=NOW)
        assert d.touched == [("u-1", NOW)]
        d.users[(ACME, "pat@acme.com")]["last_login_at"] = NOW
        sso.resolve_principal(
            claimed(), services(directory=d), now=NOW + timedelta(minutes=5)
        )
        assert len(d.touched) == 1
        sso.resolve_principal(
            claimed(), services(directory=d), now=NOW + timedelta(minutes=16)
        )
        assert len(d.touched) == 2

    def test_a_directory_error_refuses_rather_than_guessing(self):
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(
                claimed(), services(directory=FakeDirectory(fail=True)), now=NOW
            )
        assert (
            exc.value.status_code == 503
            and "needs a database" in exc.value.detail
        )

    def test_without_services_claims_are_the_authority(self):
        p = sso.resolve_principal(claimed(), None, now=NOW)
        assert p.role == "clerk"


class TestUnclaimedTokens:
    def test_a_bound_domain_provisions_the_default_role(self):
        d, c = FakeDirectory(domains=[BOUND]), FakeClaims()
        p = sso.resolve_principal(unclaimed(), sso.SsoServices(d, c), now=NOW)
        assert (p.tenant_id, p.role, p.email) == (
            ACME,
            "viewer",
            "new@acme.com",
        )
        assert d.provisioned[0]["uid"] == "fb-9"
        assert d.provisioned[0]["display_name"] == "New Person"
        assert c.written == [("fb-9", {"tenant_id": ACME, "role": "viewer"})]
        # Written in the insert's own transaction: services has no sink.
        assert d.audits == [
            (
                ACME,
                "user_provisioned_by_sso",
                {
                    "email": "new@acme.com",
                    "role": "viewer",
                    "domain": "acme.com",
                    "user_id": "id-new@acme.com",
                },
            )
        ]

    def test_provisioning_stamps_the_login_without_a_second_write(self):
        d = FakeDirectory(domains=[BOUND])
        sso.resolve_principal(unclaimed(), services(directory=d), now=NOW)
        assert d.provisioned[0]["last_login_at"] == NOW
        assert d.touched == []

    def test_a_binding_for_another_provider_refuses_a_google_account(self):
        bound = dict(BOUND, provider="microsoft")
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(
                unclaimed(),
                services(directory=FakeDirectory(domains=[bound])),
                now=NOW,
            )
        assert exc.value.status_code == 403

    def test_a_concurrent_first_login_proceeds_as_the_winning_row(self):
        d = FakeDirectory(domains=[BOUND])
        d.provision_conflict = True
        d.provision_conflict_row = dict(
            ROW, email="new@acme.com", role="viewer"
        )
        p = sso.resolve_principal(unclaimed(), services(directory=d), now=NOW)
        assert (p.tenant_id, p.role) == (ACME, "viewer")
        assert d.provisioned == [] and d.audits == []

    def test_a_uid_held_by_another_persons_row_refuses_sign_in(self):
        """uq_user_uid fires and no row exists for this email: refuse, and
        never proceed as whoever holds the uid."""
        d = FakeDirectory(domains=[BOUND])
        d.provision_conflict = True  # the insert loses, and no row appears
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(unclaimed(), services(directory=d), now=NOW)
        assert exc.value.status_code == 503 and d.provisioned == []

    @pytest.mark.parametrize("provider", ["password", "anonymous"])
    def test_only_google_can_earn_a_tenant(self, provider):
        token = unclaimed(firebase={"sign_in_provider": provider})
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(
                token,
                services(directory=FakeDirectory(domains=[BOUND])),
                now=NOW,
            )
        assert exc.value.status_code == 403

    def test_a_string_true_is_not_a_verified_email(self):
        token = unclaimed(email_verified="true")
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(
                token,
                services(directory=FakeDirectory(domains=[BOUND])),
                now=NOW,
            )
        assert exc.value.status_code == 403

    @pytest.mark.parametrize(
        "email", ["x@acme.com@evil.test", "x@acme.com.evil.test"]
    )
    def test_a_lookalike_address_is_not_the_bound_domain(self, email):
        d = FakeDirectory(domains=[BOUND])
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(
                unclaimed(email=email), services(directory=d), now=NOW
            )
        assert exc.value.status_code == 403 and d.provisioned == []

    def test_an_existing_row_is_linked_not_duplicated(self):
        pre = dict(ROW, email="new@acme.com", uid=None, role="clerk")
        d = FakeDirectory([pre], domains=[BOUND])
        p = sso.resolve_principal(unclaimed(), services(directory=d), now=NOW)
        assert (
            p.role == "clerk"
            and d.provisioned == []
            and d.linked == [("u-1", "fb-9")]
        )

    def test_a_row_linked_to_another_uid_is_relinked_to_this_one(self, caplog):
        pre = dict(ROW, email="new@acme.com", uid="fb-old", role="clerk")
        d = FakeDirectory([pre], domains=[BOUND])
        with caplog.at_level("WARNING", logger="invoice_processing.sso"):
            p = sso.resolve_principal(
                unclaimed(), services(directory=d), now=NOW
            )
        assert p.role == "clerk" and d.linked == [("u-1", "fb-9")]
        assert "fb-old" in caplog.text and "fb-9" in caplog.text

    def test_a_row_already_linked_to_this_uid_is_left_alone(self):
        pre = dict(ROW, email="new@acme.com", uid="fb-9", role="clerk")
        d = FakeDirectory([pre], domains=[BOUND])
        sso.resolve_principal(unclaimed(), services(directory=d), now=NOW)
        assert d.linked == []

    def test_an_unbound_domain_is_not_a_member(self):
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(unclaimed(), services(), now=NOW)
        assert exc.value.status_code == 403 and "tenant_id" in exc.value.detail

    def test_microsoft_is_not_enabled_yet(self):
        token = unclaimed(firebase={"sign_in_provider": "microsoft.com"})
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(
                token,
                services(directory=FakeDirectory(domains=[BOUND])),
                now=NOW,
            )
        assert "Microsoft" in exc.value.detail

    def test_an_unverified_email_is_refused(self):
        token = unclaimed(email_verified=False)
        with pytest.raises(AuthError):
            sso.resolve_principal(
                token,
                services(directory=FakeDirectory(domains=[BOUND])),
                now=NOW,
            )

    def test_a_claims_write_failure_does_not_block_the_first_login(self):
        d = FakeDirectory(domains=[BOUND])
        p = sso.resolve_principal(
            unclaimed(),
            services(directory=d, claims=FakeClaims(fail=True)),
            now=NOW,
        )
        assert p.tenant_id == ACME and d.provisioned

    def test_without_services_an_unclaimed_token_is_refused(self):
        with pytest.raises(AuthError) as exc:
            sso.resolve_principal(unclaimed(), None, now=NOW)
        assert exc.value.status_code == 403


class TestEnforceActive:
    def test_a_deactivated_dev_identity_is_refused(self):
        off = dict(ROW, is_active=False, email="dev@acme.com")
        p = Principal(
            user_id="dev", email="dev@acme.com", tenant_id=ACME, role="admin"
        )
        with pytest.raises(AuthError):
            sso.enforce_active(p, services(directory=FakeDirectory([off])))

    def test_an_unreachable_database_refuses_the_dev_identity(self):
        p = Principal(
            user_id="dev", email="dev@acme.com", tenant_id=ACME, role="admin"
        )
        with pytest.raises(AuthError) as exc:
            sso.enforce_active(p, services(directory=FakeDirectory(fail=True)))
        assert exc.value.status_code == 503

    def test_the_header_role_is_kept(self):
        row = dict(ROW, email="dev@acme.com", role="viewer")
        p = Principal(
            user_id="dev", email="dev@acme.com", tenant_id=ACME, role="admin"
        )
        assert (
            sso.enforce_active(p, services(directory=FakeDirectory([row]))).role
            == "admin"
        )


def test_sso_never_logs_an_email_address(caplog):
    """A log line is read by more people than the audit trail; the uid and
    tenant identify the account without naming the person."""
    d = FakeDirectory(
        [dict(ROW, email="new@acme.com", uid="fb-old", role="clerk")],
        domains=[BOUND],
    )
    with caplog.at_level("WARNING", logger="invoice_processing.sso"):
        sso.resolve_principal(claimed(), services(), now=NOW)
        sso.resolve_principal(unclaimed(), services(directory=d), now=NOW)
    assert caplog.records, "both paths log a warning"
    assert "@" not in caplog.text
