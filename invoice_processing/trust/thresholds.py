"""Every engine threshold, with its default.

Declared once here so four engines built in parallel never edit the settings
module; a tenant overrides any of them through the `trust_thresholds` setting.
"""

from __future__ import annotations

DEFAULTS: dict[str, float] = {
    # Leakage (FR-20)
    "leakage.desc_match": 0.6,
    "leakage.dup_window_days": 7,
    "leakage.split_window_days": 3,
    "leakage.rate_tolerance_pct": 0.5,
    "leakage.clause_min_confidence": 0.7,
    "leakage.renewal_notice_days": 60,
    "leakage.max_pages": 40,
    # Payment Trust (FR-21)
    "payment.amount_zscore": 3.0,
    "payment.min_history": 5,
    "payment.cadence_ratio_min": 0.25,
    "payment.name_match_min": 0.8,
    "payment.bec_hold_signals": 2,
    "payment.lookalike_ratio": 0.8,
    "payment.bank_change_recent_days": 30,
    # Provenance v1 (FR-22)
    "provenance.suspect_below": 0.5,
    "provenance.template_min_history": 3,
    # GST/ITC v1 (FR-26)
    "gst.nonfiler_missed_periods": 2,
    "gst.new_vendor_days": 90,
    "gst.first_invoice_multiple": 3,
    "gst.risk_inactive": 1.0,
    "gst.risk_nonfiler": 0.5,
    "gst.risk_shared": 0.3,
    "gst.risk_new": 0.2,
    # Reality checks (FR-23)
    "reality.qty_tolerance_pct": 0.02,
    "reality.hours_tolerance_pct": 0.10,
    "reality.seats_tolerance_pct": 0.05,
    "reality.evidence_window_days": 45,
    "reality.price_jump_zscore": 3.0,
    "reality.volume_zscore": 3.0,
    "reality.min_history": 5,
    # Agent control (FR-28.4): behavioural detection windows
    "agents.velocity_window_minutes": 10,
    "agents.velocity_max": 20,
    "agents.split_window_days": 7,
    "agents.split_min_count": 2,
    "agents.injection_min_hits": 1,
    # Wave 5: the shell banner's window, and how long the ledger keeps rows.
    "agents.banner_window_hours": 24,
    "agents.retention_days": 400,
    # Provenance v2 (FR-22)
    "provenance.edits_suspect": 2,
    # Mode progression: what the readiness report asks before recommending
    "readiness.min_days": 14,
    "readiness.min_findings": 20,
    "readiness.min_decided": 10,
    "readiness.min_precision": 0.8,
}


def merged(overrides: dict | None) -> dict[str, float]:
    return {
        **DEFAULTS,
        **{k: v for k, v in (overrides or {}).items() if k in DEFAULTS},
    }


def value(tenant_id: str, name: str, store=None) -> float:
    from .. import tenant_settings  # noqa: PLC0415 - avoids an import cycle

    if name not in DEFAULTS:
        raise ValueError(f"No such threshold: {name}")
    setting = tenant_settings.effective(tenant_id, store).get(
        "trust_thresholds"
    )
    return merged(setting)[name]
