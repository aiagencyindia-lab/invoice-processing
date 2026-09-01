"""Settings and purchase orders — the two things that blocked a new tenant.

Both previously required editing a file on disk. Without the organisation name,
check 1.2 rejects every real invoice as "addressed to different company";
without purchase orders, the Step 2.2 match reports every quoted PO as missing.
A customer could not reach their first successful invoice without a terminal.

The tests concentrate on three risks that come with exposing configuration over
an API: writing keys that were never meant to be settable, one tenant reading
or overwriting another's data, and a change that saves but never reaches the
engine — which would be the worst outcome, since the UI would report success
while nothing changed.
"""

import json

import pytest
from fastapi.testclient import TestClient

from invoice_processing import paths, tenant_settings
from invoice_processing.adapters import JsonlAuditSink
from invoice_processing.api import auth, service
from invoice_processing.api.service import app
from invoice_processing.shared_libraries.acting import general_invoice_agent
from invoice_processing.tenant_settings import JsonTenantSettings, SettingsError

TENANT = "tenant-acme"


class FakeVerifier:
    def verify(self, token):
        role, _, tenant = token.partition("@")
        if role not in ("viewer", "clerk", "approver", "admin"):
            raise auth.AuthError("Invalid token")
        return {
            "sub": f"u-{token}",
            "email": f"{role}@x.test",
            "email_verified": True,
            "tenant_id": tenant or TENANT,
            "role": role,
        }


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = JsonTenantSettings(tmp_path / "settings.json")
    monkeypatch.setattr(service, "_tenant_settings", store)
    monkeypatch.setattr(
        service, "_audit", JsonlAuditSink(tmp_path / "audit.jsonl")
    )
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    # Per-tenant PO files under a temp root, with no legacy shared file.
    monkeypatch.setattr(
        paths, "PURCHASE_ORDER_DIR", tmp_path / "purchase_orders"
    )
    monkeypatch.setattr(paths, "PURCHASE_ORDER_FILE", tmp_path / "absent.json")
    return {
        "store": store,
        "tmp": tmp_path,
        "client": lambda token="admin": TestClient(
            app, headers={"Authorization": f"Bearer {token}"}
        ),
    }


# ---------------------------------------------------------------------------
# Settings validation — the allow-list
# ---------------------------------------------------------------------------


class TestValidation:
    def test_accepts_the_settable_keys(self):
        cleaned = tenant_settings.validate(
            {
                "organization_names": ["Enew bharat"],
                "duplicate_check_enabled": True,
                "po_tolerance_pct": 0.05,
            }
        )
        assert cleaned["organization_names"] == ["Enew bharat"]

    @pytest.mark.parametrize(
        "key",
        [
            "gemini_pro_model",
            "purchase_order_file",
            "balance_tolerance",
            "__class__",
        ],
    )
    def test_refuses_keys_that_are_not_settable(self, key):
        """These land in the engine config; a passthrough would be arbitrary control."""
        with pytest.raises(SettingsError, match="Not settable"):
            tenant_settings.validate({key: "anything"})

    def test_refuses_a_wrong_type(self):
        with pytest.raises(SettingsError, match="must be"):
            tenant_settings.validate({"organization_names": "Acme"})

    def test_a_boolean_is_not_a_number(self):
        """bool subclasses int, so True would otherwise pass a numeric check."""
        with pytest.raises(SettingsError, match="not a true/false"):
            tenant_settings.validate({"po_tolerance_pct": True})

    def test_refuses_an_empty_organisation_list(self):
        """Every invoice would be rejected as addressed to someone else."""
        with pytest.raises(SettingsError, match="At least one"):
            tenant_settings.validate({"organization_names": ["", "  "]})

    def test_refuses_a_negative_tolerance(self):
        with pytest.raises(SettingsError, match="negative"):
            tenant_settings.validate({"po_tolerance_abs": -10})

    def test_trims_organisation_names(self):
        cleaned = tenant_settings.validate(
            {"organization_names": ["  Acme  ", ""]}
        )
        assert cleaned["organization_names"] == ["Acme"]


# ---------------------------------------------------------------------------
# Settings endpoint
# ---------------------------------------------------------------------------


