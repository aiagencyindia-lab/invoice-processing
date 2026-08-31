"""Xero export, with never-post-twice as the property under test.

A duplicate bill in Xero is a duplicate liability. It reaches the ledger,
supplier statements and eventually a payment run, and unwinding it is manual
work done under time pressure at month end. Everything else this connector does
is secondary to not doing that.

The nastiest version is not a careless second click: it is a network timeout
*after* Xero has already committed the bill. From our side that is
indistinguishable from a failure, so a naive retry duplicates the liability.
`test_recovers_from_a_lost_response` is that scenario.

No test touches the network -- a fake transport records requests and returns
canned Xero responses.
"""

import pytest

from invoice_processing.connectors.xero import (
    AlreadyPosted,  # noqa: F401  (exported for platform use)
    ExportResult,
    XeroClient,
    XeroCredentials,
    XeroError,
    XeroExporter,
    build_bill_payload,
)

INVOICE = {
    "invoice_number": "INV-00005",
    "invoice_date": "2026-02-11",
    "due_date": "2026-03-13",
    "vendor_name": "QuickFix HVAC Repairs",
    "vendor_tax_id": "51824753556",
    "currency": "AUD",
    "invoice_total_ex_tax": 972.11,
    "tax_amount": 97.21,
    "invoice_total_inc_tax": 1069.32,
    "po_number": "PO-4471",
    "line_items": [
        {
            "description": "Gasket and seal kit",
            "item_code": "PARTS",
            "quantity": 3,
            "unit_price": 219.87,
            "amount_ex_tax": 659.61,
            "tax_amount": 65.96,
        },
        {
            "description": "Technician labour",
            "item_code": "LABOUR",
            "quantity": 2.5,
            "unit_price": 125.0,
            "amount_ex_tax": 312.50,
            "tax_amount": 31.25,
        },
    ],
}

CREATED = {
    "InvoiceID": "xero-uuid-1234",
    "InvoiceNumber": "INV-00005",
    "Status": "DRAFT",
}


class FakeTransport:
    """Records requests; replays queued responses. Never hits the network."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests = []

    def request(self, method, url, *, headers, json=None, params=None):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "json": json,
                "params": params,
            }
        )
        if not self.responses:
            return 200, {"Invoices": []}
        return self.responses.pop(0)

    @property
    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]


def _exporter(responses=None, **kwargs):
    transport = FakeTransport(responses)
    client = XeroClient(
        XeroCredentials("token-abc", "xero-tenant-1"),
        transport,
        sleep=lambda _s: None,
    )
    return XeroExporter(client, **kwargs), transport


class TestNeverPostsTwice:
    """The property that protects real money."""

    def test_known_external_id_short_circuits(self):
        """Already recorded as posted: do not even ask Xero."""
        exporter, transport = _exporter()
        result = exporter.export(
            INVOICE, approved=True, external_id="xero-uuid-1234"
        )

        assert result.status == "already_posted"
        assert result.external_id == "xero-uuid-1234"
        assert transport.requests == [], "no call should be made at all"

    def test_recovers_from_a_lost_response(self):
        """The dangerous case: Xero committed, the response never arrived.

        A retry must find the existing bill and link to it, not create a second.
        """
        exporter, transport = _exporter(
            responses=[(200, {"Invoices": [CREATED]})]  # the search finds it
        )
        result = exporter.export(INVOICE, approved=True)

        assert result.status == "already_posted"
        assert result.external_id == "xero-uuid-1234"
        assert transport.posts == [], "created a duplicate bill"

    def test_searches_before_creating(self):
        exporter, transport = _exporter(
            responses=[(200, {"Invoices": []}), (200, {"Invoices": [CREATED]})]
        )
        exporter.export(INVOICE, approved=True)

        assert transport.requests[0]["method"] == "GET", (
            "posted without checking first"
        )
        assert "INV-00005" in transport.requests[0]["params"]["where"]

    def test_a_different_vendors_same_number_is_not_a_match(self):
        """Invoice numbers are unique per vendor, not globally."""
        other_vendor = {**CREATED, "Contact": {"Name": "Someone Else Entirely"}}
        exporter, transport = _exporter(
            responses=[
                (200, {"Invoices": [other_vendor]}),
                (200, {"Invoices": [CREATED]}),
            ]
        )
        result = exporter.export(INVOICE, approved=True)

        assert result.status == "created", (
            "matched a different vendor's invoice"
        )
        assert len(transport.posts) == 1

    def test_ambiguous_vendor_stops_rather_than_posting(self):
        """When the vendor cannot be compared, do not create.

        The two mistakes are not equal. Wrongly stopping means a human links or
        re-posts deliberately. Wrongly creating puts a duplicate liability on
        the ledger and into a payment run.
        """
        no_contact = {k: v for k, v in CREATED.items() if k != "Contact"}
        exporter, transport = _exporter(
            responses=[(200, {"Invoices": [no_contact]})]
        )
        result = exporter.export(INVOICE, approved=True)

        assert result.status == "already_posted"
        assert transport.posts == [], "posted despite an unresolved duplicate"

    def test_vendor_name_variants_still_match(self):
        """Trading vs legal names must not cause a duplicate post."""
        variant = {
            **CREATED,
            "Contact": {"Name": "QuickFix HVAC Repairs Pty Ltd"},
        }
        exporter, transport = _exporter(
            responses=[(200, {"Invoices": [variant]})]
        )
        result = exporter.export(INVOICE, approved=True)

        assert result.status == "already_posted"
        assert transport.posts == []


class TestApprovalGate:
    def test_unapproved_invoice_is_not_posted(self):
        exporter, transport = _exporter()
        result = exporter.export(INVOICE, approved=False)

        assert result.status == "failed"
        assert "not approved" in result.message
        assert transport.requests == []

    def test_bill_is_created_as_draft(self):
        """Never AUTHORISED: approval in the ledger is a person's decision."""
        payload = build_bill_payload(INVOICE)
        assert payload["Status"] == "DRAFT"


