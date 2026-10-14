"""The reviewer's flow must be reachable from the app, not just by curl.

Upload and export endpoints existed and were tested for a full phase while the
UI had no control for either, so the flow the product is sold on -- upload,
review, correct, approve, export -- could not be completed by a person.
Endpoint tests cannot catch that. Only asking what the interface can reach can.

These assertions used to run against the HTML the API served at `/`. That page
is gone; the Next.js app replaced it, so they run against its source instead.
Reading source rather than driving a browser is a deliberate trade: it cannot
prove a control renders, but it costs no browser download in CI and it does
catch the failure that actually happened here -- an endpoint with no way to
reach it. A Playwright smoke test over the running app is the missing half.
"""

import pathlib

import pytest

FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "src"

# Every view the sidebar offers. A link with no page behind it is a 404 the
# user meets after trusting the navigation.
VIEWS = (
    "dashboard",
    "invoices",
    "documents",
    "activity",
    "purchase-orders",
    "settings",
    "admin",
)

pytestmark = pytest.mark.skipif(
    not FRONTEND.exists(),
    reason="frontend/ is not present in this checkout",
)


def source() -> str:
    """Every .tsx and .ts file in the app, concatenated."""
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(FRONTEND.rglob("*.ts*"))
    )


def page(view: str) -> str:
    return (FRONTEND / "app" / view / "page.tsx").read_text(encoding="utf-8")


class TestNavigation:
    def test_every_sidebar_view_has_a_page(self):
        for view in VIEWS:
            assert (FRONTEND / "app" / view / "page.tsx").exists(), view

    def test_the_sidebar_offers_every_view(self):
        shell = (FRONTEND / "components" / "AppShell.tsx").read_text(
            encoding="utf-8"
        )
        for view in VIEWS:
            assert f'href: "/{view}"' in shell, view


class TestTheFlowHasControls:
    """A reachable endpoint is not a usable one.

    Asserting only that a URL appears would pass with every button deleted --
    the fetch calls would still mention it. Each step needs something a person
    can click or press.
    """

    def test_upload_has_a_control(self):
        for view in ("invoices", "documents"):
            assert 'type="file"' in page(view), view
            assert "/api/documents" in page(view), view

    def test_correcting_approving_and_rejecting_have_controls(self):
        invoices = page("invoices")
        assert "onClick={save}" in invoices
        # Both decisions now open the dialog that asks why, rather than
        # deciding on the spot -- an approval over a pipeline rejection is
        # worth as much as a rejection, and neither is worth much unrecorded.
        assert 'onClick={() => setDeciding("approve")}' in invoices
        assert 'onClick={() => setDeciding("reject")}' in invoices

    def test_a_decision_carries_a_reason_category(self):
        """The category is what routes a disagreement into the learning loop.
        A page that collects only free text cannot tell "we read it wrong"
        from "the rulebook is missing a rule"."""
        invoices = page("invoices")
        assert "reason_category: reasonCategory" in invoices
        # The dialog is shared with /approvals, so it lives in a component.
        dialog = (FRONTEND / "components" / "DecisionDialog.tsx").read_text(
            encoding="utf-8"
        )
        assert "REASON_CATEGORIES.map" in dialog
        assert "DecisionDialog" in invoices

    def test_export_has_a_control(self):
        assert "onClick={exportCsv}" in page("invoices")

    def test_retry_has_a_control(self):
        assert "/retry" in page("documents")

    def test_settings_can_be_saved(self):
        """The one screen that decides whether a real invoice is accepted."""
        settings = page("settings")
        assert 'method: "PUT"' in settings
        assert "/api/settings" in settings

    def test_purchase_orders_can_be_replaced(self):
        orders = page("purchase-orders")
        assert "/api/purchase-orders" in orders
        assert 'method: "PUT"' in orders

    def test_users_can_be_added(self):
        admin = page("admin")
        assert "/api/admin/users" in admin
        assert 'method: "POST"' in admin


