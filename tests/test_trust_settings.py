"""Engine modes and provider choices are tenant settings, off and fake by default."""

from datetime import UTC, datetime

import pytest

from invoice_processing import tenant_settings
from invoice_processing.ports import RunContext
from invoice_processing.tenant_settings import SettingsError, validate
from invoice_processing.trust import modes, thresholds


class Store:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, tenant_id):
        return self.data.get(tenant_id)

    def put(self, tenant_id, settings):
        self.data[tenant_id] = {**self.data.get(tenant_id, {}), **settings}
        return self.data[tenant_id]


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
    "agents.velocity_window_minutes": 10,
    "agents.velocity_max": 20,
    "agents.split_window_days": 7,
    "agents.split_min_count": 2,
    "agents.injection_min_hits": 1,
    "agents.banner_window_hours": 24,
    "agents.retention_days": 400,
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


# -- mode changes (Wave 3) --------------------------------------------------


T0 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
CTX = RunContext(tenant_id="t", actor="admin@x.test")


class Audit:
    def __init__(self):
        self.events = []

    def emit(self, ctx, kind, payload):
        self.events.append((kind, payload))


class Holds:
    def __init__(self):
        self.calls = []

    def reconcile(self, ctx, engine, *, enforcing, url=None):
        self.calls.append((engine, enforcing))
        return {"placed": 0, "lifted": 0}


def test_a_mode_change_stamps_its_clock_and_is_audited():
    store, audit = Store(), Audit()
    out = modes.set_engine_mode(
        CTX,
        "payment",
        "shadow",
        store=store,
        audit=audit,
        now=T0,
        reason="pilot",
    )
    assert out == {
        "engine": "payment",
        "from": "off",
        "to": "shadow",
        "since": T0.isoformat(),
    }
    assert modes.engine_mode("t", "payment", store=store) == "shadow"
    assert modes.mode_since("t", "payment", store=store) == T0
    assert modes.mode_since("t", "leakage", store=store) is None
    assert audit.events == [
        (
            "trust_engine_mode_changed",
            {
                "engine": "payment",
                "from": "off",
                "to": "shadow",
                "reason": "pilot",
            },
        )
    ]


def test_holds_are_reconciled_only_across_enforcing():
    store, holds = Store(), Holds()
    for mode in ("shadow", "advisory", "enforcing", "enforcing", "advisory"):
        modes.set_engine_mode(
            CTX, "payment", mode, store=store, now=T0, holds=holds
        )
    assert holds.calls == [("payment", True), ("payment", False)]


def test_a_mode_change_keeps_the_other_engines():
    store = Store({"t": {"trust_engine_modes": {"leakage": "shadow"}}})
    modes.set_engine_mode(CTX, "payment", "advisory", store=store, now=T0)
    assert store.data["t"]["trust_engine_modes"] == {
        "leakage": "shadow",
        "payment": "advisory",
    }


def test_a_mode_change_refuses_an_unknown_engine_or_mode():
    with pytest.raises(ValueError, match="No such engine"):
        modes.set_engine_mode(CTX, "x", "shadow", store=Store())
    with pytest.raises(ValueError, match="mode"):
        modes.set_engine_mode(CTX, "payment", "loud", store=Store())


def test_the_mode_clock_is_not_settable_by_hand():
    with pytest.raises(SettingsError, match="Not settable"):
        validate({"trust_engine_mode_since": {"payment": T0.isoformat()}})


def test_shutdown_periods_are_pairs_of_dates_in_order():
    assert validate(
        {"trust_shutdown_periods": [["2026-12-24", "2027-01-02"]]}
    ) == {"trust_shutdown_periods": [["2026-12-24", "2027-01-02"]]}
    for bad in (["2026-12-24"], [["2027-01-02", "2026-12-24"]], [["x", "y"]]):
        with pytest.raises(SettingsError, match="shutdown"):
            validate({"trust_shutdown_periods": bad})


def test_a_shutdown_period_may_be_one_line_of_the_settings_form():
    # The form edits a list as one line per item; a pair renders "a,b".
    assert validate(
        {
            "trust_shutdown_periods": [
                "2026-12-24, 2027-01-02",
                "2027-03-01,2027-03-02",
            ]
        }
    ) == {
        "trust_shutdown_periods": [
            ["2026-12-24", "2027-01-02"],
            ["2027-03-01", "2027-03-02"],
        ]
    }


def test_a_mode_change_reconciles_the_ledger_by_default(monkeypatch):
    from invoice_processing.api import service  # noqa: PLC0415

    holds = Holds()
    monkeypatch.setattr(service, "get_hold_store", lambda: holds)
    store = Store({"t": {"trust_engine_modes": {"payment": "advisory"}}})
    modes.set_engine_mode(CTX, "payment", "enforcing", store=store, now=T0)
    assert holds.calls == [("payment", True)]


def test_without_a_ledger_a_mode_change_still_succeeds(monkeypatch):
    from invoice_processing.api import service  # noqa: PLC0415

    monkeypatch.setattr(service, "get_hold_store", lambda: None)
    out = modes.set_engine_mode(
        CTX, "payment", "enforcing", store=Store(), now=T0
    )
    assert out["to"] == "enforcing"


def test_an_unchanged_mode_is_not_audited_or_reclocked():
    store, audit = Store(), Audit()
    modes.set_engine_mode(
        CTX, "payment", "shadow", store=store, audit=audit, now=T0
    )
    later = T0.replace(day=22)
    out = modes.set_engine_mode(
        CTX, "payment", "shadow", store=store, audit=audit, now=later
    )
    assert len(audit.events) == 1 and out["since"] == T0.isoformat()