class TestPayloadMapping:
    def test_is_a_supplier_bill_not_a_customer_invoice(self):
        """ACCREC would file a payable as a receivable."""
        assert build_bill_payload(INVOICE)["Type"] == "ACCPAY"

    def test_line_amounts_are_declared_exclusive(self):
        """Declaring Inclusive would overstate every bill by the tax."""
        assert build_bill_payload(INVOICE)["LineAmountTypes"] == "Exclusive"

    def test_header_fields_map(self):
        payload = build_bill_payload(INVOICE)
        assert payload["InvoiceNumber"] == "INV-00005"
        assert payload["Date"] == "2026-02-11"
        assert payload["DueDate"] == "2026-03-13"
        assert payload["CurrencyCode"] == "AUD"
        assert payload["Reference"] == "PO-4471"

    def test_line_items_map_with_amounts(self):
        lines = build_bill_payload(INVOICE)["LineItems"]
        assert len(lines) == 2
        assert lines[0]["Description"] == "Gasket and seal kit"
        assert lines[0]["Quantity"] == 3
        assert lines[0]["UnitAmount"] == 219.87
        assert lines[0]["LineAmount"] == 659.61

    def test_line_totals_match_the_invoice(self):
        """A mapping that changes the total is worse than one that fails."""
        lines = build_bill_payload(INVOICE)["LineItems"]
        assert round(sum(line["LineAmount"] for line in lines), 2) == 972.11

    def test_per_line_tax_is_preserved(self):
        """Header-only tax cannot express a multi-rate invoice."""
        lines = build_bill_payload(INVOICE)["LineItems"]
        assert lines[0]["TaxAmount"] == 65.96
        assert lines[1]["TaxAmount"] == 31.25

    def test_account_codes_map_from_item_codes(self):
        payload = build_bill_payload(
            INVOICE, account_code_map={"PARTS": "310", "LABOUR": "477"}
        )
        assert payload["LineItems"][0]["AccountCode"] == "310"
        assert payload["LineItems"][1]["AccountCode"] == "477"

    def test_unmapped_code_falls_back_rather_than_failing(self):
        """A wrong expense account is a five-second fix; a failed post blocks payment."""
        payload = build_bill_payload(INVOICE, account_code_map={})
        assert payload["LineItems"][0]["AccountCode"] == "429"

    def test_invoice_with_no_lines_still_posts(self):
        """Xero rejects a bill with no lines; a summary line keeps the total."""
        payload = build_bill_payload({**INVOICE, "line_items": []})
        assert len(payload["LineItems"]) == 1
        assert payload["LineItems"][0]["UnitAmount"] == 972.11

    def test_missing_optional_fields_are_omitted_not_nulled(self):
        minimal = {"vendor_name": "V", "line_items": []}
        payload = build_bill_payload(minimal)
        assert "DueDate" not in payload
        assert "Reference" not in payload


