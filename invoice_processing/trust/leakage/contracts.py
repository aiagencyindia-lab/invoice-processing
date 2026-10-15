"""Contract vs invoice: rate, escalator, tier, rebate, term, discount, radar."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..findings.model import FindingSpec
from .view import SOURCE, View, cents, cite, day, money

ENGINE_VERSION = "leakage-1"
RULE_CONFIDENCE = 0.9
# A guessed (latest, not covering) contract is a weaker link than one whose
# term covers the invoice date.
LATEST_CONFIDENCE = 0.5
_STOP = {
    "the",
    "for",
    "and",
    "per",
    "unit",
    "units",
    "of",
    "is",
    "at",
    "inr",
    "rs",
    "rate",
    "price",
    "with",
    "to",
    "in",
    "a",
    "an",
    "be",
    "shall",
}


def tokens(text: str | None) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {
        w for w in words if len(w) > 1 and w not in _STOP and not w.isdigit()
    }


def overlap(line_desc: str | None, excerpt: str) -> float:
    """Share of the line's words the clause names."""
    words = tokens(line_desc)
    return len(words & tokens(excerpt)) / len(words) if words else 0.0


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:  # 29 February
        return d.replace(year=d.year + n, day=28)


def years_between(start: date, d: date) -> int:
    return d.year - start.year - ((d.month, d.day) < (start.month, start.day))


@dataclass
class Contract:
    node: dict
    by_type: dict[str, list[dict]] = field(default_factory=dict)

    def first(self, kind: str) -> dict | None:
        found = self.by_type.get(kind) or []
        return found[0] if found else None

    @property
    def term(self) -> tuple[date | None, date | None]:
        c = self.first("term")
        if c is None:
            return None, None
        p = c["attrs"]["params"]
        return day(p.get("start")), day(p.get("end"))

    def covers(self, d: date) -> bool:
        start, end = self.term
        return bool(start and end and start <= d <= end)

    def period(self, d: date) -> tuple[date, date]:
        """The contract year holding `d` (calendar year without a term)."""
        start, _ = self.term
        if start is None:
            return date(d.year, 1, 1), date(d.year + 1, 1, 1)
        k = years_between(start, d)
        return add_years(start, k), add_years(start, k + 1)


def _conf(c: dict) -> float:
    return float(c["attrs"].get("confidence") or 0)


def contracts(view: View, ectx) -> dict[str, list[Contract]]:
    floor = ectx.threshold("leakage.clause_min_confidence")
    out: dict[str, list[Contract]] = {}
    for ven, nodes in view.contracts_of.items():
        for node in nodes:
            c = Contract(node)
            for cl in sorted(
                view.clauses.get(node["id"], []), key=_conf, reverse=True
            ):
                a = cl["attrs"]
                if not (a.get("reviewed") or _conf(cl) >= floor):
                    continue
                if not isinstance(a.get("params"), dict):
                    continue
                c.by_type.setdefault(a.get("type"), []).append(cl)
            out.setdefault(ven, []).append(c)
    return out


def governing(options: list[Contract], d: date) -> tuple[Contract, bool]:
    covering = [c for c in options if c.covers(d)]
    if covering:
        return covering[0], True

    def started(c: Contract):
        return (c.term[0] or date.min, c.node["valid_from"] or "")

    return max(options, key=started), False


def _spec(
    code,
    txn,
    key,
    impact,
    conf,
    summary,
    details,
    citations,
    rule,
    currency="INR",
) -> FindingSpec:
    return FindingSpec(
        type=code,
        transaction_node=txn,
        dedupe_key=f"{code}:{key}",
        impact=None if impact is None else cents(impact),
        currency=currency if impact is not None else None,
        confidence=round(conf, 4),
        engine_version=ENGINE_VERSION,
        rule_version=rule,
        model_version="decide" if details.get("clauses") else "none",
        summary=summary,
        details=details,
        citations=tuple(citations),
    )


def _clause_details(used: list[dict], extra: dict) -> dict:
    return {
        **extra,
        "clauses": [c["id"] for c in used],
        "needs_review": any(not c["attrs"].get("reviewed") for c in used),
    }


def _clause_conf(used: list[dict]) -> float:
    out = RULE_CONFIDENCE
    for c in used:
        # A reviewed clause with no score is as sure as the reviewer.
        out *= _conf(c) or (1.0 if c["attrs"].get("reviewed") else 0.0)
    return out


def _cites(inv, contract, used, line=None):
    out = [cite(inv), cite(contract.node)]
    if line is not None:
        out.append(cite(line, line["attrs"].get("description")))
    out += [cite(c, c["attrs"].get("excerpt")) for c in used]
    return out


def _link(ectx, inv, contract: Contract, covered: bool, term) -> None:
    ectx.graph.upsert_edge(
        ectx.ctx,
        src=inv["id"],
        dst=contract.node["id"],
        kind="governed_by",
        source_system=SOURCE,
        provenance="model_inference",
        confidence=_conf(term) if covered and term else LATEST_CONFIDENCE,
        observed_at=ectx.now,
        at=ectx.now,
    )


