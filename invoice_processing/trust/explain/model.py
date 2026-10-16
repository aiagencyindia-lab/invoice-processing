"""What an explanation is, and the rule that keeps it honest: every step,
actor and piece of evidence points at a node the finding itself cited."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

EXPLAIN_VERSION = "explain-1"


class Uncited(ValueError):
    """A template named a node the finding never cited (FR-24.2)."""


@dataclass(frozen=True)
class Step:
    text: str
    node_id: str


@dataclass(frozen=True)
class Actor:
    kind: str  # vendor | person | system | engine | provider
    label: str
    node_id: str | None = None


@dataclass(frozen=True)
class Evidence:
    node_id: str
    kind: str
    label: str
    excerpt: str | None
    source: dict | None


@dataclass(frozen=True)
class Counterfactual:
    currency: str | None
    payable_now: str | None
    payable_correct: str | None
    delta: str | None
    formula: str
    note: str | None


@dataclass(frozen=True)
class Explanation:
    finding_id: str
    type: str
    version: str
    what_happened: str
    why: tuple[Step, ...]
    caused_by: tuple[Actor, ...]
    evidence: tuple[Evidence, ...]
    confidence: float | None
    confidence_note: str
    next_action: str
    would_change: tuple[str, ...]
    counterfactual: Counterfactual | None
    versions: dict = field(default_factory=dict)
    transaction_node: str = ""

    def check(self, cited) -> None:
        allowed = set(cited) | {self.transaction_node}
        if not self.evidence:
            raise Uncited(
                "An explanation needs at least one piece of evidence."
            )
        if not self.why:
            raise Uncited("An explanation needs at least one step.")
        for s in self.why:
            if not s.text.strip():
                raise Uncited("A step has no text.")
            if s.node_id not in allowed:
                raise Uncited(f"A step cites an uncited node: {s.node_id}")
        for a in self.caused_by:
            if a.node_id is not None and a.node_id not in allowed:
                raise Uncited(f"An actor cites an uncited node: {a.node_id}")
        for e in self.evidence:
            if e.node_id not in allowed:
                raise Uncited(f"Evidence cites an uncited node: {e.node_id}")

    def as_dict(self) -> dict:
        d = asdict(self)
        d["why"], d["caused_by"], d["evidence"] = (
            list(d["why"]),
            list(d["caused_by"]),
            list(d["evidence"]),
        )
        d["would_change"] = list(d["would_change"])
        return d
