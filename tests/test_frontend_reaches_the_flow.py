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
        assert 'onClick={() => act("approve")}' in invoices
        assert 'onClick={() => act("reject")}' in invoices

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


class TestNoEndpointTakesATenant:
    """The API's central rule, restated where a client could break it.

    The tenant comes from the signed token. A frontend that passed one would
    be asking the server to trust the caller, which is the shape of every
    cross-tenant leak in this class of system.
    """

    def test_the_app_never_sends_a_tenant_id(self):
        assert "tenant_id=" not in source()
