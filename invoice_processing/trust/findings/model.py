"""What a finding is and which status changes are legal."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

STATUSES = (
    "detected",
    "triaged",
    "accepted",
    "rejected",
    "needs_vendor_input",
    "claimed",
    "recovered",
    "prevented",
    "written_off",
)
TRANSITIONS: dict[str, frozenset[str]] = {
    "detected": frozenset({"triaged"}),
    "triaged": frozenset({"accepted", "rejected", "needs_vendor_input"}),
    "needs_vendor_input": frozenset({"accepted", "rejected"}),
    "accepted": frozenset({"claimed", "prevented"}),
    "claimed": frozenset({"recovered", "prevented", "written_off"}),
    "rejected": frozenset(),
    "recovered": frozenset(),
    "prevented": frozenset(),
    "written_off": frozenset(),
}
# Once a human has ruled, an engine re-run may not touch the finding.
DECIDED = frozenset(
    {
        "accepted",
        "rejected",
        "needs_vendor_input",
        "claimed",
        "recovered",
        "prevented",
        "written_off",
    }
)
REASON_REQUIRED = frozenset({"rejected", "written_off"})


class IllegalTransition(ValueError):
    pass


def can_transition(a: str, b: str) -> bool:
    return b in TRANSITIONS.get(a, frozenset())


@dataclass(frozen=True)
class Citation:
    node_id: str
    excerpt: str | None = None


@dataclass(frozen=True)
class FindingSpec:
    type: str
    transaction_node: str
    dedupe_key: str
    impact: Decimal | None
    currency: str | None
    confidence: float | None
    engine_version: str
    rule_version: str
    model_version: str
    summary: str
    details: dict = field(default_factory=dict)
    citations: tuple[Citation, ...] = ()


# Each engine registers the finding types it emits; the registry is what the
# UI and the eval harness enumerate.
TYPES: dict[str, str] = {}
# Which engine owns each type (its per-tenant mode decides whether a hold
# gates), which types are holds rather than insights, and the trust dimension
# each speaks to.
TYPE_ENGINE: dict[str, str] = {}
HOLD_TYPES: set[str] = set()
TYPE_DIMENSION: dict[str, str | None] = {}


def register_type(
    code: str,
    description: str,
    *,
    engine: str = "bench",
    hold: bool = False,
    dimension: str | None = None,
) -> None:
    TYPES[code] = description
    TYPE_ENGINE[code] = engine
    if hold:
        HOLD_TYPES.add(code)
    else:
        HOLD_TYPES.discard(code)
    # An engine's own call may omit the dimension; the static table set it.
    TYPE_DIMENSION[code] = dimension or TYPE_DIMENSION.get(code)


register_type(
    "BENCH-IMPORT",
    "Benchmark harness: a record the import round-trip could not link",
)
ENGINE_FAILED = "TRUST-ENGINE-FAILED"


def _register_table() -> None:
    # Here, not where each type is written, so every process that asks the
    # payment gate knows which types hold (the API server never imports
    # the engines).
    from ..types import TABLE  # noqa: PLC0415

    for code, t in TABLE.items():
        register_type(
            code,
            t.description,
            engine=t.engine,
            hold=t.hold,
            dimension=t.dimension,
        )


_register_table()
