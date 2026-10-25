"""One Protocol per capability, and the result every call returns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from ..modes import CAPABILITIES

__all__ = ["CAPABILITIES", "METHOD", "ProviderResult"]


@dataclass(frozen=True)
class ProviderResult:
    status: Literal["ok", "unavailable"]
    data: dict
    raw_sha256: str
    provider: str
    capability: str
    observed_at: datetime
    evidence_node: str | None


@runtime_checkable
class BankAccountVerifier(Protocol):
    def verify(self, account: str, code: str, name: str) -> dict: ...


@runtime_checkable
class GstinLookup(Protocol):
    def lookup(self, gstin: str) -> dict: ...


@runtime_checkable
class CompanyRegistry(Protocol):
    def lookup(self, query: str) -> dict: ...


@runtime_checkable
class AbnLookup(Protocol):
    def lookup(self, abn: str) -> dict: ...


@runtime_checkable
class VatLookup(Protocol):
    def check(self, vat: str) -> dict: ...


@runtime_checkable
class Mailbox(Protocol):
    def fetch(self, since: str) -> list[dict]: ...


@runtime_checkable
class Tickets(Protocol):
    def search(self, query: str) -> list[dict]: ...


@runtime_checkable
class Timesheets(Protocol):
    def hours(self, vendor: str, period: str) -> dict: ...


@runtime_checkable
class Logistics(Protocol):
    def track(self, reference: str) -> dict: ...


@runtime_checkable
class Usage(Protocol):
    def seats(self, vendor: str, period: str) -> dict: ...


# The one method each capability exposes; the registry calls it by name.
METHOD = {
    "bank_verification": "verify",
    "gstin": "lookup",
    "company_registry": "lookup",
    "abn": "lookup",
    "vat": "check",
    "mailbox": "fetch",
    "tickets": "search",
    "timesheets": "hours",
    "logistics": "track",
    "usage": "seats",
}
