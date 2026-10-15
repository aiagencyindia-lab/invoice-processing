"""What a PDF says about where it came from: metadata, layout, fonts, text."""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import datetime, timedelta, timezone

from ... import confidence

_DATE = re.compile(
    r"(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
    r"(Z|[+-]\d{2}(?:'?\d{2})?)?'?"
)
# A subset-embedded font is named "ABCDEF+Arial"; the prefix changes per file.
_SUBSET = re.compile(r"^[A-Z]{6}\+")


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


def facts(data: bytes) -> dict | None:
    """The document's provenance facts, None when it is not a readable PDF."""
    if not data.startswith(b"%PDF"):
        return None
    try:
        import pdfplumber  # noqa: PLC0415 - heavy; only this engine needs it

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            meta = pdf.metadata or {}
            sizes, fonts, texts = set(), set(), []
            for page in pdf.pages:
                sizes.add((round(float(page.width)), round(float(page.height))))
                fonts.update(
                    _SUBSET.sub("", c.get("fontname") or "") for c in page.chars
                )
                texts.append(page.extract_text() or "")
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
    }


def fingerprint(f: dict) -> str:
    """The template: rounded page sizes + sorted font set + producer."""
    key = json.dumps(
        [f["page_sizes"], f["fonts"], f["producer"]], separators=(",", ":")
    )
    return hashlib.sha256(key.encode()).hexdigest()
