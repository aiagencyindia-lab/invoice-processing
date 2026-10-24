"""The text an agent acted on, for the injection detector: the PDF's text
layer plus, for an emailed document, the email body. Read lazily (only a
detector that needs it pays), once, and never raises: a missing or unreadable
blob is no signal. OCR only when asked (the worker's deferred scan)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from functools import cache

from ... import confidence
from ..payment.signals import parse_email
from ..provenance import pdfmeta
from ..provenance.pdfmeta import page_text

logger = logging.getLogger(__name__)
MAX_PAGES = 20
MAX_CHARS = 200_000
# A scan's injection is read by OCR, worker only (program plan B8): a few
# pages, and well inside the queue's 600 s ack deadline so a slow scan is
# never redelivered mid-OCR.
MAX_OCR_PAGES = 5
OCR_BUDGET_SECONDS = 120


def _read(fn):
    try:
        return fn() or ""
    except Exception as exc:  # a missing blob is a gap, not a crash
        logger.info(
            "agent_text_unreadable", extra={"error": type(exc).__name__}
        )
        return ""


def invoice_text(
    tenant_id: str, case_id: str, *, ocr: bool = False
) -> Callable[[], str]:
    """The invoice's document text, read only when called."""

    def load() -> str:
        from ...api import service  # noqa: PLC0415 - asked from the API

        document = service.get_documents().get(tenant_id, case_id) or {}
        blobs = service.get_blobs(tenant_id)
        if ocr:
            return text_of(document, blobs, ocr=True)()
        return text_of(document, blobs)()

    return load


def _ocr(data: bytes) -> str:
    """OCR of a PDF `page_text` would itself read, within the budget."""
    if not data.startswith(b"%PDF") or len(data) > pdfmeta.MAX_BYTES:
        return ""
    return "\n".join(
        confidence.ocr_page_text(
            data,
            max_pages=MAX_OCR_PAGES,
            deadline=time.monotonic() + OCR_BUDGET_SECONDS,
        )
    )


def text_of(document: dict, blobs, *, ocr: bool = False) -> Callable[[], str]:
    @cache
    def load() -> str:
        parts = []
        if key := document.get("storage_key"):
            data = _read(lambda: blobs.get(key)) or b""
            layer = _read(lambda: page_text(data, MAX_PAGES, MAX_CHARS))
            parts.append(layer)
            if ocr and not confidence.has_readable_text([layer]):
                parts.append(_read(lambda: _ocr(data)))
        ref = document.get("source_ref")
        if document.get("source") == "email" and ref:
            parts.append(_read(lambda: parse_email(blobs.get(ref)).body))
        return "\n".join(p for p in parts if p)[:MAX_CHARS]

    return load
