"""Agent control (FR-28, Wave 4, built dark): a registry of machine actors,
two-person mandates, and a policy check every governed write asks first.

Not a per-invoice engine: actions are judged when they happen (the guard on
the governed routes, or an imported log), so `run` finds nothing. The engine
exists for its mode, which the guard reads: `off` changes nothing anywhere.
"""

from __future__ import annotations

from ..findings.model import register_type
from ..types import TABLE

ENGINE = "agents"
ENGINE_VERSION = "agents-1"
ACTIONS = (
    "vendor.create",
    "vendor.patch",
    "bank_change.propose",
    "bank_change.decide",
    "invoice.approve",
    "invoice.reject",
    "invoice.export",
    # Wave 5: governed on the PO import and the document uploads.
    "po.create",
    "document.upload",
)
DECISIONS = ("ALLOW", "ALLOW-WITH-REVIEW", "BLOCK")
# dryrun = a question ("would this be allowed?"): never counted, never a
# finding. log = a past action read from an imported log.
SOURCES = ("inline", "log", "dryrun")

for _code, _t in TABLE.items():
    if _t.engine == ENGINE:
        register_type(
            _code,
            _t.description,
            engine=ENGINE,
            hold=_t.hold,
            dimension=_t.dimension,
        )


def run(ectx) -> list:
    return []
