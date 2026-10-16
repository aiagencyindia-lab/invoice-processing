"""One batch run of the engines over a tenant's whole history (FR-29.2).

The worker calls `run_batch`; everything it needs to know about the run is
in the run row. The tenant's engine modes are not changed: a requested
engine that is `off` runs as `advisory` for this run only (decision 7), and
an engine the run did not ask for sits out.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ...ports import RunContext
from ..evaluate import evaluate
from ..modes import ENGINES
from .store import TrustAuditStore

logger = logging.getLogger(__name__)


class lifted:
    """A tenant settings store whose engine modes are the batch run's."""

    def __init__(self, inner, engines: list[str]):
        self.inner = inner
        self.engines = set(engines) or set(ENGINES)

    def get(self, tenant_id: str) -> dict:
        base = dict(self.inner.get(tenant_id) or {})
        modes = dict(base.get("trust_engine_modes") or {})
        for name in ENGINES:
            if name not in self.engines:
                modes[name] = "off"
            elif modes.get(name, "off") == "off":
                modes[name] = "advisory"
        return {**base, "trust_engine_modes": modes}


def run_batch(
    ctx: RunContext,
    run_id: str,
    *,
    graph,
    links,
    url: str | None = None,
    store: TrustAuditStore | None = None,
    findings=None,
    settings_store=None,
    now: datetime | None = None,
) -> dict | None:
    """queued|failed -> running -> done (per_engine = the report) or failed
    (error kept, exception re-raised so the worker retries). A done run is a
    no-op; a running one is run again, idempotent by dedupe_key."""
    store = store or TrustAuditStore(url)
    run = store.get_run(ctx.tenant_id, run_id)
    if run is None:
        raise KeyError(run_id)
    if store.start(ctx.tenant_id, run_id, now=now) is None:
        return None
    if settings_store is None:
        from ...api import service  # noqa: PLC0415

        settings_store = service.get_tenant_settings()
    try:
        out = evaluate(
            ctx,
            None,
            graph=graph,
            links=links,
            url=url,
            settings_store=lifted(settings_store, run["engines"]),
            findings=findings,
            on_progress=store.progress(ctx.tenant_id, run_id),
            **({"now": now} if now is not None else {}),
        )
    except Exception as exc:
        logger.warning(
            "trust_audit_run_failed", extra={"error": type(exc).__name__}
        )
        store.finish(
            ctx.tenant_id,
            run_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            now=now,
        )
        raise
    store.finish(
        ctx.tenant_id,
        run_id,
        status="done",
        per_engine=out.get("engines") or {},
        now=now,
    )
    return out
