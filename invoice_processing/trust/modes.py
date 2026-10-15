"""Per-tenant switches: which engines run, how hard, and which providers."""

from __future__ import annotations

ENGINES = ("leakage", "payment", "provenance", "gst_itc")
# Only `enforcing` places holds; the rest observe (spec §3.1.5).
MODES = ("off", "shadow", "advisory", "enforcing")
# Engines that can hold payment: they fail closed even when their package
# never imported far enough to register its hold types.
HOLD_ENGINES = frozenset({"payment", "provenance", "gst_itc"})
CAPABILITIES = (
    "bank_verification",
    "gstin",
    "company_registry",
    "abn",
    "vat",
    "mailbox",
    "tickets",
    "timesheets",
    "logistics",
)


def engine_mode(tenant_id: str, engine: str, store=None) -> str:
    from .. import tenant_settings  # noqa: PLC0415 - avoids an import cycle

    if engine not in ENGINES:
        raise ValueError(f"No such engine: {engine}")
    setting = (
        tenant_settings.effective(tenant_id, store).get("trust_engine_modes")
        or {}
    )
    return setting.get(engine, "off")


def provider_name(tenant_id: str, capability: str, store=None) -> str:
    from .. import tenant_settings  # noqa: PLC0415

    if capability not in CAPABILITIES:
        raise ValueError(f"No such capability: {capability}")
    setting = (
        tenant_settings.effective(tenant_id, store).get("trust_providers") or {}
    )
    return setting.get(capability, "fake")
