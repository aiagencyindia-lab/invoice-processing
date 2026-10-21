"""Engine modes and provider choices are tenant settings, off and fake by default."""

import pytest

from invoice_processing import tenant_settings
from invoice_processing.tenant_settings import SettingsError, validate
from invoice_processing.trust import modes, thresholds


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


THRESHOLDS = {
    "leakage.desc_match": 0.6,
    "leakage.dup_window_days": 7,
    "leakage.split_window_days": 3,
    "leakage.rate_tolerance_pct": 0.5,
    "leakage.clause_min_confidence": 0.7,
    "leakage.renewal_notice_days": 60,
    "leakage.max_pages": 40,
    "payment.amount_zscore": 3.0,
    "payment.min_history": 5,
    "payment.cadence_ratio_min": 0.25,
    "payment.name_match_min": 0.8,
    "payment.bec_hold_signals": 2,
    "payment.lookalike_ratio": 0.8,
    "payment.bank_change_recent_days": 30,
    "provenance.suspect_below": 0.5,
    "provenance.template_min_history": 3,
    "gst.nonfiler_missed_periods": 2,
    "gst.new_vendor_days": 90,
    "gst.first_invoice_multiple": 3,
    "gst.risk_inactive": 1.0,
    "gst.risk_nonfiler": 0.5,
    "gst.risk_shared": 0.3,
    "gst.risk_new": 0.2,
    "reality.qty_tolerance_pct": 0.02,
    "reality.hours_tolerance_pct": 0.10,
    "reality.seats_tolerance_pct": 0.05,
    "reality.evidence_window_days": 45,
    "reality.price_jump_zscore": 3.0,
    "reality.volume_zscore": 3.0,
    "reality.min_history": 5,
    "provenance.edits_suspect": 2,
    "readiness.min_days": 14,
    "readiness.min_findings": 20,
    "readiness.min_decided": 10,
    "readiness.min_precision": 0.8,
}


def test_threshold_defaults_cover_every_name():
    assert thresholds.DEFAULTS == THRESHOLDS


def test_trust_thresholds_rejects_unknown_name_and_bool():
    with pytest.raises(SettingsError, match="No such threshold"):
        validate({"trust_thresholds": {"leakage.nope": 1}})
    with pytest.raises(SettingsError, match="must be a number"):
        validate({"trust_thresholds": {"payment.min_history": True}})
    with pytest.raises(SettingsError, match="must be a number"):
        validate({"trust_thresholds": {"payment.min_history": float("nan")}})


def test_trust_thresholds_merges_over_defaults():
    store = Store({"t": {"trust_thresholds": {"payment.min_history": 9}}})
    assert thresholds.value("t", "payment.min_history", store=store) == 9
    assert thresholds.value("t", "payment.amount_zscore", store=store) == 3.0
    assert validate({"trust_thresholds": {"gst.risk_new": 0.25}}) == {
        "trust_thresholds": {"gst.risk_new": 0.25}
    }
    assert "trust_thresholds" in tenant_settings.SETTABLE
    assert "trust_thresholds" in tenant_settings.DESCRIPTIONS


def test_bec_window_is_a_payment_threshold():
    assert thresholds.DEFAULTS["payment.bank_change_recent_days"] == 30
