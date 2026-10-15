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


def register_type(code: str, description: str) -> None:
    TYPES[code] = description


register_type(
    "BENCH-IMPORT",
    "Benchmark harness: a record the import round-trip could not link",
)
