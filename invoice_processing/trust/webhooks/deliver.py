"""Send due outbox rows. The v2/w3-api branch fills `due`; the worker calls
it every `WEBHOOK_SCAN_SECONDS`."""

from __future__ import annotations

from datetime import datetime


def due(now: datetime, *, post=None, limit: int = 50) -> int:
    return 0
