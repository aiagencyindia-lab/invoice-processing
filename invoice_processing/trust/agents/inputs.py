"""The text an agent acted on, for the injection detector: the PDF's text
layer plus, for an emailed document, the email body. Read lazily (only a
detector that needs it pays), once, and never raises: a missing or unreadable
blob is no signal. No OCR (program plan B12)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import cache

from ..payment.signals import parse_email
from ..provenance.pdfmeta import page_text

logger = logging.getLogger(__name__)
MAX_PAGES = 20
MAX_CHARS = 200_000


def _read(fn) -> str:
    try:
        return fn() or ""
    except Exception as exc:  # a missing blob is a gap, not a crash
        logger.info(
            "agent_text_unreadable", extra={"error": type(exc).__name__}
        )
        return ""


def invoice_text(tenant_id: str, case_id: str) -> Callable[[], str]:
    """The invoice's document text, read only when called."""

    def load() -> str:
        from ...api import service  # noqa: PLC0415 - asked from the API

        document = service.get_documents().get(tenant_id, case_id) or {}
        return text_of(document, service.get_blobs(tenant_id))()

    return load


def text_of(document: dict, blobs) -> Callable[[], str]:
    @cache
    def load() -> str:
        parts = []
        if key := document.get("storage_key"):
            parts.append(
                _read(lambda: page_text(blobs.get(key), MAX_PAGES, MAX_CHARS))
            )
        ref = document.get("source_ref")
        if document.get("source") == "email" and ref:
            parts.append(_read(lambda: parse_email(blobs.get(ref)).body))
        return "\n".join(p for p in parts if p)[:MAX_CHARS]

    return load
