"""trust.evaluate: sync what a document added to the graph, then run every
engine whose per-tenant mode is not `off`.

Engines are found by convention (`invoice_processing.trust.<name>.run`, for
each name in `modes.ENGINES`); a missing package is skipped, so the runtime
ships before any engine does. Mode semantics live here, once (spec §3.1.5):
shadow findings are written and marked; advisory ones are surfaced; in
enforcing, hold-type findings gate approval and export (see `gate.py`).
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select

from ..db.models import Document, Invoice
from ..db.session import tenant_session
from ..llm_guard import CallBudget
from ..ports import RunContext
from .engine import EngineContext
from .findings import model as finding_model
from .findings.model import Citation, FindingSpec
from .findings.store import FindingStore
from .graph.sync import sync_tenant
from .modes import ENGINES

logger = logging.getLogger(__name__)

# name -> run(ectx); filled lazily by discovery, injectable by tests.
RUNNERS: dict[str, Callable[[EngineContext], list[FindingSpec]]] = {}
ENGINE_FAILED = finding_model.ENGINE_FAILED
# A sync job's model calls are capped by the decider; one evaluation of one
# document gets the same budget for all four engines together.
MAX_MODEL_CALLS_PER_EVALUATION = 25


def _runner(name: str):
    if name in RUNNERS:
        return RUNNERS[name]
    module_name = f"{__package__}.{name}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise  # the engine exists but one of its imports does not
        return None
    run = getattr(module, "run", None)
    if run is not None:
        RUNNERS[name] = run
    return run


def _document(s, tenant_id: str, document_id: str) -> dict | None:
    row = s.scalars(
        select(Document).where(
            Document.tenant_id == tenant_id, Document.id == document_id
        )
    ).first()
    if row is None:
        return None
    return {
        "id": row.id,
        "kind": row.kind,
        "sha256": row.sha256,
        "storage_key": row.storage_key,
        "source": row.source,
        "source_ref": row.source_ref,
        "sender": row.sender,
        "reply_to": row.reply_to,
        "filename": row.filename,
    }


def _owns_holds(name: str) -> bool:
    return any(
        finding_model.TYPE_ENGINE.get(code) == name
        for code in finding_model.HOLD_TYPES
    )


def _engine_failed(name: str, node: str, version: str) -> FindingSpec:
    return FindingSpec(
        type=ENGINE_FAILED,
        transaction_node=node,
        dedupe_key=f"{ENGINE_FAILED}:{name}:{node}",
        impact=None,
        currency=None,
        confidence=1.0,
        engine_version=version,
        rule_version="contract-3.1.4",
        model_version="none",
        summary=f"The {name} engine failed; payment is held until it runs.",
        details={"engine": name, "mode": "enforcing"},
        citations=(Citation(node, f"{name} engine failure"),),
    )


def _retire_engine_failed(ectx: EngineContext, name: str) -> None:
    node = ectx.invoice_node
    if node is None:
        return
    for f in ectx.findings.list(
        ectx.ctx.tenant_id, type=ENGINE_FAILED, transaction_node=node
    ):
        if f["dedupe_key"] != f"{ENGINE_FAILED}:{name}:{node}":
            continue
        if f["status"] == "detected":
            ectx.findings.transition(
                ectx.ctx, f["id"], "triaged", actor="system"
            )
            f["status"] = "triaged"
        if f["status"] == "triaged":
            ectx.findings.transition(
                ectx.ctx,
                f["id"],
                "rejected",
                actor="system",
                reason="The engine ran cleanly.",
            )


def _default_providers(graph):
    from .providers import fakes  # noqa: PLC0415
    from .providers.registry import Registry  # noqa: PLC0415

    registry = Registry(graph)
    fakes.install(registry)
    return registry


def _default_decider():
    from .decide import GeminiDecider  # noqa: PLC0415

    return GeminiDecider(
        budget=CallBudget(max_calls=MAX_MODEL_CALLS_PER_EVALUATION)
    )


def _default_blobs(tenant_id: str):
    from ..api import service  # noqa: PLC0415

    return service.get_blobs(tenant_id)


def evaluate(
    ctx: RunContext,
    document_id: str | None,
    *,
    graph,
    links,
    url: str | None = None,
    settings_store=None,
    findings=None,
    providers=None,
    decider=None,
    blobs=None,
    now: datetime | None = None,
) -> dict:
    from .. import tenant_settings  # noqa: PLC0415 - avoids an import cycle

    document = None
    invoice_node = None
    counts: dict = {}
    if document_id is not None:
        with tenant_session(ctx.tenant_id, url) as s:
            document = _document(s, ctx.tenant_id, document_id)
            invoice_id = s.scalars(
                select(Invoice.id).where(
                    Invoice.tenant_id == ctx.tenant_id,
                    Invoice.document_id == document_id,
                )
            ).first()
        if document is None:
            return {"skipped": "no document"}
        if invoice_id is not None:
            # ponytail: an incremental sync still loads every vendor and PO
            # node to match against; index those by key in the graph if a
            # tenant's masters pass ~50k rows.
            counts = sync_tenant(
                ctx, graph, links, url=url, document_id=document_id
            )
            invoice_node = graph.nodes_by_ref(
                ctx.tenant_id, "invoices", [invoice_id]
            ).get(invoice_id)

    settings = tenant_settings.effective(ctx.tenant_id, settings_store)
    modes = settings.get("trust_engine_modes") or {}
    if all(modes.get(name, "off") == "off" for name in ENGINES):
        # The default for every tenant: nothing to build, nothing to run.
        return {"synced": counts, "engines": dict.fromkeys(ENGINES, "off")}
    base = EngineContext(
        ctx=ctx,
        mode="off",
        document=document,
        invoice_node=invoice_node,
        graph=graph,
        links=links,
        findings=findings or FindingStore(url),
        providers=providers or _default_providers(graph),
        settings=settings,
        decider=decider or _default_decider(),
        blobs=blobs if blobs is not None else _default_blobs(ctx.tenant_id),
        url=url,
        **({"now": now} if now is not None else {}),
    )

    report: dict = {}
    for name in ENGINES:
        mode = modes.get(name, "off")
        if mode == "off":
            report[name] = "off"
            continue
        run = _runner(name)
        if run is None:
            logger.info("trust_engine_missing", extra={"engine": name})
            report[name] = "missing"
            continue
        ectx = dataclasses.replace(base, mode=mode)
        try:
            specs = list(run(ectx) or [])
        except Exception as exc:
            logger.warning(
                "trust_engine_failed",
                extra={"engine": name, "error": type(exc).__name__},
            )
            report[name] = "failed"
            # Fail closed for an engine that can hold payment (§3.1.4): with
            # nothing to read its verdict from, the invoice is held instead.
            if mode == "enforcing" and _owns_holds(name) and invoice_node:
                base.findings.upsert(
                    ctx, _engine_failed(name, invoice_node, f"{name}-?")
                )
            continue
        for spec in specs:
            base.findings.upsert(
                ctx,
                dataclasses.replace(
                    spec, details={**spec.details, "mode": mode}
                ),
            )
        if mode == "enforcing" and _owns_holds(name):
            _retire_engine_failed(ectx, name)
        report[name] = {"mode": mode, "findings": len(specs)}
    return {"synced": counts, "engines": report}
