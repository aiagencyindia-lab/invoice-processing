"""The recovery ledger of an engagement and its contingency fee (FR-29.5).

Money is Decimal end to end; every figure leaves as a string quantized to a
cent, ROUND_HALF_UP, so the JSON and the CSV carry exactly what was computed.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
# The statuses that move money, in the order the ledger lists them.
LEDGER_STATUSES = ("claimed", "recovered", "prevented", "written_off")


def money(value) -> str:
    return str(Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP))


def rows(scope, events: list[dict]) -> list[dict]:
    """`scope` maps finding_id -> vendor name (None when unknown); a plain
    set or list of ids is accepted too. Events outside the scope or that
    moved no money are dropped; the order is the events' own."""
    vendors = scope if isinstance(scope, dict) else dict.fromkeys(scope)
    return [
        {
            "finding_id": e["finding_id"],
            "type": e["type"],
            "vendor": vendors[e["finding_id"]],
            "to_status": e["to_status"],
            "amount": None if e.get("amount") is None else money(e["amount"]),
            "attested_by": e["actor"],
            "at": e.get("at"),
            "reason": e.get("reason"),
        }
        for e in events
        if e["finding_id"] in vendors and e["to_status"] in LEDGER_STATUSES
    ]


def totals(ledger_rows: list[dict]) -> dict[str, str]:
    sums = dict.fromkeys(
        ("recovered", "prevented", "written_off", "claimed"), Decimal(0)
    )
    for r in ledger_rows:
        if r["amount"] is not None:
            sums[r["to_status"]] += Decimal(r["amount"])
    return {k: money(v) for k, v in sums.items()}


def _pct(terms: dict, key: str) -> Decimal:
    return Decimal(str(terms.get(key) or 0)) / 100


def _opt(terms: dict, key: str) -> Decimal | None:
    value = terms.get(key)
    return None if value in (None, "") else Decimal(str(value))


def fee(terms: dict, recovered: Decimal, prevented: Decimal) -> dict[str, str]:
    """recovered * pct + prevented * pct, then the cap, then the minimum."""
    fee_recovered = (recovered * _pct(terms, "recovered_pct")).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    fee_prevented = (prevented * _pct(terms, "prevented_pct")).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    gross = fee_recovered + fee_prevented
    cap, minimum = _opt(terms, "cap"), _opt(terms, "minimum")
    capped = min(gross, cap) if cap is not None else gross
    due = max(capped, minimum) if minimum is not None else capped
    return {
        "fee_recovered": money(fee_recovered),
        "fee_prevented": money(fee_prevented),
        "gross": money(gross),
        "capped": money(capped),
        "due": money(due),
    }
