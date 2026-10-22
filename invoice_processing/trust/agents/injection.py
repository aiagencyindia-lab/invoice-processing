"""Deterministic prompt-injection indicators (FR-28.4): text in a document or
email that addresses an automated agent instead of a person.

Patterns only; a hit is a reason to ask the model, never a verdict on its own
(`behaviour.injection`). Offsets point into `normalise(text)`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...masking import mask_text

ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff"
_ZW = re.compile(f"[{ZERO_WIDTH}]")
_SPACES = re.compile(r"[^\S\n]+")
_NEWLINES = re.compile(r" ?\n[\s]*")
MIN_ZERO_WIDTH = 3
# A number as masking reads one (digits with space, dot or dash between).
_NUMBER = re.compile(r"\d(?:[ .-]?\d)*")

PATTERNS: dict[str, re.Pattern] = {
    name: re.compile(rx, re.IGNORECASE | re.MULTILINE)
    for name, rx in {
        "ignore_instructions": r"\b(?:ignore|disregard) (?:(?:all|any) )?"
        r"(?:previous|prior|above) instructions\b",
        "role_prefix": r"^(?:system|assistant|developer)(?: prompt)?:",
        "you_are_agent": r"\byou are (?:now )?(?:an?|the) "
        r"(?:ai|assistant|agent|bot)\b",
        "approve_now": r"\bapprove (?:this|the) (?:invoice|payment) "
        r"(?:immediately|without|now)\b",
        "do_not_check": r"\bdo not (?:flag|hold|review|escalate)\b",
        "new_account": r"\b(?:pay|transfer) to (?:the )?(?:new|updated) account\b",
    }.items()
}
# A run of 64+ base64 characters mixing upper case, lower case and digits,
# standing alone. Case-sensitive: a hex hash (an e-invoice IRN is 64 hex) has
# one case. Not part of a dotted name, URL host or JWT (a signed e-invoice QR
# is base64url segments joined by dots), which is ordinary invoice text.
PATTERNS["base64"] = re.compile(
    r"(?<![A-Za-z0-9+/._-])(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[a-z])"
    r"(?=[A-Za-z0-9+/]*[0-9])[A-Za-z0-9+/]{64,}={0,2}(?![A-Za-z0-9+/=._-])"
)


@dataclass(frozen=True)
class Hit:
    pattern: str
    start: int
    end: int


def normalise(text: str) -> str:
    """Zero-width characters removed, runs of spaces collapsed, lines kept
    (a role prefix only counts at a line start)."""
    text = _SPACES.sub(" ", _ZW.sub("", text or ""))
    return _NEWLINES.sub("\n", text).strip()


def scan(text: str) -> list[Hit]:
    norm = normalise(text)
    hits = [
        Hit(name, m.start(), m.end())
        for name, rx in PATTERNS.items()
        for m in rx.finditer(norm)
    ]
    if len(_ZW.findall(text or "")) >= MIN_ZERO_WIDTH:
        hits.append(Hit("zero_width", 0, 0))
    return sorted(hits, key=lambda h: (h.start, h.pattern))


def excerpts(
    text: str, hits: list[Hit], n: int = 3, width: int = 120
) -> list[str]:
    """Up to `n` masked snippets around the hits, each at most `width`."""
    norm = normalise(text)
    out: list[str] = []
    for h in hits:
        if h.pattern == "zero_width":
            snippet = f"{len(_ZW.findall(text or ''))} zero-width characters"
        else:
            pad = max((width - (h.end - h.start)) // 2, 0)
            start, end = max(h.start - pad, 0), h.end + pad
            # Never cut through a number: masked whole, or its middle shows.
            for m in _NUMBER.finditer(norm):
                if m.start() < start < m.end():
                    start = m.start()
                if m.start() < end < m.end():
                    end = m.end()
            snippet = mask_text(" ".join(norm[start:end].split()))[:width]
        if snippet not in out:
            out.append(snippet)
        if len(out) == n:
            break
    return out
