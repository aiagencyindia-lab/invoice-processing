"""What would be payable had the finding not happened. Decimal only; the
delta is always what the engine priced (its `impact`), so the benchmark's
impact error is the engine's, not a second opinion."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from ..findings.model import HOLD_TYPES
from .model import Counterfactual

NO_MONEY = "no money is computed for this finding"
OVERBILLED = (
    "LEAK-RATE",
    "LEAK-ESCALATOR",
    "LEAK-TIER",
    "LEAK-REBATE",
    "LEAK-DISCOUNT",
)
PAY_ONCE = (
    "DUP-NEAR",
    "DUP-CROSS",
    "STMT-DOUBLE-PAID",
    "STMT-UNAPPLIED-CREDIT",
)


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        out = Decimal(str(value))
    except InvalidOperation:
        return None
    return out if out.is_finite() else None


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(Decimal("0.01")))


def _formula(
    code: str, d: dict, impact: Decimal, amount: Decimal | None
) -> str:
    g = d.get
    if code in ("LEAK-RATE", "LEAK-ESCALATOR"):
        return (
            f"({g('unit_price')} - {g('allowed_unit_price')}) x {g('quantity')}"
            f" = {_money(impact)}"
        )
    if code == "LEAK-TIER":
        return (
            f"({g('unit_price')} - {g('tier_price')}) x {g('quantity')}"
            f" = {_money(impact)}"
        )
    if code == "LEAK-REBATE":
        return f"{g('period_spend')} x {g('pct')}% = {_money(impact)}"
    if code == "LEAK-DISCOUNT":
        # The engine prices amount x pct / 100; `paid` can exceed the amount.
        return f"{_money(amount) or 'amount'} x {g('pct')}% = {_money(impact)}"
    if code == "STMT-DOUBLE-PAID":
        return f"paid {g('paid')} - invoiced = {_money(impact)}"
    if code == "GST-ITC-RISK":
        return f"tax x {g('factor')} = {_money(impact)}"
    if code == "PAY-ANOMALY":
        return f"amount - median {g('median')} = {_money(impact)}"
    return f"impact = {_money(impact)}"


def counterfactual(finding: dict, txn: dict | None) -> Counterfactual:
    code, currency = finding["type"], finding.get("currency")
    impact = _dec(finding.get("impact"))
    if impact is None:
        return Counterfactual(currency, None, None, None, "", NO_MONEY)
    amount = _dec(((txn or {}).get("attrs") or {}).get("amount"))
    details = finding.get("details") or {}
    formula = _formula(code, details, impact, amount)
    if code in OVERBILLED:
        return Counterfactual(
            currency,
            _money(amount),
            _money(amount - impact) if amount is not None else None,
            _money(impact),
            formula,
            "payable once the overbilled amount is short-paid or credited",
        )
    if code in PAY_ONCE:
        return Counterfactual(
            currency,
            _money(impact),
            _money(Decimal(0)),
            _money(impact),
            formula,
            "a credit to apply, not an amount to pay"
            if code == "STMT-UNAPPLIED-CREDIT"
            else "pay once: nothing is payable on the later copy",
        )
    if code == "GST-ITC-RISK":
        note = "ITC claimable if the vendor is active and files"
    elif code in HOLD_TYPES:
        note = "amount at risk if paid now; nothing changes until the hold is cleared"
    elif code == "PAY-ANOMALY":
        note = "amount above the vendor's median; no hold applies"
    else:
        note = "informational; nothing is withheld"
    return Counterfactual(
        currency, _money(amount), _money(amount), _money(impact), formula, note
    )
