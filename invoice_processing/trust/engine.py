"""What an engine gets: `run(ectx: EngineContext) -> list[FindingSpec]`.

An engine is a package `invoice_processing/trust/<name>/` (name in
`modes.ENGINES`) exposing a module-level `run`. It reads the graph, may ask the
model through `ectx.decider` and external systems through `ectx.providers`, and
returns findings; `evaluate()` stamps the mode and stores them. Engines are
mode-agnostic: whether a hold gates is decided by the tenant's mode, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..ports import RunContext
from . import thresholds as _thresholds
from .graph.transactions import Transaction, transactions


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class EngineContext:
    ctx: RunContext
    # off | shadow | advisory | enforcing (never "off" inside an engine)
    mode: str
    # id, kind, sha256, storage_key, source, source_ref, sender, reply_to,
    # filename; None for a batch (tenant-wide) run
    document: dict | None
    # the invoice node this document projects to; None for contracts and batch
    invoice_node: str | None
    graph: Any
    links: Any
    findings: Any
    providers: Any
    settings: dict
    decider: Any
    # .get(key) -> bytes, the tenant's blob store
    blobs: Any
    url: str | None
    now: datetime = field(default_factory=_now)
    # Shared across the engines of one evaluate() call: one tenant scan, not four.
    cache: dict = field(default_factory=dict, repr=False)

    def threshold(self, name: str) -> float:
        if name not in _thresholds.DEFAULTS:
            raise ValueError(f"No such threshold: {name}")
        return _thresholds.merged(self.settings.get("trust_thresholds"))[name]

    def transactions(self, as_of: datetime | None = None) -> list[Transaction]:
        if as_of is not None:
            return transactions(
                self.ctx.tenant_id, graph=self.graph, as_of=as_of
            )
        if "transactions" not in self.cache:
            self.cache["transactions"] = transactions(
                self.ctx.tenant_id, graph=self.graph
            )
        return self.cache["transactions"]