class TestTheControlsAreHonest:
    def test_upload_accepts_only_pdfs(self):
        """The server checks magic bytes; the picker should not offer the rest."""
        for view in ("invoices", "documents"):
            assert 'accept="application/pdf"' in page(view), view

    def test_upload_does_not_claim_the_invoice_is_processed(self):
        """It is stored and queued; extraction happens later, in the worker."""
        invoices = page("invoices")
        assert "queued for processing" in invoices
        assert "duplicate" in invoices, (
            "a re-sent document must be reported as such"
        )

    def test_an_unapproved_export_is_explained_not_just_failed(self):
        """A reviewer needs to know approving is next, not that it broke."""
        invoices = page("invoices")
        assert "Approve the invoice before exporting" in invoices
        assert "409" in invoices

    def test_a_paused_pipeline_is_announced(self):
        """ "I uploaded and nothing happened" was the most expensive confusion
        in this project, and this banner is the answer to it."""
        shell = (FRONTEND / "components" / "AppShell.tsx").read_text(
            encoding="utf-8"
        )
        assert "Processing is paused" in shell
        assert "/api/system/status" in shell


class TestKeyboard:
    """Throughput target is ~45s per exception; that needs keys, not clicks."""

    @pytest.mark.parametrize("key", ["j", "k", "a", "r", "s", "e", "u"])
    def test_every_workbench_shortcut_survives(self, key):
        assert f'case "{key}":' in page("invoices"), key

    def test_typing_in_a_field_does_not_trigger_shortcuts(self):
        """Otherwise a vendor name containing "a" approves the invoice."""
        invoices = page("invoices")
        assert 'target?.tagName === "INPUT"' in invoices


