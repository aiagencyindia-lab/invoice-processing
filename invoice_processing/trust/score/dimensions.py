"""The six dimension rules (FR-25): pure, no database, no graph.

Each dimension is `{value, hold, drivers, note}`. `value` is in [0, 1] or
None when nothing could be measured; `drivers` are the open findings of that
dimension; `hold` is whether one of them is holding payment (an open
hold-type finding whose engine is enforcing). Any open hold-type finding puts
its dimension at 0.0, so a held invoice never shows a healthy dimension.
"Open" is the gate's: every status but `rejected`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..findings import model as finding_model
from ..modes import BLOCKING_ENGINES
from ..types import DIMENSIONS, TABLE

# The engines whose findings speak to each dimension: with all of them off
# and nothing found, the dimension is unmeasured, not clean. Agent control
# is left out: authorization is measured from approvals whether or not it
# runs, and its findings still show as drivers.
FEEDS: dict[str, frozenset[str]] = {
    d: frozenset(
        t.engine
        for t in TABLE.values()
        if t.dimension == d and t.engine not in BLOCKING_ENGINES
    )
    for d in DIMENSIONS
}
INSIGHT_SEVERITY = 0.4
REALITY = {
    "CORROBORATED": 1.0,
    "PARTIAL": 0.6,
    "NO_EVIDENCE": 0.4,
    "CONTRADICTED": 0.0,
}
PRICE_LEAKS = frozenset(
    {"LEAK-RATE", "LEAK-ESCALATOR", "LEAK-TIER", "LEAK-DISCOUNT", "LEAK-REBATE"}
)
PRICE_JUMP_VALUE = 0.3
VOLUME_SPIKE_CAP = 0.4
ANOMALY_STEP = 0.5
OVERRIDDEN = 0.7
PENDING = 0.5


@dataclass
class Inputs:
    findings: list[dict]
    amount: Decimal | None = None
    # approval nodes' attrs: {status, decided_by}
    approvals: list[dict] = field(default_factory=list)
    # the workflow's decision (approve/reject) when there is no chain
    decision: str | None = None
    overridden: bool = False
    # a live (v1) invoice, rather than one imported from history
    live: bool = True
    # the reality_check node's attrs, the document_version node's attrs
    reality: dict | None = None
    document: dict | None = None
    has_contract: bool = False
    has_history: bool = False


def is_open(f: dict) -> bool:
    return f["status"] != "rejected"


def engine_of(f: dict) -> str | None:
    return (f.get("details") or {}).get(
        "engine"
    ) or finding_model.TYPE_ENGINE.get(f["type"])


def is_hold(f: dict) -> bool:
    return bool(
        (f.get("details") or {}).get(
            "hold", f["type"] in finding_model.HOLD_TYPES
        )
    )


def dimension_of(f: dict) -> str | None:
    return finding_model.TYPE_DIMENSION.get(f["type"])


def holds(findings: list[dict], enforcing: set[str]) -> list[str]:
    """The open findings holding payment, of any dimension."""
    return sorted(
        f["id"]
        for f in findings
        if is_open(f) and is_hold(f) and engine_of(f) in enforcing
    )


def _identity(fs, i):
    if not fs:
        return 1.0, "No identity finding is open."
    return 1.0 - INSIGHT_SEVERITY, "An identity insight is open."


def _authorization(fs, i):
    if i.approvals:
        statuses = {a.get("status") for a in i.approvals}
        if "rejected" in statuses:
            return 0.0, "An approver rejected the invoice."
        decided = {a.get("decided_by") for a in i.approvals} - {None}
        if "pending" in statuses or not decided:
            return PENDING, "Approval is pending."
    elif i.decision == "reject":
        return 0.0, "The invoice was rejected."
    elif i.decision != "approve":
        if not i.live:
            return None, "No approval record (imported history)."
        return PENDING, "Approval is pending."
    if i.overridden:
        return OVERRIDDEN, "Approved over a payment hold, with a reason."
    return 1.0, "Approved."


def _reality(fs, i):
    if i.reality is None:
        return None, "No reality check on record."
    result = i.reality.get("result")
    if result not in REALITY:
        return None, f"Unknown reality result {result!r}."
    return REALITY[result], f"Reality check: {result}."


def _price(fs, i):
    leaks = [f for f in fs if f["type"] in PRICE_LEAKS]
    if i.has_contract or leaks:
        total = sum(
            (Decimal(f["impact"]) for f in leaks if f.get("impact")),
            Decimal(0),
        )
        if not total:
            return 1.0, "Priced as the contract allows."
        if not i.amount or i.amount <= 0:
            return 0.0, "A contract leak on an invoice with no amount."
        share = min(Decimal(1), total / i.amount)
        return round(float(1 - share), 4), "Priced above the contract."
    if any(f["type"] == "REAL-PRICE-JUMP" for f in fs):
        return PRICE_JUMP_VALUE, "No contract; price far above history."
    if i.has_history:
        return 1.0, "No contract; in line with the vendor's history."
    return None, "No baseline: no contract and no history."


def _authenticity(fs, i):
    if any(f["type"] == "PROV-TAMPER" for f in fs):
        return 0.0, "Re-submitted with different document bytes."
    score = (i.document or {}).get("score")
    if score is None:
        return None, "The document has not been scored."
    return round(float(score), 4), "Document authenticity score."


def _consistency(fs, i):
    signals = sum(
        len((f.get("details") or {}).get("signals") or {})
        for f in fs
        if f["type"] == "PAY-ANOMALY"
    )
    value = max(0.0, 1.0 - ANOMALY_STEP * signals)
    note = (
        f"{signals} anomaly signal(s)." if signals else "Usual for the vendor."
    )
    if any(f["type"] == "REAL-VOLUME-SPIKE" for f in fs):
        value, note = min(value, VOLUME_SPIKE_CAP), "Volume far above usual."
    return value, note


RULES = {
    "identity": _identity,
    "authorization": _authorization,
    "reality": _reality,
    "price": _price,
    "authenticity": _authenticity,
    "consistency": _consistency,
}


def dimensions(
    inputs: Inputs, *, enforcing: set[str], running: set[str]
) -> dict[str, dict]:
    """`enforcing`: engines in enforcing mode; `running`: engines not off."""
    open_ = [f for f in inputs.findings if is_open(f)]
    out = {}
    for dim in DIMENSIONS:
        fs = [f for f in open_ if dimension_of(f) == dim]
        if FEEDS[dim] and not FEEDS[dim] & running and not fs:
            value, note = None, "No engine that measures this is on."
        else:
            value, note = RULES[dim](fs, inputs)
        held = [f for f in fs if is_hold(f)]
        if held:
            value, note = 0.0, "A hold-type finding is open."
        out[dim] = {
            "value": value,
            "hold": any(engine_of(f) in enforcing for f in held),
            "drivers": sorted(f["id"] for f in fs),
            "note": note,
        }
    return out


def composite(dims: dict[str, dict]) -> float | None:
    """The mean of the measured dimensions. Advisory: nothing reads it to
    hold payment."""
    known = [d["value"] for d in dims.values() if d["value"] is not None]
    return round(sum(known) / len(known), 4) if known else None
