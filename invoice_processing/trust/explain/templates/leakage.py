"""Leakage (FR-20): contract rates, rebates, discounts, duplicates,
statements and the renewal radar. Reads `details` as the engine writes it;
anything missing falls back to the summary wording."""

from __future__ import annotations

from . import Context, register

VENDOR_PRICING = "{} (likely a price list not updated to the contract)"


def _clauses(ctx: Context) -> list[str]:
    return ctx.cited_from("clauses") or ctx.of_kind("contract_clause")


def _contract(ctx: Context) -> str:
    return ctx.first("contract")


def _clause_step(ctx: Context, text: str, index: int = 0):
    clauses = _clauses(ctx)
    node = clauses[index] if len(clauses) > index else _contract(ctx)
    excerpt = ctx.excerpt(node)
    return ctx.step(f"{text}" + (f' ("{excerpt}")' if excerpt else ""), node)


@register("LEAK-RATE", "LEAK-ESCALATOR", "LEAK-TIER")
def overbilled(ctx: Context) -> dict:
    d, code = ctx.detail, ctx.finding["type"]
    line = ctx.first("invoice_line")
    allowed = (
        d("tier_price") if code == "LEAK-TIER" else d("allowed_unit_price")
    )
    if code == "LEAK-RATE":
        first = _clause_step(
            ctx, f"The contract rate is {d('contract_rate')} per unit"
        )
    elif code == "LEAK-ESCALATOR":
        first = _clause_step(
            ctx,
            f"The contract rate {d('contract_rate')} escalates to {allowed} by "
            "the invoice date",
            1,
        )
    else:
        first = _clause_step(
            ctx,
            f"The tier price {allowed} applies once {d('period_quantity')} units "
            "were bought this contract period",
            1,
        )
    return {
        "what_happened": ctx.finding["summary"]
        + f" {ctx.vendor_name} billed {d('unit_price')} against {allowed}.",
        "why": [
            first,
            ctx.step(
                f"This line billed {d('quantity')} x {d('unit_price')}"
                + (
                    f" for {ctx.attr(line, 'description')}"
                    if ctx.attr(line, "description")
                    else ""
                )
                + ".",
                line,
            ),
            ctx.step(
                f"Over by {ctx.money}: ({d('unit_price')} - {allowed}) x {d('quantity')}."
            ),
        ],
        "caused_by": [
            ctx.vendor_actor(VENDOR_PRICING.format(ctx.vendor_name)),
            *ctx.person_actors(),
            ctx.engine_actor(),
        ],
        "next_action": f"Short-pay {ctx.money} or raise a claim with {ctx.vendor_name}.",
        "would_change": [
            "A reviewed clause with a different rate, tier or escalation.",
            "A credit note or statement credit covering the difference.",
            "A line description that does not match the cited clause.",
        ],
    }


@register("LEAK-REBATE")
def rebate(ctx: Context) -> dict:
    d = ctx.detail
    return {
        "why": [
            _clause_step(
                ctx,
                f"The contract pays a {d('pct')}% rebate above {d('threshold')}",
            ),
            ctx.step(
                f"Spend since {d('period_start')} reached {d('period_spend')} with this invoice."
            ),
            ctx.step(f"No credit covering the {ctx.money} rebate has arrived."),
        ],
        "caused_by": [
            ctx.vendor_actor(
                f"{ctx.vendor_name} (likely never issued the rebate credit)"
            ),
            ctx.engine_actor(),
        ],
        "next_action": f"Claim the {ctx.money} rebate from {ctx.vendor_name}.",
        "would_change": [
            "A statement credit or credit note for the rebate.",
            "A reviewed clause with a different threshold or percentage.",
        ],
    }


@register("LEAK-DISCOUNT")
def discount(ctx: Context) -> dict:
    d = ctx.detail
    payments = ctx.cited_from("payments") or ctx.of_kind("payment")
    terms = _clauses(ctx)
    why = [
        ctx.step(
            f"Terms give {d('pct')}% off if paid within {d('days')} days.",
            terms[0] if terms else ctx.txn,
        )
    ]
    why += [
        ctx.step(f"Paid {d('paid')} inside the window.", p)
        for p in payments[:3]
    ]
    why.append(ctx.step(f"The discount of {ctx.money} was not taken."))
    return {
        "why": why,
        "caused_by": [
            ctx.vendor_actor(
                f"{ctx.vendor_name} (likely invoiced the full amount)"
            ),
            *ctx.person_actors(),
            ctx.engine_actor(),
        ],
        "next_action": f"Claim the {ctx.money} discount or deduct it from the next payment.",
        "would_change": [
            "A payment dated outside the discount window.",
            "A credit note for the discount already received.",
        ],
    }


@register("LEAK-TERM")
def term(ctx: Context) -> dict:
    d = ctx.detail
    return {
        "why": [
            _clause_step(ctx, "The contract term is"),
            ctx.step(f"This invoice is dated {d('invoice_date')}, outside it."),
        ],
        "caused_by": [ctx.vendor_actor(ctx.vendor_name), ctx.engine_actor()],
        "next_action": "Confirm which contract governs this invoice before rating its lines.",
        "would_change": [
            "A renewal or a reviewed term clause covering the date."
        ],
    }


