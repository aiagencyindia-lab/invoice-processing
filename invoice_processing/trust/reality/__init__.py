"""Reality checks (FR-23): is what was invoiced corroborated by deliveries,
hours or seats on record? The v2/w3-reality branch fills `run`."""

from __future__ import annotations

from ..findings.model import register_type
from ..types import TABLE

ENGINE = "reality"
ENGINE_VERSION = "reality-0"

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
