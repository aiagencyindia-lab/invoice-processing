"""Who must approve an invoice, and whether this person may.

Pure on purpose, like bank_check: the routing rules are the kind of thing an
auditor asks to see, and a function that takes rules in and gives a chain out
can be read, tested and explained without a database.

Only invoices a person must touch get a chain. The pipeline's AUTO routing
is the touchless rate a pilot is judged on; an amount tier is the one thing
that can pull an AUTO invoice back to a person.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from . import po_matching

ROLE_RANK = {"viewer": 0, "clerk": 1, "approver": 2, "admin": 3}


@dataclass(frozen=True)
class Assignee:
    kind: str
    value: str


@dataclass(frozen=True)
class StepSpec:
    assignee: Assignee
    source_rule_id: str | None


ROLE_FALLBACK = StepSpec(Assignee("role", "approver"), None)


def _email(value) -> str:
    return str(value or "").strip().casefold()


def _fold(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def match_entity(customer_name, organization_name) -> bool:
    """The test step 1.2 makes: letters and digits only, substring."""
    org = _fold(organization_name)
    return bool(org) and org in _fold(customer_name)


def parse_amount(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    # "nan" and "inf" parse, but no tier can be compared against them.
    return number if math.isfinite(number) else None


def _usable(rule) -> bool:
    """Whether the engine can read this stored rule.

    One malformed row must not stop routing for the whole tenant, so what
    cannot be read is skipped. Write-time validation is the first line of
    defence; this is the last.
    """
    if not isinstance(rule, dict) or not isinstance(rule.get("id"), str):
        return False
    if rule.get("kind") not in ("vendor", "entity", "amount"):
        return False
    if not isinstance(rule.get("match"), dict):
        return False
    steps = rule.get("steps")
    if not isinstance(steps, list) or not all(
        isinstance(s, dict)
        and isinstance(s.get("kind"), str)
        and isinstance(s.get("value"), str)
        for s in steps
    ):
        return False
    if rule["kind"] == "amount":
        floor = rule["match"].get("min_amount")
        return floor is None or parse_amount(floor) is not None
    return True


def _live(rules: list[dict]) -> list[dict]:
    return [r for r in rules if _usable(r) and r.get("enabled", True)]


def _floor(rule: dict) -> float:
    return parse_amount(rule["match"].get("min_amount")) or 0.0


def _amount_tier(invoice: dict, rules: list[dict]) -> dict | None:
    """The amount rule that applies, or None.

    A total that is missing or unreadable must not slip under every tier (an
    OCR'd "12,500.00" would otherwise route as touchless), so it takes the
    highest tier instead: the most senior approver looks at it.
    """
    total = parse_amount(invoice.get("invoice_total_inc_tax"))
    currency = str(invoice.get("currency") or "").upper()
    best = None
    # Earliest created wins a tie on the floor, then input order (strict >).
    for r in sorted(_live(rules), key=lambda r: str(r.get("created_at") or "")):
        if r["kind"] != "amount":
            continue
        wanted = str(r["match"].get("currency") or "").upper()
        # An unknown total with no currency either may be any tier.
        if wanted and wanted != currency and (total is not None or currency):
            continue
        reaches = total is None or total >= _floor(r)
        if reaches and (best is None or _floor(r) > _floor(best)):
            best = r
    return best


def needs_person(invoice: dict, rules: list[dict]) -> bool:
    if invoice.get("_routing") == "REVIEW":
        return True
    if _amount_tier(invoice, rules) is not None:
        return True
    # Unreadable total and amount rules exist, but none in this currency:
    # still a person, since nobody can say the invoice is under every tier.
    return parse_amount(invoice.get("invoice_total_inc_tax")) is None and any(
        r["kind"] == "amount" for r in _live(rules)
    )


def build_chain(invoice: dict, rules: list[dict]) -> list[StepSpec]:
    vendor_key = po_matching.normalize_vendor(invoice.get("vendor_name"))
    ordered = sorted(
        _live(rules),
        key=lambda r: str(r.get("created_at") or ""),
    )
    matched: list[dict] = []
    matched += [
        r
        for r in ordered
        if r["kind"] == "vendor"
        and po_matching.normalize_vendor(r["match"].get("vendor_name"))
        == vendor_key
        and vendor_key
    ]
    matched += [
        r
        for r in ordered
        if r["kind"] == "entity"
        and match_entity(
            invoice.get("customer_name"), r["match"].get("organization_name")
        )
    ]
    tier = _amount_tier(invoice, rules)
    if tier:
        matched.append(tier)

    chain: list[StepSpec] = []
    seen: set[tuple[str, str]] = set()
    for r in matched:
        for s in r.get("steps") or []:
            a = Assignee(str(s["kind"]), str(s["value"]))
            key = (a.kind, a.value.casefold())
            if key in seen:
                continue  # one person approves once, at their first position
            seen.add(key)
            chain.append(StepSpec(a, r["id"]))
    return chain or [ROLE_FALLBACK]


def satisfaction(
    email: str,
    role: str,
    step: dict,
    *,
    groups: dict[str, list[str]],
    delegators_of: list[str],
) -> str | None:
    """Why this person may decide this step, or None."""
    me = _email(email)
    kind, value = step["assignee_kind"], step["assignee_value"]

    def direct(who: str) -> str | None:
        if kind == "user" and _email(value) == who:
            return "assigned to you" if who == me else f"delegate for {value}"
        if kind == "group" and who in {
            _email(m) for m in groups.get(value, [])
        }:
            return (
                f"member of {value}"
                if who == me
                else f"delegate for a member of {value}"
            )
        return None

    # The caller first, then delegators in the order given: a list, never a
    # set, so someone who holds the seat themselves is never recorded as
    # acting for somebody else.
    for who in [me, *(_email(d) for d in delegators_of)]:
        why = direct(who)
        if why:
            return why
    if kind == "role" and ROLE_RANK.get(role, -1) >= ROLE_RANK.get(value, 99):
        return f"has the {value} role"
    target = step.get("escalated_to")
    if isinstance(target, str) and target:
        # Named after the seat it came from (a user, group or role), which is
        # what the escalation target needs to know about the step.
        if (
            target.startswith("role:")
            and ROLE_RANK.get(role, -1) >= ROLE_RANK.get(target[5:], 99)
        ) or (not target.startswith("role:") and _email(target) == me):
            return f"escalated from {value}"
    return None


def refusal(
    email: str, *, uploader: str | None, previous_deciders: list[str]
) -> str | None:
    me = _email(email)
    if uploader and _email(uploader) == me:
        return "The person who uploaded an invoice cannot approve it."
    if me in {_email(d) for d in previous_deciders}:
        return (
            "You decided an earlier step; each step needs a different person."
        )
    return None


def editor_needs_second(
    email: str, editors: list[str], is_only_pending: bool
) -> bool:
    """Someone who corrected the fields cannot be the sole approver."""
    return is_only_pending and _email(email) in {_email(e) for e in editors}