class TestSignIn:
    """The app must be able to authenticate, not just to run locally.

    For a whole phase `setToken` was exported and never called, so no request
    ever carried a bearer token and the app worked only against an API started
    with AUTH_DISABLED. Pointed at a real deployment, every request was a 401.
    """

    def shell(self) -> str:
        return (FRONTEND / "components" / "AppShell.tsx").read_text(
            encoding="utf-8"
        )

    def test_a_final_refusal_is_shown_not_sent_to_sign_up(self):
        """A deactivated person and a suspended organisation are both
        refused for good; the shell must show the server's sentence and a
        sign-out link rather than send them to "Create your account". It
        recognises them by pattern, so the pattern and the server's two
        sentences are tied together here."""
        import re  # noqa: PLC0415

        from invoice_processing import sso  # noqa: PLC0415

        found = re.search(r"const FINAL_REFUSAL = /(.+?)/;", self.shell())
        assert found, "AppShell.tsx has no FINAL_REFUSAL pattern"
        final = re.compile(found.group(1))
        assert final.search(sso.SUSPENDED)
        assert final.search("This account has been deactivated. Ask an admin")
        assert not final.search(
            "This account is not attached to an organisation."
        )

    def test_a_sign_in_screen_exists(self):
        assert (FRONTEND / "components" / "SignIn.tsx").exists()

    def test_every_request_can_carry_a_token(self):
        assert "setTokenProvider" in self.shell()
        assert "currentIdToken" in self.shell()

    def test_the_token_is_fetched_per_request_not_stored(self):
        """Identity Platform tokens last an hour.

        One captured at sign-in makes every request after that a 401, and the
        failure arrives long after the code that caused it.
        """
        api = (FRONTEND / "lib" / "api.ts").read_text(encoding="utf-8")
        assert "provider ? await provider() : null" in api
        assert "let token: string | null = null" not in api, (
            "a stored token goes stale an hour after sign-in"
        )

    def test_an_error_carries_the_request_id_for_support(self):
        """A 500 shows a reference a person can quote; the id joins the
        frontend's report to the server log and the audit trail."""
        api = (FRONTEND / "lib" / "api.ts").read_text(encoding="utf-8")
        assert 'response.headers.get("x-request-id")' in api
        assert "requestId" in api
        assert "(ref " in api
        # Only the bare 500: a 503 carries a sentence ("... needs a
        # database") that pages string-match and a person needs to read.
        assert "this.status === 500 && this.requestId" in api
        assert "this.status >= 500" not in api

    def test_the_server_decides_whether_sign_in_is_required(self):
        """Not a client-side guess: a deployment with authentication disabled
        must never show anyone a sign-in screen."""
        shell = self.shell()
        # The screen is a consequence of what /api/me answered, never of a
        # client-side guess about whether authentication is switched on.
        assert '"/api/me"' in shell
        assert "me === null" in shell
        assert "caught instanceof ApiError" in shell

    def test_a_blocked_popup_falls_back_to_a_redirect(self):
        """A popup is the nicest sign-in and the easiest one to block.

        Extensions, mobile browsers and corporate policy all stop it, and the
        user is left pressing a button that appears to do nothing.
        """
        auth = (FRONTEND / "lib" / "auth.ts").read_text(encoding="utf-8")
        assert "signInWithRedirect" in auth
        assert "auth/popup-blocked" in auth

    def test_a_cancelled_popup_does_not_redirect(self):
        """Closing the window is a decision, not a failure. Retrying by
        another route would drag the user through a sign-in they declined."""
        auth = (FRONTEND / "lib" / "auth.ts").read_text(encoding="utf-8")
        assert (
            "auth/popup-closed-by-user"
            not in auth.split("POPUP_UNAVAILABLE")[1].split(")")[0]
        )

    def test_a_failed_redirect_reports_itself(self):
        """The result arrives as a page load, so nobody is waiting to catch
        the error; uncollected it looks like a button that does nothing."""
        auth = (FRONTEND / "lib" / "auth.ts").read_text(encoding="utf-8")
        assert "getRedirectResult" in auth
        signin = (FRONTEND / "components" / "SignIn.tsx").read_text(
            encoding="utf-8"
        )
        assert "redirectSignInError" in signin

    def test_a_refused_account_is_not_shown_the_sign_in_button_again(self):
        """403 means the token was accepted and the account has no access.

        Rendering the ordinary sign-in screen for that sent the user back to
        the button they had just used successfully, which is indistinguishable
        from a button that does nothing -- and it loops forever, because
        signing in again cannot add a claim the account does not have.
        """
        shell = self.shell()
        assert "status === 403" in shell
        signin = (FRONTEND / "components" / "SignIn.tsx").read_text(
            encoding="utf-8"
        )
        assert "no access" in signin
        assert "administrator" in signin

    def test_a_refused_account_can_sign_out(self):
        """Otherwise the only way to try another account is clearing cookies."""
        signin = (FRONTEND / "components" / "SignIn.tsx").read_text(
            encoding="utf-8"
        )
        assert "try another account" in signin

    def test_signing_out_is_possible(self):
        assert "signOut" in self.shell()

    def test_dev_mode_is_announced(self):
        """An API answering without a token is a fact worth putting on screen
        -- it is the difference between a demo and a system holding real
        money."""
        assert "dev mode" in self.shell()

    def test_no_secret_is_baked_into_the_bundle(self):
        """The Firebase client config is public by design; a service-account
        key or an API secret is not, and must never be read here."""
        auth = (FRONTEND / "lib" / "auth.ts").read_text(encoding="utf-8")
        for forbidden in ("private_key", "client_secret", "SERVICE_ACCOUNT"):
            assert forbidden not in auth, forbidden
        # Anything reaching the browser must be explicitly public-prefixed.
        for line in auth.splitlines():
            if "process.env." in line:
                assert "process.env.NEXT_PUBLIC_" in line, line