class TestSettingsEndpoint:
    def test_read_returns_defaults_with_descriptions(self, env):
        body = env["client"]("viewer").get("/api/settings").json()
        assert "organization_names" in body["settings"]
        assert all(f["description"] for f in body["fields"])

    def test_every_role_can_read(self, env):
        """A clerk who cannot see the configured names cannot explain a rejection."""
        for role in ("viewer", "clerk", "approver", "admin"):
            assert env["client"](role).get("/api/settings").status_code == 200

    def test_only_admin_can_write(self, env):
        for role in ("viewer", "clerk", "approver"):
            response = env["client"](role).put(
                "/api/settings",
                json={"settings": {"duplicate_check_enabled": False}},
            )
            assert response.status_code == 403

    def test_write_then_read_round_trip(self, env):
        env["client"]().put(
            "/api/settings",
            json={"settings": {"organization_names": ["Enew bharat"]}},
        )
        body = env["client"]().get("/api/settings").json()
        assert body["settings"]["organization_names"] == ["Enew bharat"]

    def test_a_partial_update_keeps_other_settings(self, env):
        client = env["client"]()
        client.put(
            "/api/settings", json={"settings": {"organization_names": ["A"]}}
        )
        client.put(
            "/api/settings", json={"settings": {"po_tolerance_abs": 25.0}}
        )
        settings = client.get("/api/settings").json()["settings"]
        assert settings["organization_names"] == ["A"]
        assert settings["po_tolerance_abs"] == 25.0

    def test_an_invalid_key_is_400_not_500(self, env):
        response = env["client"]().put(
            "/api/settings", json={"settings": {"gemini_pro_model": "evil"}}
        )
        assert response.status_code == 400
        assert "Not settable" in response.json()["detail"]

    def test_a_change_is_audited(self, env):
        """These values decide whether invoices are accepted -- a business event."""
        env["client"]().put(
            "/api/settings",
            json={"settings": {"duplicate_check_enabled": False}},
        )
        events = json.loads(
            (env["tmp"] / "audit.jsonl").read_text().strip().splitlines()[-1]
        )
        assert events["event_type"] == "settings_changed"

    def test_requires_authentication(self, env):
        assert TestClient(app).get("/api/settings").status_code == 401


class TestSettingsReachTheEngine:
    """A change that saves but never reaches the pipeline is the worst outcome:
    the UI reports success while nothing actually changed."""

    def test_apply_to_pipeline_uses_the_tenant_value(self, env):
        env["store"].put(TENANT, {"organization_names": ["Enew bharat"]})
        try:
            config = tenant_settings.apply_to_pipeline(TENANT, env["store"])
            assert config["organization_names"] == ["Enew bharat"]
            assert general_invoice_agent._get_config()[
                "organization_names"
            ] == ["Enew bharat"]
        finally:
            general_invoice_agent._config_store["CONFIG"] = (
                general_invoice_agent.DEFAULT_CONFIG.copy()
            )

    def test_an_unset_tenant_gets_master_data_defaults(self, env):
        effective = tenant_settings.effective(
            "tenant-nothing-set", env["store"]
        )
        assert (
            effective["organization_names"]
            == general_invoice_agent.DEFAULT_CONFIG["organization_names"]
        )

    def test_the_tenants_po_file_is_threaded_into_config(self, env):
        try:
            config = tenant_settings.apply_to_pipeline(TENANT, env["store"])
            assert TENANT.replace("-", "-") in config["purchase_order_file"]
        finally:
            general_invoice_agent._config_store["CONFIG"] = (
                general_invoice_agent.DEFAULT_CONFIG.copy()
            )


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------


def po(number="PO-1", vendor="Acme Pty Ltd", **overrides):
    return {
        "po_number": number,
        "vendor_name": vendor,
        "total_amount": 10000.0,
        "currency": "AUD",
        "status": "open",
        "amount_invoiced": 6000.0,
        **overrides,
    }


class TestPurchaseOrders:
    def test_empty_is_reported_explicitly(self, env):
        """The answer to 'why does every PO come back not found?'"""
        body = env["client"]("clerk").get("/api/purchase-orders").json()
        assert body == {
            "purchase_orders": [],
            "count": 0,
            "source": f"{TENANT}.json",
        }

    def test_upload_then_list(self, env):
        env["client"]().put(
            "/api/purchase-orders", json={"purchase_orders": [po()]}
        )
        body = env["client"]("clerk").get("/api/purchase-orders").json()
        assert body["count"] == 1
        assert body["purchase_orders"][0]["remaining"] == 4000.0
        assert body["purchase_orders"][0]["open"] is True

    def test_replace_does_not_merge(self, env):
        """A merge would keep orders the customer's ERP has since closed."""
        client = env["client"]()
        client.put(
            "/api/purchase-orders",
            json={"purchase_orders": [po("PO-1"), po("PO-2")]},
        )
        client.put(
            "/api/purchase-orders", json={"purchase_orders": [po("PO-3")]}
        )
        body = client.get("/api/purchase-orders").json()
        assert body["count"] == 1
        assert body["purchase_orders"][0]["po_number"] == "PO-3"

    def test_a_duplicate_number_is_refused(self, env):
        """The match would use whichever loaded last."""
        response = env["client"]().put(
            "/api/purchase-orders",
            json={"purchase_orders": [po("PO-1"), po("po 1")]},
        )
        assert response.status_code == 400
        assert "twice" in response.json()["detail"]

    def test_a_missing_vendor_is_refused(self, env):
        """Without one the vendor-mismatch check -- the important one -- cannot run."""
        response = env["client"]().put(
            "/api/purchase-orders", json={"purchase_orders": [po(vendor="")]}
        )
        assert response.status_code == 400
        assert "no vendor" in response.json()["detail"]

    def test_a_missing_number_is_refused(self, env):
        response = env["client"]().put(
            "/api/purchase-orders", json={"purchase_orders": [po(number="")]}
        )
        assert response.status_code == 400

    def test_nothing_is_written_when_validation_fails(self, env):
        """A malformed export must not leave the match half-loaded."""
        client = env["client"]()
        client.put(
            "/api/purchase-orders", json={"purchase_orders": [po("PO-GOOD")]}
        )
        client.put(
            "/api/purchase-orders",
            json={"purchase_orders": [po("PO-NEW"), po(vendor="")]},
        )
        body = client.get("/api/purchase-orders").json()
        assert body["purchase_orders"][0]["po_number"] == "PO-GOOD"

    def test_a_clerk_cannot_replace(self, env):
        response = env["client"]("clerk").put(
            "/api/purchase-orders", json={"purchase_orders": [po()]}
        )
        assert response.status_code == 403

    def test_a_viewer_cannot_even_list(self, env):
        assert (
            env["client"]("viewer").get("/api/purchase-orders").status_code
            == 403
        )


