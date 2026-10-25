"""Structured logs and error reporting, configured in one place.

Every line the API or the worker writes is one JSON object carrying the
request id, the tenant and the document, so a line can be joined to the
audit trail (which stores the same request id) and to the Sentry event it
produced. Locally the lines are plain text. Sentry is off unless SENTRY_DSN
is set, so tests and local runs report nothing.

# ponytail: one handler, one formatter; dictConfig when a second sink exists.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.utils import BadDsn

HANDLER_NAME = "ledgerly"
TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
_DROP_HEADERS = {"authorization", "cookie", "x-dev-principal"}
# LogRecord's own attributes; anything else on a record came from `extra=`.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "color_message",  # uvicorn's ANSI copy of its own message
}

# The request id is set by the API middleware and by the worker per job;
# api.auth re-exports this same object, which the audit context reads.
current_request_id: ContextVar[str] = ContextVar("request_id", default="")
current_tenant_id: ContextVar[str] = ContextVar("tenant_id", default="")
current_document_id: ContextVar[str] = ContextVar("document_id", default="")


def _request_id() -> str:
    return current_request_id.get("")


def _stamp(record: logging.LogRecord) -> None:
    # Because every record is born with these three attributes, the stdlib
    # refuses `extra={"request_id": ...}` (KeyError: "Attempt to overwrite"),
    # raised in the caller. Set the ContextVar instead.
    record.request_id = _request_id()
    record.tenant_id = current_tenant_id.get("")
    record.document_id = current_document_id.get("")


# The ids go on the record when it is made, not in a filter: a child
# logger's record reaches the root handlers without passing the root
# logger's filters, so any handler (pytest's caplog, Sentry's, ours) would
# otherwise see records with no ids at all.
_base_factory = logging.getLogRecordFactory()
if not getattr(_base_factory, "_ledgerly", False):

    def _factory(*args, **kwargs) -> logging.LogRecord:
        record = _base_factory(*args, **kwargs)
        _stamp(record)
        return record

    _factory._ledgerly = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(_factory)


class ContextFilter(logging.Filter):
    """Stamp every record with the ids and the process it came from."""

    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        _stamp(record)
        record.service = self.service
        record.env = os.getenv("ENV", "")
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        try:
            msg = record.getMessage()
        except (TypeError, ValueError):  # a bad %-format must not lose the line
            msg = f"{record.msg} {record.args!r}"
        line = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": msg,
        }
        for key in ("service", "env", "request_id", "tenant_id", "document_id"):
            line[key] = getattr(record, key, "")
        for key, value in record.__dict__.items():
            if key not in _STANDARD and key not in line:
                line[key] = value
        if record.exc_info and "exc" not in line:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def _json_wanted() -> bool:
    chosen = os.getenv("LOG_FORMAT", "").lower()
    if chosen:
        return chosen == "json"
    # Here, not at module level: importing the api package loads the app,
    # and this module must stay importable from anywhere without that.
    from .api import preflight  # noqa: PLC0415

    return preflight.is_deployed()


def configure(service: str) -> None:
    """One stdout handler on the root logger; safe to call more than once."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.name == HANDLER_NAME:
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.name = HANDLER_NAME
    handler.addFilter(ContextFilter(service))
    handler.setFormatter(
        JsonFormatter() if _json_wanted() else logging.Formatter(TEXT_FORMAT)
    )
    root.addHandler(handler)
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    try:
        root.setLevel(level)
    except ValueError:
        # A typo must not stop the API from starting.
        root.setLevel(logging.INFO)
        logging.getLogger(__name__).warning(
            "LOG_LEVEL %r is not a level; using INFO", level
        )
    # uvicorn's lines go through the same handler; its access log is replaced
    # by the API's own request line (service._identify_request).
    for name in ("uvicorn", "uvicorn.error"):
        child = logging.getLogger(name)
        child.handlers.clear()
        child.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    init_sentry(service)


def scrub(event: dict, hint: dict) -> dict:
    """Drop credentials from the event and tag it with the ids."""
    headers = (event.get("request") or {}).get("headers")
    if isinstance(headers, dict):
        event["request"]["headers"] = {
            k: v for k, v in headers.items() if k.lower() not in _DROP_HEADERS
        }
    elif isinstance(headers, list):  # [[name, value], ...]
        event["request"]["headers"] = [
            pair
            for pair in headers
            if str(pair[0]).lower() not in _DROP_HEADERS
        ]
    tags = event.setdefault("tags", {})
    tags.update(
        request_id=_request_id(),
        tenant_id=current_tenant_id.get(""),
        document_id=current_document_id.get(""),
    )
    exc_info = hint.get("exc_info")
    if (
        exc_info
        and exc_info[0] is not None
        and exc_info[0].__name__ == "LlmUnavailable"
    ):
        event["fingerprint"] = ["llm-unavailable"]
    return event


def init_sentry(service: str) -> bool:
    dsn = os.getenv("SENTRY_DSN", "")
    if not dsn:
        return False
    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("ENV", "") or "unknown",
            release=os.getenv("RAILWAY_GIT_COMMIT_SHA") or None,
            send_default_pii=False,
            max_request_body_size="never",
            traces_sample_rate=0,
            integrations=[
                LoggingIntegration(
                    level=logging.INFO, event_level=logging.ERROR
                )
            ],
            before_send=scrub,
        )
    except BadDsn:
        # Monitoring is not a control: a mistyped DSN crash-looped both
        # processes on Railway. Never log the value, it is a credential.
        logging.getLogger(__name__).warning(
            "SENTRY_DSN is not a valid DSN; running without Sentry."
        )
        return False
    sentry_sdk.set_tag("service", service)
    return True
