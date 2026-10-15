"""Contract clauses: candidate spans by regex, a type by the model, params by regex.

The model only says what kind of clause a sentence is (and how sure it is);
every number a rule later multiplies is read from the excerpt here, so a
model can never put a rupee into a finding.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ... import masking

TYPES = ("rate", "escalator", "tier", "rebate", "discount", "term", "none")

_MONTHS = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|"
    "march|april|june|july|august|september|october|november|december"
)
_DATE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{4}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})\.?,?\s+\d{{4}}"
    rf"|(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}})\b",
    re.IGNORECASE,
)
_MONEY = re.compile(
    r"(?:₹|\bINR\b|\bRs\b\.?)\s*(\d[\d,]*(?:\.\d+)?)", re.IGNORECASE
)
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|per\s*cent\b|percent\b)", re.I)
_QTY = re.compile(r"(\d[\d,]*)\s*(?:units?|nos\.?|pieces|pcs)\b", re.I)
_TERMS_SLASH = re.compile(
    r"(\d+(?:\.\d+)?)\s*%?\s*/\s*(\d+)\s*,?\s*net\s*(\d+)", re.I
)
_TERMS_WORDS = re.compile(
    r"(\d+(?:\.\d+)?)\s*%[^.;]*?within\s+(\d+)\s+days(?:[^.;]*?net\s*(\d+))?",
    re.I,
)
_TRIGGER = re.compile(
    r"%|₹|\bINR\b|\bRs\b|per\s+unit|\btier|rebate|escalat|renew|notice"
    r"|\bnet\s*\d+|" + _DATE.pattern,
    re.IGNORECASE,
)
# A sentence ends at . or ; after a word, digit, bracket or percent sign
# and before whitespace; "Rs." is an abbreviation, not an end.
_SENTENCE_END = re.compile(r"(?<=[A-Za-z0-9)%][.;])\s+")
_ABBREV = re.compile(r"\b(Rs|No|Nos)\.(?=\s*\d)", re.IGNORECASE)
_LONG_DIGITS = re.compile(r"\d{9,}")
MAX_EXCERPT = 400


def scrub(text: str) -> str:
    """A run of nine or more digits is a bank or card number; mask it."""
    return _LONG_DIGITS.sub(lambda m: masking.mask_bank(m.group()), text)


def candidates(page_text: str) -> list[str]:
    """Sentences worth asking about: money, percentages, dates, keywords."""
    flat = re.sub(r"\s+", " ", page_text or "").strip()
    protected = _ABBREV.sub(lambda m: m.group(1) + "\x00", flat)
    out = []
    for raw in _SENTENCE_END.split(protected):
        sentence = raw.replace("\x00", ".").strip()
        if sentence and _TRIGGER.search(sentence):
            out.append(scrub(sentence)[:MAX_EXCERPT])
    return out


def _number(text: str) -> str | None:
    try:
        return str(Decimal(text.replace(",", "")))
    except InvalidOperation:
        return None


def _date(text: str) -> date | None:
    cleaned = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", text, flags=re.I)
    cleaned = re.sub(r"[.,]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().replace("Sept", "Sep")
    for fmt in (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d %Y",
        "%b %d %Y",
    ):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def dates(text: str) -> list[date]:
    found = (_date(m.group(1)) for m in _DATE.finditer(text))
    return [d for d in found if d is not None]


def parse_terms(text: str) -> dict | None:
    """`2/10 net 30` or `2% ... within 10 days ... net 30`."""
    m = _TERMS_SLASH.search(text or "") or _TERMS_WORDS.search(text or "")
    if not m:
        return None
    pct, days, net = m.group(1), m.group(2), m.group(3)
    out = {"pct": _number(pct), "days": str(int(days))}
    if net:
        out["net"] = str(int(net))
    return out if out["pct"] and Decimal(out["pct"]) > 0 else None


def params(kind: str, excerpt: str) -> dict | None:
    """The numbers a clause of this kind carries, or None if it carries none."""
    money = [_number(m) for m in _MONEY.findall(excerpt)]
    money = [m for m in money if m is not None]
    pct = [_number(p) for p in _PCT.findall(excerpt)]
    pct = [p for p in pct if p is not None]
    if kind == "rate" and money:
        return {"price": money[0]}
    if kind == "escalator" and pct:
        return {"pct": pct[0]}
    if kind == "tier":
        qty = _QTY.search(excerpt)
        if qty and money:
            return {"qty": _number(qty.group(1)), "price": money[0]}
    if kind == "rebate" and money and pct:
        return {"threshold": money[0], "pct": pct[0]}
    if kind == "discount":
        return parse_terms(excerpt)
    if kind == "term":
        found = dates(excerpt)
        if len(found) >= 2:
            return {
                "start": min(found).isoformat(),
                "end": max(found).isoformat(),
            }
    return None
