"""Xero export: post an approved invoice as an accounts-payable bill.

The rule this module exists to enforce is that **an invoice is never posted
twice**. A duplicate bill in Xero is a duplicate liability: it survives into the
ledger, into supplier statements and into a payment run, and unwinding it is
manual work someone does under time pressure at month end. Every other concern
here is secondary to that one.

Three defences, in order:

1. Nothing posts unless the invoice is approved and has no external id.
2. Before creating, Xero is searched for the same vendor + invoice number.
   Recovers the case where a previous attempt succeeded but the response was
   lost -- a network timeout after Xero committed looks identical to a failure.
3. The returned Xero id is stored immediately, so the next attempt short-circuits.

Xero specifics worth knowing when reading this:

* A supplier bill is an ``Invoice`` with ``Type: "ACCPAY"``. The same endpoint
  serves customer invoices (``ACCREC``); the type is the only difference and
  getting it wrong files a payable as a receivable.
* Bills are created as ``DRAFT``. Posting straight to ``AUTHORISED`` would put
  an unreviewed liability on the ledger; approval belongs to a person in Xero.
* ``LineAmountTypes`` must match how the amounts were extracted. We extract
  tax-exclusive line amounts, so ``Exclusive`` -- declaring ``Inclusive`` would
  silently overstate every bill by the tax.
* Rate limits are 60 calls/minute and 5000/day per tenant.

Not implemented here: OAuth token acquisition and refresh. Tokens arrive from
the platform's credential store; this module never sees a client secret.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

XERO_API = "https://api.xero.com/api.xro/2.0"

# Xero's documented limits. Exceeding them returns 429 with a Retry-After.
RATE_LIMIT_PER_MINUTE = 60


class XeroError(RuntimeError):
    """A Xero API call failed. Carries the ERP's own message verbatim."""

    def __init__(
        self, message: str, status: int | None = None, body: Any = None
    ):
        super().__init__(message)
        self.status = status
        self.body = body


class AlreadyPosted(RuntimeError):
    """The invoice already exists in Xero. Not an error -- a stop."""

    def __init__(self, external_id: str, message: str = ""):
        super().__init__(message or f"Already posted as {external_id}")
        self.external_id = external_id


