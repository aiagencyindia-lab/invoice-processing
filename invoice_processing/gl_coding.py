"""Which GL account each invoice line is coded to.

Pure, like approvals and bank_check: tenant rules in, a coding artifact out,
so the answer to "why did this line go to 6100?" is a rule id an auditor can
read. Deterministic rules only; an uncoded line is informational and never
holds an invoice.
"""

from __future__ import annotations

from . import po_matching

# Higher wins: a rule naming both the vendor and the words is the most specific.
_VENDOR_AND_KEYWORD, _KEYWORD, _VENDOR = 3, 2, 1


def _str(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _usable(rule) -> bool:
    """Whether a stored rule can be read; one bad row must not stop coding."""
    return (
        isinstance(rule, dict)
        and isinstance(rule.get("id"), str)
        and 0 < len(_str(rule.get("gl_account"))) <= 64
        and bool(_str(rule.get("vendor_name")) or _str(rule.get("keyword")))
        and rule.get("enabled", True) is not False
    )


def _rank(rule: dict, vendor: str, description: str) -> int:
    """How specifically this rule matches the line, 0 when it does not."""
    rule_vendor = po_matching.normalize_vendor(_str(rule.get("vendor_name")))
    keyword = _str(rule.get("keyword")).casefold()
    if _str(rule.get("vendor_name")) and not (
        rule_vendor and rule_vendor == vendor
    ):
        return 0
    if keyword and keyword not in description:
        return 0
    if keyword:
        return _VENDOR_AND_KEYWORD if rule_vendor else _KEYWORD
    return _VENDOR


def valid_entries(coding) -> list[dict]:
    """The stored coding's entries that address a line; the rest are ignored.

    A line number must be a real int: a list would not hash, and True would
    silently code line 1.
    """
    entries = coding.get("lines") if isinstance(coding, dict) else None
    return [
        e
        for e in (entries if isinstance(entries, list) else [])
        if isinstance(e, dict)
        and isinstance(e.get("line_number"), int)
        and not isinstance(e.get("line_number"), bool)
    ]


def _uncoded(line_number) -> dict:
    return {
        "line_number": line_number,
        "gl_account": None,
        "source": "none",
        "rule_id": None,
        "coded_by": None,
        "coded_at": None,
    }


def code_lines(
    invoice: dict,
    lines: list[dict],
    rules: list[dict],
    previous: dict | None = None,
) -> dict:
    """The `gl_coding` artifact for these lines.

    A line a person coded (source "manual" in `previous`) is kept as it is:
    a re-run never overwrites a person.
    """
    manual = {
        entry["line_number"]: entry
        for entry in valid_entries(previous)
        if entry.get("source") == "manual"
    }

    vendor = po_matching.normalize_vendor(invoice.get("vendor_name"))
    # Stable sort: earliest created first, then input order, so a strict >
    # below keeps the first of equally specific rules.
    live = sorted(
        (r for r in rules or [] if _usable(r)),
        key=lambda r: str(r.get("created_at") or ""),
    )

    out = []
    for line in lines:
        number = line.get("line_number")
        if number in manual:
            out.append(manual[number])
            continue
        description = str(line.get("description") or "").casefold()
        best, best_rank = None, 0
        for rule in live:
            rank = _rank(rule, vendor, description)
            if rank > best_rank:
                best, best_rank = rule, rank
        coded = _uncoded(number)
        if best is not None:
            coded.update(
                gl_account=_str(best["gl_account"]),
                source="rule",
                rule_id=best["id"],
            )
        out.append(coded)
    return {"lines": out}
