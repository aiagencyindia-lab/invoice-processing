"""What a PDF says about where it came from: metadata, layout, fonts, text."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from ... import confidence

logger = logging.getLogger(__name__)
# Parsing cost grows with bytes and pages; the ingest limit is 25 MB, and the
# provenance facts are all on the first pages.
MAX_BYTES = 25 * 2**20
MAX_PAGES = 40

_DATE = re.compile(
    r"(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
    r"(Z|[+-]\d{2}(?:'?\d{2})?)?'?"
)
# A subset-embedded font is named "ABCDEF+Arial"; the prefix changes per file.
_SUBSET = re.compile(r"^[A-Z]{6}\+")
# A trailer: the keyword, the xref offset, then end-of-file. The bare word
# can appear in page text or an embedded file.
_TRAILER = re.compile(rb"startxref\s+\d+\s+%%EOF")
# A line holding an amount: a number with two decimals ("1,180.00").
_AMOUNT = re.compile(r"\d[\d,]*\.\d{2}\b")


def _text(value) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("latin-1", errors="replace")
    if not isinstance(value, str):
        return None
    return value.strip() or None


def pdf_date(value) -> datetime | None:
    """A PDF date ("D:YYYYMMDDHHmmSS+HH'mm'", every part after the year
    optional) as an aware datetime; no zone means UTC. None when unparseable."""
    match = _DATE.fullmatch(_text(value) or "")
    if not match:
        return None
    y, mo, d, h, mi, s, tz = match.groups()
    try:
        offset = timedelta(0)
        if tz and tz != "Z":
            digits = tz[1:].replace("'", "")
            offset = timedelta(
                hours=int(digits[:2]), minutes=int(digits[2:] or 0)
            )
            offset = -offset if tz[0] == "-" else offset
        return datetime(
            int(y),
            int(mo or 1),
            int(d or 1),
            int(h or 0),
            int(mi or 0),
            int(s or 0),
            tzinfo=timezone(offset),
        )
    except ValueError:
        return None


def incremental_updates(data: bytes) -> int:
    """How many times the file was saved on top of itself: every save
    appends a section ending in its own `startxref`. A linearised file's
    first-page section carries one more that is not a save."""
    sections = len(_TRAILER.findall(data))
    if b"/Linearized" in data[:1024]:
        sections -= 1
    return max(sections - 1, 0)


def _lines(chars) -> dict:
    """Chars grouped into lines by their rounded top edge."""
    lines: dict = {}
    for c in chars:
        lines.setdefault(round(float(c.get("top") or 0)), []).append(c)
    return lines


def facts(data: bytes) -> dict | None:
    """The document's provenance facts, None when it is not a readable PDF."""
    if not data.startswith(b"%PDF"):
        return None
    if len(data) > MAX_BYTES:
        logger.info("provenance_skipped", extra={"reason": "too_large"})
        return None
    try:
        import pdfplumber  # noqa: PLC0415 - heavy; only this engine needs it

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            meta = pdf.metadata or {}
            sizes, fonts, texts = set(), set(), []
            body, amounts = set(), set()
            for page in pdf.pages[:MAX_PAGES]:
                sizes.add((round(float(page.width)), round(float(page.height))))
                fonts.update(
                    _SUBSET.sub("", c.get("fontname") or "") for c in page.chars
                )
                texts.append(page.extract_text() or "")
                for line in _lines(page.chars).values():
                    text = "".join(c.get("text") or "" for c in line)
                    names = {c.get("fontname") or "" for c in line}
                    (amounts if _AMOUNT.search(text) else body).update(names)
            page_count = len(pdf.pages)
    except Exception:  # pdfminer raises many types on a damaged or locked file
        return None
    created = pdf_date(meta.get("CreationDate"))
    modified = pdf_date(meta.get("ModDate"))
    return {
        "producer": _text(meta.get("Producer")),
        "creator": _text(meta.get("Creator")),
        "creation_date": created.isoformat() if created else None,
        "mod_date": modified.isoformat() if modified else None,
        "page_count": page_count,
        "page_sizes": [list(s) for s in sorted(sizes)],
        "fonts": sorted(f for f in fonts if f),
        "has_text": confidence.has_readable_text(texts),
        "incremental_updates": incremental_updates(data),
        # Raw names, subset prefixes kept: forensics compares them.
        "font_names": {
            "body": sorted(f for f in body if f),
            "amounts": sorted(f for f in amounts if f),
        },
    }


def page_text(data: bytes, max_pages: int, max_chars: int) -> str:
    """The text layer of the first `max_pages` pages, at most `max_chars`;
    empty for anything that is not a readable PDF (no OCR)."""
    if not data.startswith(b"%PDF") or len(data) > MAX_BYTES:
        return ""
    texts: list[str] = []
    size = 0
    try:
        import pdfplumber  # noqa: PLC0415 - heavy; loaded on first use

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages[:max_pages]:
                texts.append(page.extract_text() or "")
                size += len(texts[-1]) + 1
                if size >= max_chars:
                    break
    except Exception as exc:  # pdfminer raises many types on a bad file
        logger.info("pdf_text_unreadable", extra={"error": type(exc).__name__})
        return ""
    return "\n".join(texts)[:max_chars]


def fingerprint(f: dict) -> str:
    """The template: rounded page sizes + sorted font set + producer."""
    key = json.dumps(
        [f["page_sizes"], f["fonts"], f["producer"]], separators=(",", ":")
    )
    return hashlib.sha256(key.encode()).hexdigest()