@runtime_checkable
class Transport(Protocol):
    """HTTP seam, so tests never touch the network."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict,
        json: Any = None,
        params: dict | None = None,
    ) -> tuple[int, Any]: ...


class HttpTransport:
    """The real HTTP implementation of `Transport`.

    The Protocol above existed from the start so tests never touch the network,
    but nothing ever implemented it -- which meant the connector was fully
    tested and not actually callable. This is the missing half.

    Errors are returned as `(status, body)` rather than raised, because the
    client above already interprets status codes for retry and rate limiting;
    turning a 429 into an exception here would bypass that.
    """

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict,
        json: Any = None,
        params: dict | None = None,
    ) -> tuple[int, Any]:
        import requests  # noqa: PLC0415

        response = requests.request(
            method,
            url,
            headers=headers,
            json=json,
            params=params,
            timeout=self.timeout,
        )
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text


@dataclass
class XeroCredentials:
    """Per-tenant Xero connection. Tokens come from the credential store."""

    access_token: str
    xero_tenant_id: str


@dataclass
class ExportResult:
    external_id: str | None = None
    status: str = "pending"  # created | already_posted | failed
    message: str = ""
    payload: dict = field(default_factory=dict)
    xero_response: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _decimal(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(Decimal(str(value).replace(",", "")))
    except (ValueError, ArithmeticError):
        return None


def build_bill_payload(
    invoice: dict,
    contact_id: str | None = None,
    account_code_map: dict[str, str] | None = None,
    default_account_code: str = "429",
) -> dict:
    """Map a canonical invoice to a Xero ACCPAY bill.

    `account_code_map` translates our internal item codes (LABOUR, PARTS, ...)
    to the tenant's Xero chart of accounts. Unmapped codes fall back to the
    default rather than failing the post: a bill in the wrong expense account is
    a reviewer's five-second fix, whereas a failed post blocks payment.
    """
    account_code_map = account_code_map or {}
    lines = []

    for index, line in enumerate(invoice.get("line_items") or [], start=1):
        item_code = str(line.get("item_code") or "").upper()
        lines.append(
            {
                "Description": line.get("description") or f"Line {index}",
                "Quantity": _decimal(line.get("quantity")) or 1,
                "UnitAmount": _decimal(line.get("unit_price"))
                or _decimal(line.get("amount_ex_tax"))
                or 0,
                "LineAmount": _decimal(line.get("amount_ex_tax")),
                "AccountCode": account_code_map.get(
                    item_code, default_account_code
                ),
                # Tax is carried per line so multi-rate invoices survive the
                # mapping; a single header tax cannot express them.
                "TaxAmount": _decimal(line.get("tax_amount")),
            }
        )

    if not lines:
        # Xero rejects a bill with no lines. A single summary line keeps the
        # total correct and visible rather than failing the export outright.
        lines = [
            {
                "Description": f"Invoice {invoice.get('invoice_number') or ''}".strip(),
                "Quantity": 1,
                "UnitAmount": _decimal(invoice.get("invoice_total_ex_tax"))
                or 0,
                "AccountCode": default_account_code,
            }
        ]

    contact: dict[str, Any] = (
        {"ContactID": contact_id}
        if contact_id
        else {"Name": invoice.get("vendor_name") or "Unknown vendor"}
    )

    payload: dict[str, Any] = {
        "Type": "ACCPAY",  # supplier bill, not a customer invoice
        "Contact": contact,
        "LineItems": lines,
        # Draft, never AUTHORISED: approval is a person's decision in Xero.
        "Status": "DRAFT",
        # Our amounts are tax-exclusive; declaring Inclusive would overstate
        # every bill by the tax amount.
        "LineAmountTypes": "Exclusive",
    }

    if invoice.get("invoice_number"):
        payload["InvoiceNumber"] = str(invoice["invoice_number"])
    if invoice.get("invoice_date"):
        payload["Date"] = str(invoice["invoice_date"])
    if invoice.get("due_date"):
        payload["DueDate"] = str(invoice["due_date"])
    if invoice.get("currency"):
        payload["CurrencyCode"] = str(invoice["currency"])
    if invoice.get("po_number"):
        payload["Reference"] = str(invoice["po_number"])

    return payload


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class XeroClient:
    """Thin Xero Accounting API client with retry and rate-limit handling."""

    def __init__(
        self,
        credentials: XeroCredentials,
        transport: Transport,
        max_attempts: int = 3,
        sleep=time.sleep,
    ):
        self.credentials = credentials
        self.transport = transport
        self.max_attempts = max_attempts
        self._sleep = sleep

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.credentials.access_token}",
            "Xero-Tenant-Id": self.credentials.xero_tenant_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _call(self, method: str, path: str, **kwargs) -> Any:
        url = f"{XERO_API}{path}"
        last: XeroError | None = None

        for attempt in range(self.max_attempts):
            status, body = self.transport.request(
                method, url, headers=self._headers(), **kwargs
            )

            if 200 <= status < 300:
                return body

            # 429 and 5xx are transient. 4xx is our payload being wrong, and
            # retrying it just burns rate limit against a request that cannot
            # succeed.
            transient = status == 429 or status >= 500
            last = XeroError(
                _error_message(status, body), status=status, body=body
            )
            if not transient or attempt == self.max_attempts - 1:
                raise last

            delay = _retry_after(body) or (2**attempt)
            self._sleep(delay)

        raise last or XeroError("Xero request failed")

    def find_bill(self, vendor_name: str, invoice_number: str) -> dict | None:
        """Look for an existing bill. The recovery path for a lost response."""
        if not invoice_number:
            return None
        where = f'Type=="ACCPAY" AND InvoiceNumber=="{_escape(invoice_number)}"'
        body = self._call("GET", "/Invoices", params={"where": where})
        for candidate in (body or {}).get("Invoices", []) or []:
            name = (candidate.get("Contact") or {}).get("Name") or ""
            # The query already pinned type and invoice number. If the vendor
            # cannot be compared -- either side missing, or Xero returned a
            # summary without the contact -- treat it as a match and stop.
            #
            # The two mistakes are not equal. Wrongly stopping means a human
            # links or re-posts deliberately; wrongly creating means a duplicate
            # liability reaches the ledger and a payment run. When the evidence
            # is ambiguous, do not post.
            if not vendor_name or not name or _similar(name, vendor_name):
                return candidate
        return None

    def create_bill(self, payload: dict) -> dict:
        body = self._call("POST", "/Invoices", json={"Invoices": [payload]})
        invoices = (body or {}).get("Invoices") or []
        if not invoices:
            raise XeroError(
                "Xero accepted the request but returned no invoice", body=body
            )
        return invoices[0]


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------


class XeroExporter:
    """Posts approved invoices, exactly once."""

    def __init__(
        self, client: XeroClient, account_code_map: dict | None = None
    ):
        self.client = client
        self.account_code_map = account_code_map or {}

    def export(
        self,
        invoice: dict,
        *,
        approved: bool,
        external_id: str | None = None,
    ) -> ExportResult:
        """Post one invoice.

        `external_id` is whatever we already recorded for this invoice; its
        presence alone stops a second post.
        """
        if external_id:
            return ExportResult(
                external_id=external_id,
                status="already_posted",
                message=f"Already posted to Xero as {external_id}; not posting again",
            )

        if not approved:
            return ExportResult(
                status="failed",
                message=(
                    "Invoice is not approved. Bills post only after final "
                    "approval -- posting earlier puts an unreviewed liability "
                    "on the ledger."
                ),
            )

        payload = build_bill_payload(
            invoice, account_code_map=self.account_code_map
        )

        # A previous attempt may have succeeded with its response lost in
        # transit. Creating again would duplicate the liability.
        existing = self.client.find_bill(
            invoice.get("vendor_name") or "",
            str(invoice.get("invoice_number") or ""),
        )
        if existing:
            return ExportResult(
                external_id=existing.get("InvoiceID"),
                status="already_posted",
                message=(
                    f"Xero already has bill {existing.get('InvoiceNumber')} for this "
                    f"vendor; linking to it instead of creating a duplicate"
                ),
                payload=payload,
                xero_response=existing,
            )

        try:
            created = self.client.create_bill(payload)
        except XeroError as exc:
            # Surface Xero's own wording: a reviewer resolving a sync exception
            # needs the ERP's message, not our paraphrase of it.
            return ExportResult(
                status="failed",
                message=str(exc),
                payload=payload,
                xero_response=exc.body if isinstance(exc.body, dict) else {},
            )

        return ExportResult(
            external_id=created.get("InvoiceID"),
            status="created",
            message=f"Created Xero bill {created.get('InvoiceNumber') or ''}".strip(),
            payload=payload,
            xero_response=created,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _escape(value: str) -> str:
    """Escape a value for a Xero `where` clause."""
    return str(value).replace('"', '\\"')


def _similar(left: str, right: str) -> bool:
    """Vendor names differ between systems (trading vs legal names)."""

    def norm(text: str) -> str:
        text = "".join(
            ch for ch in str(text).upper() if ch.isalnum() or ch == " "
        )
        for suffix in (" PTY LTD", " PTY LIMITED", " LIMITED", " LTD", " INC"):
            if text.strip().endswith(suffix):
                text = text.strip()[: -len(suffix)]
        return " ".join(text.split())

    return norm(left) == norm(right)


def _error_message(status: int, body: Any) -> str:
    if isinstance(body, dict):
        for key in ("Message", "Detail", "message", "error_description"):
            if body.get(key):
                return f"Xero {status}: {body[key]}"
        elements = body.get("Elements") or []
        if elements:
            errors = elements[0].get("ValidationErrors") or []
            if errors:
                return f"Xero {status}: " + "; ".join(
                    e.get("Message", "") for e in errors
                )
    return f"Xero {status}: {str(body)[:200]}"


def _retry_after(body: Any) -> float | None:
    if isinstance(body, dict):
        value = body.get("Retry-After") or body.get("retry_after")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return None