class TestLoginAndSignUp:
    """Access is granted per organisation, and the pages have to say so.

    A sign-up that creates its own tenant would let anyone with a Google
    address mint organisations, inbound-mail addresses and storage prefixes
    inside the system -- which is what ALLOW_TENANT_PROVISIONING being off by
    default exists to prevent. So sign-up registers an account and grants
    nothing; an administrator does the rest.
    """

    def page(self, route: str) -> str:
        return (FRONTEND / "app" / route / "page.tsx").read_text(
            encoding="utf-8"
        )

    def test_both_routes_exist(self):
        for route in ("login", "signup"):
            assert (FRONTEND / "app" / route / "page.tsx").exists(), route

    def test_they_render_without_the_shell_gate(self):
        """Otherwise an unauthenticated visitor is redirected away from the
        very page that would let them sign in."""
        shell = (FRONTEND / "components" / "AppShell.tsx").read_text(
            encoding="utf-8"
        )
        assert "PUBLIC_ROUTES" in shell
        assert '"/login"' in shell and '"/signup"' in shell

    def test_the_two_failures_go_to_different_places(self):
        """Not signed in is a different problem from signed in with no
        organisation, and sending both to the sign-in button was how someone
        who had just signed in was asked to sign in again."""
        shell = (FRONTEND / "components" / "AppShell.tsx").read_text(
            encoding="utf-8"
        )
        assert 'denied ? "/signup" : "/login"' in shell

    def test_sign_up_provisions_through_the_gated_endpoint(self):
        """Never through the admin routes, which belong to an existing
        tenant's administrator and would be a way to act inside someone
        else's organisation."""
        signup = self.page("signup")
        assert '"/api/signup"' in signup
        assert "/api/admin/tenants" not in signup

    def test_sign_up_verifies_the_email_before_provisioning(self):
        """Invoices are financial records, so an address has to be proven.
        The API refuses an unverified token anyway, so signing straight
        through would land on a 403 with nothing explaining it."""
        signup = self.page("signup")
        assert "isEmailVerified" in signup
        assert "Check your email" in signup

    def test_sign_up_refreshes_the_token_after_provisioning(self):
        """Claims are baked in when a token is minted. Without a forced
        refresh the account keeps presenting the one it signed up with -- no
        claims -- and is refused by the system that just granted it access."""
        assert "refreshToken" in self.page("signup")

    def test_passwords_are_offered_as_well_as_google(self):
        for route in ("login", "signup"):
            assert "PasswordForm" in self.page(route), route

    def test_a_failed_sign_in_does_not_say_which_half_was_wrong(self):
        """Saying the address exists but the password is wrong tells an
        attacker which addresses have accounts here."""
        auth = (FRONTEND / "lib" / "auth.ts").read_text(encoding="utf-8")
        assert "do not match an account" in auth
        assert "auth/user-not-found" in auth

    def test_login_links_to_the_explanation(self):
        assert "/signup" in self.page("login")


class TestNoEndpointTakesATenant:
    """The API's central rule, restated where a client could break it.

    The tenant comes from the signed token. A frontend that passed one would
    be asking the server to trust the caller, which is the shape of every
    cross-tenant leak in this class of system.
    """

    def test_the_app_never_sends_a_tenant_id(self):
        assert "tenant_id=" not in source()


def test_the_content_security_policy_is_enforced():
    """Report-only until Google sign-in had run against a live Identity
    Platform project (2026-10-14, rasayan): sign-in, sign-up and the main
    pages raised no report on a production build, so it now blocks."""
    config = (FRONTEND.parent / "next.config.ts").read_text(encoding="utf-8")
    assert '{ key: "Content-Security-Policy", value: CSP }' in config
    assert "Report-Only" not in config


class TestMicrosoftSignIn:
    def test_the_button_asks_which_provider_then_signs_in_through_it(self):
        sign_in = (FRONTEND / "components" / "SignIn.tsx").read_text(
            encoding="utf-8"
        )
        auth = (FRONTEND / "lib" / "auth.ts").read_text(encoding="utf-8")
        assert "/api/sso/microsoft" in sign_in
        # A POST body: the address is kept out of URLs that proxies log.
        assert 'method: "POST"' in sign_in
        assert "signInWithMicrosoft" in sign_in
        assert "new mod.OAuthProvider(providerId)" in auth
        assert "login_hint" in auth

    def test_the_admin_form_offers_google_only(self):
        admin = (FRONTEND / "app" / "admin" / "page.tsx").read_text(
            encoding="utf-8"
        )
        assert 'value="microsoft"' not in admin
        assert "set up by Ledgerly support" in admin
