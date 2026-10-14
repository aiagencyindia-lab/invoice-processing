"""Engine modes and provider choices are tenant settings, off and fake by default."""

import pytest

from invoice_processing import tenant_settings
from invoice_processing.tenant_settings import SettingsError, validate
from invoice_processing.trust import modes


class Store:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, tenant_id):
        return self.data.get(tenant_id)


def test_engine_mode_defaults_to_off():
    assert modes.engine_mode("t", "leakage", store=Store()) == "off"


def test_engine_mode_reads_the_setting():
    store = Store({"t": {"trust_engine_modes": {"leakage": "shadow"}}})
    assert modes.engine_mode("t", "leakage", store=store) == "shadow"
    assert modes.engine_mode("t", "payment", store=store) == "off"


def test_unknown_engine_or_capability_is_refused():
    with pytest.raises(ValueError, match="No such engine"):
        modes.engine_mode("t", "x", store=Store())
    with pytest.raises(ValueError, match="No such capability"):
        modes.provider_name("t", "telepathy", store=Store())


def test_trust_engine_modes_rejects_unknown_engine():
    with pytest.raises(SettingsError, match="No such engine"):
        validate({"trust_engine_modes": {"x": "off"}})


def test_trust_engine_modes_rejects_unknown_mode():
    with pytest.raises(SettingsError, match="must be one of"):
        validate({"trust_engine_modes": {"leakage": "on"}})


def test_trust_providers_rejects_unknown_capability():
    with pytest.raises(SettingsError, match="No such capability"):
        validate({"trust_providers": {"telepathy": "fake"}})


def test_provider_name_defaults_to_fake():
    assert (
        modes.provider_name("t", "bank_verification", store=Store()) == "fake"
    )


def test_settings_are_settable_and_described():
    for key in ("trust_engine_modes", "trust_providers"):
        assert key in tenant_settings.SETTABLE
        assert key in tenant_settings.DESCRIPTIONS
        assert key in tenant_settings.NON_ENGINE_DEFAULTS
