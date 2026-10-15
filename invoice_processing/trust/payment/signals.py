"""Payment Trust's pure signals: no database, no graph, no provider.

Statistics on a vendor's history, domain look-alikes, what an email body says
about urgency and bank details, and the stored email itself in each shape the
ingest keeps it (raw MIME, Postmark JSON, SendGrid multipart).
"""

from __future__ import annotations

import difflib
import email
import html
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from email import policy
from email.utils import parseaddr
from statistics import median

from ... import masking

# -- statistics ------------------------------------------------------------


def robust_z(amount: Decimal, history: list[Decimal]) -> float:
    """(amount − median) / (1.4826 × MAD); ±inf when the history never varies."""
    med = median(history)
    mad = median(abs(x - med) for x in history)
    if mad == 0:
        if amount == med:
            return 0.0
        return float("inf") if amount > med else float("-inf")
    return float((amount - med) / (Decimal("1.4826") * mad))


def nearest_gap_ratio(when: date, others: list[date]) -> float | None:
    """Days to the nearest other invoice over the median gap between the
    others; None when there is no cadence to compare with."""
    ordered = sorted(others)
    gaps = [(b - a).days for a, b in zip(ordered, ordered[1:], strict=False)]
    if not gaps:
        return None
    typical = median(gaps)
    if typical <= 0:
        return None
    return min(abs((when - d).days) for d in ordered) / typical


# -- domains ---------------------------------------------------------------

# ponytail: a small confusable map (digits, Cyrillic/Greek look-alikes, rn/vv);
# the full Unicode confusables table if IDN attacks beyond these show up.
_CONFUSABLE = str.maketrans(
    {
        "0": "o",
        "1": "l",
        "i": "l",
        "3": "e",
        "5": "s",
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "х": "x",
        "у": "y",
        "і": "l",
        "ο": "o",
        "α": "a",
        "ε": "e",
    }
)


def domain_of(address: str | None) -> str | None:
    addr = parseaddr(str(address or ""))[1]
    if "@" not in addr:
        return None
    return addr.rsplit("@", 1)[1].strip(" >.").lower() or None


def same_domain(domain: str, vendor_domain: str) -> bool:
    d, v = domain.lower().strip("."), vendor_domain.lower().strip(".")
    return d == v or d.endswith("." + v)


def _fold(domain: str) -> str:
    labels = []
    for label in domain.lower().split("."):
        if label.startswith("xn--"):
            try:
                label = label.encode("ascii").decode("idna")
            except UnicodeError:
                pass
        labels.append(label)
    text = unicodedata.normalize("NFKC", ".".join(labels)).translate(
        _CONFUSABLE
    )
    return text.replace("rn", "m").replace("vv", "w")


def lookalike(vendor_domain: str, domain: str, min_ratio: float) -> bool:
    if same_domain(domain, vendor_domain):
        return False
    ratio = difflib.SequenceMatcher(
        None, _fold(vendor_domain), _fold(domain)
    ).ratio()
    return ratio >= min_ratio


# -- bodies ----------------------------------------------------------------

_URGENT = re.compile(
    r"\b(urgent\w*|immediate\w*|asap|right away|as soon as possible|"
    r"overdue|final notice|confidential|wire (?:it|the|transfer)\w*|"
    r"(?:new|updated|changed?) (?:bank|account) (?:details|account)|"
    r"(?:bank|account) (?:details )?(?:has |have )?changed|"
    r"change of bank|changed our bank)\b",
    re.IGNORECASE,
)
_ACCOUNT = re.compile(
    r"\b(?:a/c|acct|account)\b[^\d\n]{0,30}?(\d[\d -]{6,22}\d)", re.IGNORECASE
)
_IFSC = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
_LONG_NUMBER = re.compile(r"\d[\d -]{6,}\d")


def mask_text(text: str) -> str:
    """Every number of eight digits or more becomes its last four."""

    def one(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group())
        return masking.mask_bank(digits) if len(digits) >= 8 else m.group()

    return _LONG_NUMBER.sub(one, text)


def _excerpt(text: str, start: int, end: int) -> str:
    lo = max(0, start - 60)
    return mask_text(" ".join(text[lo : end + 60].split()))[:240]


