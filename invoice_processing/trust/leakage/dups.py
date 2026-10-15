"""Duplicates v1 cannot see: near numbers, split invoices, one vendor twice."""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal

from ...duplicates import normalize_number
from ..findings.model import FindingSpec
from .contracts import ENGINE_VERSION
from .view import View, cents, cite

NEAR_SAME_NUMBER, NEAR_EDIT, CROSS, SPLIT = 0.9, 0.7, 0.9, 0.5


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(
                min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            )
        prev = cur
    return prev[-1]


def _spec(code, view, later, earlier, impact, conf, summary, details):
    return FindingSpec(
        type=code,
        transaction_node=later.node_id,
        dedupe_key=f"{code}:{view.nodes[later.node_id]['natural_key']}",
        impact=None if impact is None else cents(impact),
        currency=(later.currency or "INR") if impact is not None else None,
        confidence=conf,
        engine_version=ENGINE_VERSION,
        rule_version=f"FR-20.{code.lower()}-1",
        model_version="none",
        summary=summary,
        details=details,
        citations=tuple(
            cite(view.nodes[t.node_id], t.doc_number) for t in (later, *earlier)
        ),
    )


def _same_record(a, b) -> bool:
    """One invoice seen live and in an imported history is not a duplicate."""
    return (
        a.source != b.source
        and a.doc_number == b.doc_number
        and a.amount == b.amount
        and a.date == b.date
    )


def findings(ectx, view: View) -> list[FindingSpec]:
    window = int(ectx.threshold("leakage.dup_window_days"))
    groups = defaultdict(list)
    for t in ectx.transactions():
        if t.node_id in view.nodes and t.date is not None and t.vendor_key:
            groups[view.canon(t.vendor_key)].append(t)
    out: list[FindingSpec] = []
    for txns in groups.values():
        txns.sort(key=lambda t: (t.date, view.nodes[t.node_id]["natural_key"]))
        out += _near_and_cross(view, txns, window)
        out += _split(ectx, view, txns)
    return out


_CORE = re.compile(r"([A-Z]*)(\d+)([A-Z]*)")


def _sequential(na: str, nb: str) -> bool:
    """INV1041 and INV1042: the vendor's next invoice, not a duplicate."""
    ma, mb = _CORE.fullmatch(na), _CORE.fullmatch(nb)
    return bool(
        ma
        and mb
        and ma.group(1) == mb.group(1)
        and ma.group(3) == mb.group(3)
        and abs(int(ma.group(2)) - int(mb.group(2))) <= 10
    )


def _near_and_cross(view, txns, window) -> list[FindingSpec]:
    """`txns` is date-sorted: near matches walk back only through the
    window; cross matches are a lookup by (number, amount).

    ponytail: a vendor with thousands of invoices inside one window is still
    quadratic in that window; bucket by number prefix if that ever happens.
    """
    out = []
    numbers = [normalize_number(t.doc_number) for t in txns]
    seen: dict[tuple, list] = defaultdict(list)
    for j, b in enumerate(txns):
        nb = numbers[j]
        if not nb:
            continue
        cross = [
            a
            for a in seen.get((nb, b.amount), [])
            if a.vendor_key != b.vendor_key and not _same_record(a, b)
        ]
        if b.amount is not None:
            seen[(nb, b.amount)].append(b)
        near = []
        i = j - 1
        while not cross and i >= 0 and (b.date - txns[i].date).days <= window:
            a, na = txns[i], numbers[i]
            i -= 1
            if not na or _same_record(a, b):
                continue
            same_amount = a.amount is not None and a.amount == b.amount
            if na == nb or (
                same_amount
                and not _sequential(na, nb)
                and edit_distance(na, nb) <= 2
            ):
                near.append((a, na == nb))
        near.reverse()
        if cross:
            out.append(
                _spec(
                    "DUP-CROSS",
                    view,
                    b,
                    cross,
                    b.amount,
                    CROSS,
                    "The same invoice is recorded under two records of one vendor.",
                    {"matches": [a.node_id for a in cross]},
                )
            )
        elif near:
            exact = any(same for _, same in near)
            out.append(
                _spec(
                    "DUP-NEAR",
                    view,
                    b,
                    [a for a, _ in near],
                    b.amount,
                    NEAR_SAME_NUMBER if exact else NEAR_EDIT,
                    "A near-duplicate of an earlier invoice from this vendor.",
                    {
                        "matches": [a.node_id for a, _ in near],
                        "same_number": exact,
                    },
                )
            )
    return out


def _split(ectx, view, txns) -> list[FindingSpec]:
    """Invoices close together, each under the high-value line, together
    over it. Off while the tenant's `high_value_threshold` is 0."""
    high = ectx.settings.get("high_value_threshold") or 0
    if not high or high <= 0:
        return []
    high = Decimal(str(high))
    days = int(ectx.threshold("leakage.split_window_days"))
    out, run = [], []
    for t in txns:
        if t.amount is None or t.amount <= 0 or t.amount >= high:
            run = []
            continue
        run = [r for r in run if (t.date - r.date).days <= days] + [t]
        total = sum((r.amount for r in run), Decimal(0))
        if len(run) >= 2 and total >= high:
            out.append(
                _spec(
                    "DUP-SPLIT",
                    view,
                    t,
                    run[:-1],
                    None,
                    SPLIT,
                    "Invoices split to stay under the high-value threshold.",
                    {
                        "total": str(total),
                        "threshold": str(high),
                        "invoices": [r.node_id for r in run],
                    },
                )
            )
            run = []
    return out