def _line_findings(ectx, inv, d, contract, lines, cum) -> list[FindingSpec]:
    """Rate, escalator and tier on each priced line of one invoice. `cum`
    carries the contract-year quantity of contract items seen so far."""
    tol = Decimal(str(ectx.threshold("leakage.rate_tolerance_pct"))) / 100
    match = ectx.threshold("leakage.desc_match")
    esc = contract.first("escalator")
    tier = contract.first("tier")
    start, _ = contract.term
    out = []
    for line in lines:
        a = line["attrs"]
        qty, price = money(a.get("quantity")), money(a.get("unit_price"))
        if qty is None or price is None or qty <= 0:
            continue
        scored = [
            (overlap(a.get("description"), r["attrs"].get("excerpt", "")), r)
            for r in contract.by_type.get("rate", [])
        ]
        scored = [s for s in scored if s[0] >= match]
        if not scored:
            continue
        rate_clause = max(scored, key=lambda s: s[0])[1]
        rate = money(rate_clause["attrs"]["params"].get("price"))
        if rate is None:
            continue
        period = contract.period(d)
        cum[period] = cum.get(period, Decimal(0)) + qty
        key = line["natural_key"]
        if tier is not None:
            tp = tier["attrs"]["params"]
            tier_qty, tier_price = money(tp.get("qty")), money(tp.get("price"))
            if (
                tier_qty is not None
                and tier_price is not None
                and cum[period] >= tier_qty
                and price > tier_price * (1 + tol)
            ):
                used = [rate_clause, tier]
                out.append(
                    _spec(
                        "LEAK-TIER",
                        inv["id"],
                        key,
                        (price - tier_price) * qty,
                        _clause_conf(used),
                        "Billed above the tier price after the tier quantity "
                        "was reached.",
                        _clause_details(
                            used,
                            {
                                "line": line["id"],
                                "quantity": str(qty),
                                "unit_price": str(price),
                                "tier_price": str(tier_price),
                                "period_quantity": str(cum[period]),
                            },
                        ),
                        _cites(inv, contract, used, line),
                        "FR-20.tier-1",
                    )
                )
                continue
        allowed, code, used = rate, "LEAK-RATE", [rate_clause]
        if esc is not None and start is not None:
            years = years_between(start, d)
            pct = money(esc["attrs"]["params"].get("pct"))
            if years > 0 and pct is not None:
                allowed = rate * (1 + pct / 100) ** years
                code, used = "LEAK-ESCALATOR", [rate_clause, esc]
        if price > allowed * (1 + tol):
            out.append(
                _spec(
                    code,
                    inv["id"],
                    key,
                    (price - allowed) * qty,
                    _clause_conf(used),
                    "Unit price above what the contract allows.",
                    _clause_details(
                        used,
                        {
                            "line": line["id"],
                            "quantity": str(qty),
                            "unit_price": str(price),
                            "contract_rate": str(rate),
                            "allowed_unit_price": str(cents(allowed)),
                        },
                    ),
                    _cites(inv, contract, used, line),
                    "FR-20.rate-1",
                )
            )
    return out


def _credits(view: View, ven: str, since: date) -> Decimal:
    total = Decimal(0)
    for s in view.of_kind["vendor_statement_line"]:
        amount = money(s["attrs"].get("amount"))
        d = day(s["attrs"].get("invoice_date"))
        if (
            view.vendor(s["id"]) == ven
            and amount is not None
            and amount < 0
            and (d is None or d >= since)
        ):
            total += -amount
    return total


def findings(ectx, view: View, in_scope) -> list[FindingSpec]:
    by_vendor = contracts(view, ectx)
    out: list[FindingSpec] = []
    cum: dict[tuple[str, tuple], dict] = {}
    spend: dict[tuple[str, tuple], Decimal] = {}
    for inv in view.invoices:
        ven = view.vendor(inv["id"])
        d = day(inv["attrs"].get("invoice_date"))
        if ven not in by_vendor or d is None:
            continue
        contract, covered = governing(by_vendor[ven], d)
        term = contract.first("term")
        if in_scope(inv["id"]):
            _link(ectx, inv, contract, covered, term)
        if term is not None and not covered:
            out.append(
                _spec(
                    "LEAK-TERM",
                    inv["id"],
                    inv["natural_key"],
                    None,
                    _clause_conf([term]),
                    "Invoice dated outside the contract term.",
                    _clause_details([term], {"invoice_date": d.isoformat()}),
                    _cites(inv, contract, [term]),
                    "FR-20.term-1",
                )
            )
        out += _line_findings(
            ectx,
            inv,
            d,
            contract,
            view.lines.get(inv["id"], []),
            cum.setdefault(contract.node["id"], {}),
        )
        out += _rebate(ectx, view, inv, d, ven, contract, spend)
    return out


def _rebate(ectx, view, inv, d, ven, contract, spend) -> list[FindingSpec]:
    rebate = contract.first("rebate")
    amount = money(inv["attrs"].get("amount"))
    if rebate is None or amount is None:
        return []
    p = rebate["attrs"]["params"]
    threshold, pct = money(p.get("threshold")), money(p.get("pct"))
    if threshold is None or pct is None:
        return []
    period = contract.period(d)
    key = (contract.node["id"], period)
    before = spend.get(key, Decimal(0))
    spend[key] = before + amount
    if not before < threshold <= spend[key]:
        return []  # only the invoice that crosses the threshold
    period_spend = sum(
        (
            money(i["attrs"].get("amount")) or Decimal(0)
            for i in view.invoices
            if view.vendor(i["id"]) == ven
            and (x := day(i["attrs"].get("invoice_date"))) is not None
            and period[0] <= x < period[1]
        ),
        Decimal(0),
    )
    due = period_spend * pct / 100
    if _credits(view, ven, period[0]) >= cents(due):
        return []
    return [
        _spec(
            "LEAK-REBATE",
            inv["id"],
            f"{contract.node['natural_key']}:{period[0].isoformat()}",
            due,
            _clause_conf([rebate]),
            "Spend passed the rebate threshold and no rebate credit arrived.",
            _clause_details(
                [rebate],
                {
                    "period_start": period[0].isoformat(),
                    "period_spend": str(period_spend),
                    "threshold": str(threshold),
                    "pct": str(pct),
                },
            ),
            _cites(inv, contract, [rebate]),
            "FR-20.rebate-1",
        )
    ]