class TestErrorHandling:
    def test_xero_message_is_surfaced_verbatim(self):
        """A reviewer resolving a sync exception needs the ERP's own wording."""
        exporter, _ = _exporter(
            responses=[
                (200, {"Invoices": []}),
                (400, {"Message": "Account code 429 is archived"}),
            ]
        )
        result = exporter.export(INVOICE, approved=True)

        assert result.status == "failed"
        assert "Account code 429 is archived" in result.message

    def test_validation_errors_are_surfaced(self):
        exporter, _ = _exporter(
            responses=[
                (200, {"Invoices": []}),
                (
                    400,
                    {
                        "Elements": [
                            {
                                "ValidationErrors": [
                                    {"Message": "Contact is required"}
                                ]
                            }
                        ]
                    },
                ),
            ]
        )
        result = exporter.export(INVOICE, approved=True)
        assert "Contact is required" in result.message

    def test_client_errors_are_not_retried(self):
        """Retrying a bad payload burns rate limit on a request that cannot work."""
        transport = FakeTransport([(400, {"Message": "bad"})] * 5)
        client = XeroClient(
            XeroCredentials("t", "x"), transport, sleep=lambda _s: None
        )
        with pytest.raises(XeroError):
            client.create_bill({})
        assert len(transport.requests) == 1

    def test_rate_limit_is_retried(self):
        transport = FakeTransport(
            [(429, {"Retry-After": 0}), (200, {"Invoices": [CREATED]})]
        )
        client = XeroClient(
            XeroCredentials("t", "x"), transport, sleep=lambda _s: None
        )
        assert client.create_bill({})["InvoiceID"] == "xero-uuid-1234"
        assert len(transport.requests) == 2

    def test_server_errors_are_retried_then_surface(self):
        transport = FakeTransport([(503, {"Message": "unavailable"})] * 3)
        client = XeroClient(
            XeroCredentials("t", "x"), transport, sleep=lambda _s: None
        )
        with pytest.raises(XeroError) as exc:
            client.create_bill({})
        assert exc.value.status == 503
        assert len(transport.requests) == 3

    def test_a_failed_export_records_no_external_id(self):
        """Otherwise a later attempt would think it had already posted."""
        exporter, _ = _exporter(
            responses=[(200, {"Invoices": []}), (400, {"Message": "nope"})]
        )
        result = exporter.export(INVOICE, approved=True)
        assert result.external_id is None


class TestAuthHeaders:
    def test_tenant_and_token_are_sent(self):
        exporter, transport = _exporter(
            responses=[(200, {"Invoices": []}), (200, {"Invoices": [CREATED]})]
        )
        exporter.export(INVOICE, approved=True)

        headers = transport.requests[0]["headers"]
        assert headers["Authorization"] == "Bearer token-abc"
        assert headers["Xero-Tenant-Id"] == "xero-tenant-1"

    def test_no_secret_is_handled_by_this_module(self):
        """Tokens come from the credential store; secrets never reach here."""
        source = open(
            __import__(
                "invoice_processing.connectors.xero", fromlist=["x"]
            ).__file__
        ).read()
        assert "client_secret" not in source


def test_successful_export_reports_the_external_id():
    exporter, _ = _exporter(
        responses=[(200, {"Invoices": []}), (200, {"Invoices": [CREATED]})]
    )
    result = exporter.export(INVOICE, approved=True)

    assert isinstance(result, ExportResult)
    assert result.status == "created"
    assert result.external_id == "xero-uuid-1234"