def urgency(text: str) -> str | None:
    m = _URGENT.search(text or "")
    return _excerpt(text, m.start(), m.end()) if m else None


def bank_details_differ(
    text: str, account: str | None, code: str | None
) -> str | None:
    """A masked excerpt when the body names an account or IFSC that is not
    the vendor master's; None when it matches or the master has none."""
    text = text or ""
    own = re.sub(r"\D", "", account or "")
    if own:
        for m in _ACCOUNT.finditer(text):
            digits = re.sub(r"\D", "", m.group(1))
            if 9 <= len(digits) <= 18 and digits != own:
                return _excerpt(text, m.start(), m.end())
    ifsc = (code or "").strip().upper()
    if _IFSC.fullmatch(ifsc):
        for m in _IFSC.finditer(text):
            if m.group() != ifsc:
                return _excerpt(text, m.start(), m.end())
    return None


# -- stored email ------------------------------------------------------------


@dataclass(frozen=True)
class Mail:
    sender: str = ""
    reply_to: str = ""
    subject: str = ""
    body: str = ""


def _bare(value) -> str:
    if isinstance(value, dict):
        value = value.get("Email") or value.get("email") or ""
    addr = parseaddr(str(value or ""))[1].strip().lower()
    return addr if "@" in addr else ""


def _text(plain, markup) -> str:
    if plain and str(plain).strip():
        return str(plain)
    return html.unescape(re.sub(r"<[^>]+>", " ", str(markup or "")))


def _mime(raw: bytes) -> Mail:
    msg = email.message_from_bytes(raw, policy=policy.default)
    part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if part is not None:
        content = part.get_content()
        body = (
            _text(None, content)
            if part.get_content_subtype() == "html"
            else str(content)
        )
    return Mail(
        _bare(msg.get("From")),
        _bare(msg.get("Reply-To")),
        str(msg.get("Subject") or ""),
        body,
    )


def _postmark(doc: dict) -> Mail:
    raw = doc.get("RawEmail")
    if isinstance(raw, str) and raw.strip():
        return _mime(raw.encode("utf-8", "replace"))
    reply_to = _bare(doc.get("ReplyTo"))
    for h in doc.get("Headers") or []:
        if not reply_to and isinstance(h, dict) and h.get("Name") == "Reply-To":
            reply_to = _bare(h.get("Value"))
    return Mail(
        _bare(doc.get("FromFull") or doc.get("From")),
        reply_to,
        str(doc.get("Subject") or ""),
        _text(doc.get("TextBody"), doc.get("HtmlBody")),
    )


def _sendgrid(raw: bytes) -> Mail:
    boundary = raw.split(b"\n", 1)[0].strip()[2:].decode("ascii", "replace")
    msg = email.message_from_bytes(
        f'Content-Type: multipart/form-data; boundary="{boundary}"\r\n'
        "MIME-Version: 1.0\r\n\r\n".encode()
        + raw,
        policy=policy.default,
    )
    fields: dict[str, str] = {}
    for part in msg.walk():
        name = part.get_param("name", header="content-disposition")
        if name and not part.get_filename():
            payload = part.get_payload(decode=True) or b""
            fields[str(name).lower()] = payload.decode("utf-8", "replace")
    if fields.get("email", "").strip():
        return _mime(fields["email"].encode("utf-8", "replace"))
    reply_to = _bare(fields.get("reply-to"))
    if not reply_to and fields.get("headers"):
        reply_to = _bare(
            email.message_from_string(fields["headers"]).get("Reply-To")
        )
    return Mail(
        _bare(fields.get("from")),
        reply_to,
        fields.get("subject", ""),
        _text(fields.get("text"), fields.get("html")),
    )


def parse_email(raw: bytes) -> Mail:
    """The stored message in any shape ingest keeps; empty when unreadable."""
    head = raw.lstrip()[:2]
    try:
        if head.startswith(b"{"):
            doc = json.loads(raw.decode("utf-8", "replace"))
            return _postmark(doc) if isinstance(doc, dict) else Mail()
        if head == b"--":
            return _sendgrid(raw.lstrip())
        return _mime(raw)
    except Exception:  # noqa: BLE001 - hostile input reads as "no mail"
        return Mail()
