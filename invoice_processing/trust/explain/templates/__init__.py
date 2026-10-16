"""Deterministic templates, one per finding type, keyed by code.

A template is `fn(ctx) -> dict` with any of `what_happened`, `why`
(Steps), `caused_by` (Actors), `next_action`, `would_change`,
`confidence_note`; the engine fills the rest. A template can only point at
what `ctx` offers: the finding's cited nodes and its transaction node, so
FR-24.2 holds by construction and `Explanation.check()` is the backstop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..model import Actor, Step

TEMPLATES: dict[str, Callable] = {}


def register(*codes: str):
    def deco(fn):
        for code in codes:
            TEMPLATES[code] = fn
        return fn

    return deco


@dataclass
class Context:
    finding: dict
    nodes: dict[str, dict | None]  # cited ids and the transaction, in order
    vendor: dict | None = None  # {node, name} or None
    approvers: tuple[str, ...] = ()

    txn: str = field(init=False)
    details: dict = field(init=False)
    cited: tuple[str, ...] = field(init=False)
    excerpts: dict[str, str | None] = field(init=False)

    def __post_init__(self):
        self.txn = self.finding["transaction_node"]
        self.details = dict(self.finding.get("details") or {})
        cites = self.finding.get("citations") or []
        self.cited = tuple(dict.fromkeys(c["node_id"] for c in cites))
        self.excerpts = {}
        for c in cites:
            self.excerpts.setdefault(c["node_id"], c.get("excerpt"))

    # -- what a template may point at ---------------------------------------

    def of_kind(self, kind: str) -> list[str]:
        """Cited node ids of a kind, in citation order."""
        return [
            n
            for n in self.cited
            if (self.nodes.get(n) or {}).get("kind") == kind
        ]

    def first(self, kind: str, default: str | None = None) -> str:
        found = self.of_kind(kind)
        return found[0] if found else (default or self.txn)

    def cited_from(self, key: str) -> list[str]:
        """Ids listed under `details[key]` that the finding also cited."""
        ids = self.details.get(key)
        if not isinstance(ids, list):
            return []
        return [i for i in ids if i in self.cited]

    def others(self) -> list[str]:
        """Every cited node but the transaction, in citation order."""
        return [n for n in self.cited if n != self.txn]

    def step(self, text: str, node_id: str | None = None) -> Step:
        return Step(text, node_id or self.txn)

    def attr(self, node_id: str | None, key: str, default=None):
        node = self.nodes.get(node_id) if node_id else None
        return ((node or {}).get("attrs") or {}).get(key, default)

    def detail(self, key: str, default="?"):
        value = self.details.get(key)
        return default if value in (None, "") else value

    def excerpt(self, node_id: str) -> str | None:
        return self.excerpts.get(node_id)

    @property
    def money(self) -> str:
        impact, cur = self.finding.get("impact"), self.finding.get("currency")
        return (
            f"{cur or ''} {impact}".strip() if impact else "an unpriced amount"
        )

    @property
    def vendor_name(self) -> str:
        return (self.vendor or {}).get("name") or "the vendor"

    def vendor_actor(self, label: str) -> Actor:
        """The vendor, pointed at only when the finding cited its node."""
        node = (self.vendor or {}).get("node")
        return Actor("vendor", label, node if node in self.cited else None)

    def engine_actor(self) -> Actor:
        from ...findings.model import TYPE_ENGINE  # noqa: PLC0415

        engine = TYPE_ENGINE.get(self.finding["type"], "trust")
        return Actor(
            "engine",
            f"{engine} engine {self.finding.get('engine_version') or ''}".strip(),
        )

    def person_actors(self) -> list[Actor]:
        return [
            Actor("person", f"{who} (approver on record)")
            for who in self.approvers
        ]


from . import generic, gst_itc, leakage, payment, provenance  # noqa: E402