@register("RADAR-RENEWAL", "RADAR-ESCALATOR")
def radar(ctx: Context) -> dict:
    d, code = ctx.detail, ctx.finding["type"]
    what = (
        f"The term ends on {d('on')}."
        if code == "RADAR-RENEWAL"
        else f"Prices escalate by {d('pct')}% on {d('on')}."
    )
    return {
        "why": [
            _clause_step(ctx, what),
            ctx.step("That date is inside the notice window."),
        ],
        "caused_by": [ctx.engine_actor()],
        "next_action": "Decide whether to renegotiate, renew or let the contract lapse before the date.",
        "would_change": [
            "A reviewed clause with another date.",
            "A newer contract.",
        ],
    }


@register("DUP-NEAR", "DUP-CROSS", "DUP-SPLIT")
def duplicate(ctx: Context) -> dict:
    code = ctx.finding["type"]
    earlier = (
        ctx.cited_from("matches") or ctx.cited_from("invoices") or ctx.others()
    )
    earlier = [n for n in earlier if n != ctx.txn]
    this_no = (
        ctx.attr(ctx.txn, "invoice_number")
        or ctx.excerpt(ctx.txn)
        or "this invoice"
    )
    why = [
        ctx.step(
            f"Earlier invoice {ctx.attr(n, 'invoice_number') or ctx.excerpt(n) or n} "
            f"for {ctx.attr(n, 'amount') or '?'} dated {ctx.attr(n, 'invoice_date') or '?'}.",
            n,
        )
        for n in earlier[:5]
    ]
    if code == "DUP-SPLIT":
        why.append(
            ctx.step(
                f"Together with {this_no} they total {ctx.detail('total')}, over the "
                f"{ctx.detail('threshold')} threshold each stays under."
            )
        )
        next_action = "Hold the later invoices and ask why the work was split."
    else:
        same = (
            "the same number"
            if ctx.details.get("same_number") or code == "DUP-CROSS"
            else "a near-identical number"
        )
        why.append(
            ctx.step(
                f"{this_no} carries {same} and the same amount "
                f"{ctx.attr(ctx.txn, 'amount') or ''}".strip()
                + "."
            )
        )
        next_action = (
            f"Hold the later invoice; pay once ({ctx.money} at stake)."
        )
    return {
        "why": why,
        "caused_by": [
            ctx.vendor_actor(
                f"{ctx.vendor_name} (likely re-sent or re-keyed the invoice)"
            ),
            ctx.engine_actor(),
        ],
        "next_action": next_action,
        "would_change": [
            "A credit note cancelling one of the copies.",
            "Goods receipts showing two distinct deliveries.",
        ],
    }


@register("STMT-UNAPPLIED-CREDIT", "STMT-DISPUTED", "STMT-DOUBLE-PAID")
def statement(ctx: Context) -> dict:
    code, d = ctx.finding["type"], ctx.detail
    if code == "STMT-DOUBLE-PAID":
        payments = ctx.cited_from("payments") or ctx.of_kind("payment")
        why = [
            ctx.step(
                f"The invoice is for {ctx.attr(ctx.txn, 'amount') or '?'}."
            )
        ]
        why += [
            ctx.step(
                f"Payment {ctx.attr(p, 'doc_number') or ''} of {ctx.attr(p, 'amount') or '?'} "
                f"on {ctx.attr(p, 'invoice_date') or '?'}.".replace("  ", " "),
                p,
            )
            for p in payments[:5]
        ]
        why.append(ctx.step(f"Paid {d('paid')} in all: {ctx.money} too much."))
        return {
            "why": why,
            "caused_by": [*ctx.person_actors(), ctx.engine_actor()],
            "next_action": f"Recover {ctx.money} from {ctx.vendor_name} or offset the next invoice.",
            "would_change": ["A payment that settles a different invoice."],
        }
    if code == "STMT-UNAPPLIED-CREDIT":
        return {
            "why": [
                ctx.step(
                    f"The vendor's statement carries credit {d('doc_number')} ({ctx.excerpt(ctx.txn) or ''}).".replace(
                        " ()", ""
                    )
                ),
                ctx.step(
                    f"No invoice or payment of ours applies it: {ctx.money} unapplied."
                ),
            ],
            "caused_by": [
                ctx.vendor_actor(ctx.vendor_name),
                ctx.engine_actor(),
            ],
            "next_action": f"Apply the {ctx.money} credit against the next payment to {ctx.vendor_name}.",
            "would_change": ["A payment that already netted the credit."],
        }
    return {
        "why": [
            ctx.step(
                f"The vendor's statement lists {d('doc_number')} ({ctx.excerpt(ctx.txn) or ''}).".replace(
                    " ()", ""
                )
            ),
            ctx.step("We have no invoice with that number from this vendor."),
        ],
        "caused_by": [ctx.vendor_actor(ctx.vendor_name), ctx.engine_actor()],
        "next_action": "Ask the vendor for the invoice before accepting the statement line.",
        "would_change": ["An invoice of ours with that number."],
    }