class TestPurchaseOrderTenancy:
    """Purchase orders name suppliers and commitment amounts.

    A single shared file across tenants would be a disclosure: one customer
    seeing another's supplier list, and able to overwrite it.
    """

    def test_one_tenant_cannot_see_anothers(self, env):
        env["client"]("admin@tenant-acme").put(
            "/api/purchase-orders", json={"purchase_orders": [po("PO-ACME")]}
        )
        other = (
            env["client"]("admin@tenant-globex")
            .get("/api/purchase-orders")
            .json()
        )
        assert other["count"] == 0

    def test_one_tenant_cannot_overwrite_anothers(self, env):
        acme = env["client"]("admin@tenant-acme")
        acme.put(
            "/api/purchase-orders", json={"purchase_orders": [po("PO-ACME")]}
        )

        env["client"]("admin@tenant-globex").put(
            "/api/purchase-orders", json={"purchase_orders": [po("PO-GLOBEX")]}
        )
        still = acme.get("/api/purchase-orders").json()
        assert still["purchase_orders"][0]["po_number"] == "PO-ACME"

    def test_each_tenant_gets_its_own_file(self, env):
        env["client"]("admin@tenant-acme").put(
            "/api/purchase-orders", json={"purchase_orders": [po()]}
        )
        env["client"]("admin@tenant-globex").put(
            "/api/purchase-orders", json={"purchase_orders": [po()]}
        )
        files = sorted(
            p.name for p in (env["tmp"] / "purchase_orders").glob("*.json")
        )
        assert files == ["tenant-acme.json", "tenant-globex.json"]

    def test_a_write_never_lands_in_the_shared_legacy_file(
        self, env, monkeypatch
    ):
        """The legacy fallback is a read convenience; writing through it would
        let one tenant overwrite the file another reads."""
        legacy = env["tmp"] / "legacy.json"
        legacy.write_text(json.dumps([po("PO-LEGACY")]))
        monkeypatch.setattr(paths, "PURCHASE_ORDER_FILE", legacy)

        env["client"]("admin@tenant-acme").put(
            "/api/purchase-orders", json={"purchase_orders": [po("PO-NEW")]}
        )
        assert json.loads(legacy.read_text())[0]["po_number"] == "PO-LEGACY"


class TestConnectorStatus:
    """ "Why is Export to Xero not working?" should not need a terminal."""

    def test_reports_xero_as_unconfigured(self, env, monkeypatch):
        monkeypatch.delenv("XERO_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("XERO_TENANT_ID", raising=False)
        body = env["client"]().get("/api/settings").json()
        assert body["integrations"]["xero"]["configured"] is False

    def test_reports_xero_as_configured(self, env, monkeypatch):
        monkeypatch.setenv("XERO_ACCESS_TOKEN", "tok")
        monkeypatch.setenv("XERO_TENANT_ID", "xt")
        body = env["client"]().get("/api/settings").json()
        assert body["integrations"]["xero"]["configured"] is True

    def test_half_configured_is_not_configured(self, env, monkeypatch):
        """One of the two is a setup someone abandoned halfway, and export
        fails at the point of use rather than here."""
        monkeypatch.setenv("XERO_ACCESS_TOKEN", "tok")
        monkeypatch.delenv("XERO_TENANT_ID", raising=False)
        body = env["client"]().get("/api/settings").json()
        assert body["integrations"]["xero"]["configured"] is False

    def test_the_token_never_leaves_the_server(self, env, monkeypatch):
        """The indicator is a boolean. A UI has no use for the credential and
        every reason not to hold it."""
        monkeypatch.setenv("XERO_ACCESS_TOKEN", "super-secret-token")
        monkeypatch.setenv("XERO_TENANT_ID", "xt")
        assert (
            "super-secret-token"
            not in env["client"]().get("/api/settings").text
        )

    def test_csv_is_always_available(self, env):
        """It needs no credential, so it is the answer when Xero is not set up."""
        body = env["client"]().get("/api/settings").json()
        assert body["integrations"]["csv"]["configured"] is True
