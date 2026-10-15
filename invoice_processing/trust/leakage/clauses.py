"""Contract clauses: candidate spans by regex, a type by the model, params by regex.

The model only says what kind of clause a sentence is (and how sure it is);
every number a rule later multiplies is read from the excerpt here, so a
model can never put a rupee into a finding.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pdfplumber

from ... import confidence, masking
from ..decide import Question, decide

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
    r"(?:₹|\bINR\b|\bRs\b\.?)\s*(\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*(lakhs?|lacs?|crores?|cr)\b)?",
    re.IGNORECASE,
)
_SCALE = {
    "lakh": 100_000,
    "lac": 100_000,
    "crore": 10_000_000,
    "cr": 10_000_000,
}
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


def _segments(page_text: str) -> list[str]:
    """A line that carries money is its own segment (a table row is one
    item and one price); other lines run together as prose."""
    out, prose = [], []
    for line in (page_text or "").splitlines():
        if _MONEY.search(line):
            out += [" ".join(prose), line]
            prose = []
        else:
            prose.append(line)
    return [*out, " ".join(prose)]


def candidates(page_text: str) -> list[str]:
    """Sentences worth asking about: money, percentages, dates, keywords.
    Raw text: the caller reads params from it, then scrubs what it keeps."""
    out = []
    for segment in _segments(page_text):
        flat = re.sub(r"\s+", " ", segment).strip()
        protected = _ABBREV.sub(lambda m: m.group(1) + "\x00", flat)
        for raw in _SENTENCE_END.split(protected):
            sentence = raw.replace("\x00", ".").strip()
            if sentence and _TRIGGER.search(sentence):
                out.append(sentence[:MAX_EXCERPT])
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


def _amount(number: str, scale: str) -> str | None:
    value = _number(number)
    if value is None or not scale:
        return value
    mult = _SCALE[scale.lower().rstrip("s")]
    return format((Decimal(value) * mult).normalize(), "f")


def params(kind: str, excerpt: str) -> dict | None:
    """The numbers a clause of this kind carries, or None if it carries none.
    A rate or tier naming two different amounts is ambiguous (which item has
    which price?) and carries none."""
    money = [_amount(n, scale) for n, scale in _MONEY.findall(excerpt)]
    money = [m for m in money if m is not None]
    if kind in ("rate", "tier") and len({Decimal(m) for m in money}) > 1:
        return None
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


# -- extraction ---------------------------------------------------------------

logger = logging.getLogger(__name__)
SOURCE = "leakage"


MAX_CONTRACT_BYTES = 25 * 2**20


def _pages(data: bytes, limit: int) -> list[str]:
    """Text of the first `limit` pages only; nothing past the cap is read."""
    if len(data) > MAX_CONTRACT_BYTES:
        logger.info("leakage_contract_too_large", extra={"bytes": len(data)})
        return []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return [page.extract_text() or "" for page in pdf.pages[:limit]]
    except Exception as exc:
        logger.info(
            "leakage_contract_unparseable", extra={"error": type(exc).__name__}
        )
        return []


def _questions(cands: list[str]) -> list[Question]:
    out = []
    for i in range(len(cands)):
        out.append(
            Question(
                f"c{i}",
                "choice",
                f"Which kind of contract clause is candidate c{i}? 'rate' is "
                "a unit price, 'tier' a price after a quantity, 'rebate' a "
                "credit after spend, 'discount' early-payment terms, 'term' "
                "the contract's start and end, 'none' anything else.",
                fallback="none",
                options=TYPES,
            )
        )
        out.append(
            Question(
                f"c{i}_score",
                "score",
                f"How sure are you of the kind of candidate c{i}?",
                fallback=0.0,
            )
        )
    return out


def extract(ectx) -> list[str]:
    """Clause nodes for the contract document in `ectx.document`."""
    doc = ectx.document
    tenant = ectx.ctx.tenant_id
    contract = ectx.graph.nodes_by_ref(tenant, "documents", [doc["id"]]).get(
        doc["id"]
    )
    if contract is None:
        logger.info("leakage_contract_node_missing")
        return []
    limit = int(ectx.threshold("leakage.max_pages"))
    pages = _pages(ectx.blobs.get(doc["storage_key"]), limit)
    if not confidence.has_readable_text(pages):
        # Scanned contracts (OCR) are a Wave 1 non-goal.
        logger.info("leakage_contract_unreadable")
        return []
    ids: list[str] = []
    skipped = 0
    for page, text in enumerate(pages, start=1):
        raw = candidates(text)
        cands = [scrub(c) for c in raw]
        if not cands:
            continue
        answers = {
            a.id: a
            for a in decide(
                {
                    "page": page,
                    "candidates": {f"c{i}": c for i, c in enumerate(cands)},
                },
                _questions(cands),
                ectx.decider,
            )
        }
        for i, excerpt in enumerate(cands):
            kind = answers[f"c{i}"]
            if kind.fallback or kind.value == "none":
                skipped += kind.fallback
                continue
            found = params(kind.value, raw[i])
            if found is None:
                skipped += 1
                continue
            score = answers[f"c{i}_score"]
            conf = round(
                float(kind.probability if score.fallback else score.value), 4
            )
            ids.append(
                _write(ectx, contract, page, kind.value, found, excerpt, conf)
            )
    if skipped:
        logger.info("leakage_clauses_skipped", extra={"count": skipped})
    return ids


def _write(ectx, contract, page, kind, found, excerpt, conf) -> str:
    graph, tenant = ectx.graph, ectx.ctx.tenant_id
    digest = hashlib.sha256(excerpt.encode()).hexdigest()[:12]
    key = f"clause:{contract}:{page}:{digest}"
    open_row = graph.open_node(tenant, "contract_clause", key)
    if open_row and open_row["attrs"].get("reviewed"):
        node = open_row["id"]  # a person's review outranks a re-read
    else:
        node = graph.upsert_node(
            ectx.ctx,
            kind="contract_clause",
            natural_key=key,
            attrs={
                "type": kind,
                "params": found,
                "page": page,
                "excerpt": excerpt,
                "confidence": conf,
                "reviewed": False,
            },
            at=ectx.now,
        )
    graph.upsert_edge(
        ectx.ctx,
        src=node,
        dst=contract,
        kind="part_of",
        source_system=SOURCE,
        provenance="model_inference",
        confidence=conf,
        observed_at=ectx.now,
        at=ectx.now,
    )
    return node
