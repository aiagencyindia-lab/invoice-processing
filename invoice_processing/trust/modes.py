"""Per-tenant switches: which engines run, how hard, and which providers."""

from __future__ import annotations

from datetime import datetime, timezone

ENGINES = ("leakage", "payment", "provenance", "gst_itc", "reality")
# Only `enforcing` places holds; the rest observe (spec §3.1.5).
MODES = ("off", "shadow", "advisory", "enforcing")
# Engines that can hold payment: they fail closed even when their package
# never imported far enough to register its hold types.
HOLD_ENGINES = frozenset({"payment", "provenance", "gst_itc", "reality"})
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
    "usage",
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


# Written only by `set_engine_mode`, never by PUT /api/settings: it is when an
# engine entered its current mode, the clock the readiness report reads.
SINCE_KEY = "trust_engine_mode_since"


def _store(store):
    if store is None:
        from ..api import service  # noqa: PLC0415

        store = service.get_tenant_settings()
    return store


def mode_since(tenant_id: str, engine: str, store=None) -> datetime | None:
    at = ((_store(store).get(tenant_id) or {}).get(SINCE_KEY) or {}).get(engine)
    return datetime.fromisoformat(at) if at else None


def set_engine_mode(
    ctx,
    engine: str,
    mode: str,
    *,
    store=None,
    audit=None,
    now: datetime | None = None,
    reason: str = "",
    holds=None,
    url: str | None = None,
) -> dict:
    """Move one engine to `mode` for this tenant: the only way modes change.

    Stamps when the engine entered the mode, audits the change, and when it
    crosses `enforcing` in either direction has `holds.reconcile` place or
    lift that engine's holds (`holds` defaults to the deployment's ledger;
    none is reconciled where there is no ledger table). An unchanged mode
    writes nothing new and is not audited."""
    if engine not in ENGINES:
        raise ValueError(f"No such engine: {engine}")
    if mode not in MODES:
        raise ValueError(f"{engine} mode must be one of {', '.join(MODES)}.")
    store = _store(store)
    now = now or datetime.now(timezone.utc)
    current = dict(store.get(ctx.tenant_id) or {})
    modes = dict(current.get("trust_engine_modes") or {})
    was = modes.get(engine, "off")
    modes[engine] = mode
    since = dict(current.get(SINCE_KEY) or {})
    if was != mode or engine not in since:
        since[engine] = now.isoformat()
    store.put(ctx.tenant_id, {"trust_engine_modes": modes, SINCE_KEY: since})
    if was == mode:
        return {
            "engine": engine,
            "from": was,
            "to": mode,
            "since": since[engine],
        }
    if audit is not None:
        audit.emit(
            ctx,
            "trust_engine_mode_changed",
            {"engine": engine, "from": was, "to": mode, "reason": reason},
        )
    if "enforcing" in (was, mode):
        if holds is None:
            from ..api import service  # noqa: PLC0415

            holds = service.get_hold_store()  # None without the ledger table
        if holds is not None:
            holds.reconcile(ctx, engine, enforcing=mode == "enforcing", url=url)
    return {"engine": engine, "from": was, "to": mode, "since": since[engine]}
