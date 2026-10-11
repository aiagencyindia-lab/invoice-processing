"""What kind of document is this, and is it a tax invoice?

Two different questions, and the classifier could answer neither. It reported
one boolean -- "does this look like an invoice?" -- so a receipt, a quotation,
a proforma or a delivery note with an invoice-shaped header extracted cleanly,
passed check 1.1, and went on to be validated as a payable. 50 of the
benchmark's 300 submissions are exactly that.

Check 1.3 has the opposite problem: a Bill of Supply is a real document from a
real supplier with a real date, and no input tax can be claimed against it. The
presence of three fields cannot tell it from a tax invoice.
"""

import pytest

from invoice_processing.shared_libraries.acting import general_invoice_agent
from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    NON_TAX_INVOICE,
    TAX_INVOICE,
    ClassifierAgent,
    Phase1ValidatorAgent,
)

INVOICE = {
    "invoice_number": "INV-1",
    "invoice_date": "2026-05-25",
    "vendor_name": "Cloudhaven Infra Services Pvt Ltd",
    "vendor_tax_id": "29ZOICE4828P1ZD",
    "customer_name": "Zentrix Cloudworks Private Limited",
    "tax_amount": 128257.92,
    "line_items": [{"description": "Support", "amount_ex_tax": 712544.0}],
}


@pytest.fixture
def config(monkeypatch):
    def apply(**overrides):
        settings = general_invoice_agent.DEFAULT_CONFIG.copy()
        settings.update(
            {"organization_names": ["Zentrix Cloudworks Private Limited"]}
        )
        settings.update(overrides)
        monkeypatch.setitem(
            general_invoice_agent._config_store, "CONFIG", settings
        )

    return apply


def phase1(tmp_path, **extraction):
    payload = {"invoice": dict(INVOICE), **extraction}
    result = Phase1ValidatorAgent().run(tmp_path, payload)
    return {v["step"]: v for v in result["validations"]}


class TestCheck11:
    @pytest.mark.parametrize(
        "document_type",
        [
            "receipt",
            "quotation",
            "proforma_invoice",
            "credit_note",
            "purchase_order",
            "delivery_challan",
            "unreadable",
        ],
    )
    def test_a_document_that_is_not_payable_fails(
        self, tmp_path, config, document_type
    ):
        config()
        step = phase1(tmp_path, document_type=document_type)["1.1"]
        assert step["passed"] is False
        assert (
            step["rejection_template"] == "Document is not a valid Tax Invoice"
        )
        assert document_type.replace("_", " ") in step["evidence"]

    def test_a_tax_invoice_passes(self, tmp_path, config):
        config()
        assert (
            phase1(tmp_path, document_type=TAX_INVOICE)["1.1"]["passed"] is True
        )

    def test_an_untyped_document_behaves_as_before(self, tmp_path, config):
        """Nothing classified it -- an older artifact, or a classifier failure.
        Silence must not become a rejection."""
        config()
        assert phase1(tmp_path)["1.1"]["passed"] is True

    def test_a_failed_extraction_still_fails(self, tmp_path, config):
        config()
        step = phase1(
            tmp_path, document_type=TAX_INVOICE, extraction_failed=True
        )["1.1"]
        assert step["passed"] is False


class TestCheck13:
    def test_a_bill_of_supply_is_not_a_tax_invoice(self, tmp_path, config):
        """It fails 1.3, not 1.1: it is a genuine document, just not one input
        tax can be claimed against."""
        config(require_tax_breakup=True)
        steps = phase1(tmp_path, document_type=NON_TAX_INVOICE)
        assert steps["1.1"]["passed"] is True
        assert steps["1.3"]["passed"] is False
        assert (
            steps["1.3"]["rejection_template"]
            == "Document is not a Tax Invoice"
        )

    def test_a_tax_inclusive_total_is_still_a_tax_invoice(
        self, tmp_path, config
    ):
        """An invoice reading "Amount payable, inclusive of all applicable
        taxes" over a supplier registration is a tax invoice. It failed this
        check for five real invoices in the benchmark because no tax amount
        could be extracted -- which is the itemisation question that check 2.1
        exists to ask."""
        config(require_tax_breakup=True)
        payload = {
            "invoice": {**INVOICE, "tax_amount": None, "document_type": None}
        }
        result = Phase1ValidatorAgent().run(
            tmp_path, {**payload, "document_type": TAX_INVOICE}
        )
        step = next(v for v in result["validations"] if v["step"] == "1.3")
        assert step["passed"] is True

    def test_it_is_only_required_when_configured(self, tmp_path, config):
        """Suppliers in plenty of places legitimately bill without tax, and a
        tenant who has not asked for this must not have them rejected."""
        config(require_tax_breakup=False)
        payload = {"invoice": {**INVOICE, "tax_amount": None}}
        result = Phase1ValidatorAgent().run(tmp_path, payload)
        step = next(v for v in result["validations"] if v["step"] == "1.3")
        assert step["passed"] is True

    def test_component_tax_counts_as_tax(self, tmp_path, config):
        """An Indian invoice splits its tax into CGST and SGST and may state no
        single total, which is not the same as stating no tax."""
        config(require_tax_breakup=True)
        payload = {
            "invoice": {
                **INVOICE,
                "tax_amount": None,
                "cgst_amount": 64128.96,
                "sgst_amount": 64128.96,
            }
        }
        result = Phase1ValidatorAgent().run(tmp_path, payload)
        step = next(v for v in result["validations"] if v["step"] == "1.3")
        assert step["passed"] is True


class TestCheck12TaxIds:
    def test_a_registration_that_is_not_ours_fails(self, tmp_path, config):
        """The name matched. Names can be typed; registrations are issued."""
        config(organization_tax_ids=["29AAFCZ4821L1ZS"], tax_id_format="GSTIN")
        payload = {"invoice": {**INVOICE, "customer_tax_id": "24AAECF9034M1ZE"}}
        result = Phase1ValidatorAgent().run(tmp_path, payload)
        step = next(v for v in result["validations"] if v["step"] == "1.2")
        assert step["passed"] is False
        assert (
            step["rejection_template"]
            == "Invoice addressed to different company"
        )

    def test_our_own_registration_passes(self, tmp_path, config):
        config(organization_tax_ids=["29AAFCZ4821L1ZS"], tax_id_format="GSTIN")
        payload = {
            "invoice": {**INVOICE, "customer_tax_id": "29 AAFCZ 4821 L1ZS"}
        }
        result = Phase1ValidatorAgent().run(tmp_path, payload)
        step = next(v for v in result["validations"] if v["step"] == "1.2")
        assert step["passed"] is True

    def test_an_unreadable_registration_does_not_reject_the_invoice(
        self, tmp_path, config
    ):
        """The extractor read 24AAECF9034MIZE off a scan where the document
        says 24AAECF9034M1ZE -- a 1 read as an I. Rejecting the invoice for
        that is rejecting it for our own reading. The checksum tells them
        apart: a misread identifier fails it."""
        config(organization_tax_ids=["24AAECF9034M1ZE"], tax_id_format="GSTIN")
        payload = {"invoice": {**INVOICE, "customer_tax_id": "24AAECF9034MIZE"}}
        result = Phase1ValidatorAgent().run(tmp_path, payload)
        step = next(v for v in result["validations"] if v["step"] == "1.2")
        assert step["passed"] is True
        assert "could not be read" in step["evidence"]

    def test_no_configured_registration_leaves_the_name_check_alone(
        self, tmp_path, config
    ):
        config(organization_tax_ids=[])
        payload = {"invoice": {**INVOICE, "customer_tax_id": "24AAECF9034M1ZE"}}
        result = Phase1ValidatorAgent().run(tmp_path, payload)
        step = next(v for v in result["validations"] if v["step"] == "1.2")
        assert step["passed"] is True


class TestKeywordFallback:
    """When the model call fails, the fallback used to answer "invoice" for any
    text containing the word -- which made a quotation a payable."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "QUOTATION\nQuotation No.: QT-7732\nValid for 30 days",
                "quotation",
            ),
            ("PROFORMA INVOICE\nAdvance payment requested", "proforma_invoice"),
            ("CREDIT NOTE\nAgainst invoice INV-1", "credit_note"),
            ("DELIVERY CHALLAN\nGoods dispatched", "delivery_challan"),
            ("PURCHASE ORDER\nPlease supply", "purchase_order"),
            # A receipt note quotes its PO, so it must win over "purchase order".
            (
                "GOODS RECEIPT NOTE\nGRN No.: GRN-1\nPurchase Order: PO-4471",
                "goods_receipt",
            ),
            ("GRN No.: GRN-88\nReceived in good condition", "goods_receipt"),
            ("GOODS RECEIVED NOTE\nStores", "goods_receipt"),
            ("MATERIAL RECEIPT NOTE\nMRN-12", "goods_receipt"),
            # A whole word only: "IMRN-2" is a reference, not a receipt note.
            ("INVOICE\nRef: IMRN-2", NON_TAX_INVOICE),
            ("RECEIPT\nPayment received with thanks", "receipt"),
            ("BILL OF SUPPLY\nComposition scheme", NON_TAX_INVOICE),
            ("TAX INVOICE\nGSTIN: 29ZOICE4828P1ZD", TAX_INVOICE),
            # An invoice quoting the receipt it bills is still an invoice.
            ("TAX INVOICE\nGRN No: 123", TAX_INVOICE),
            # A supporting marker counts only in the title region, and only
            # when no invoice marker comes before it.
            ("TAX INVOICE\nPurchase Order No: PO-1", TAX_INVOICE),
            ("TAX INVOICE\nInvoice No: 9\nGRN No: 5", TAX_INVOICE),
            (
                "ACME TRADERS\nTAX INVOICE\nGSTIN: 29AAA\nInvoice No: 9\n"
                "Date: 01-04-2026\nDelivery Challan No: DC-4\nPO No: 7",
                TAX_INVOICE,
            ),
            (
                "ACME TRADERS\nPlot 4\nPeenya\nBengaluru\nPh 080\n"
                "Bill of supply 22\nPurchase Order: PO-1",
                NON_TAX_INVOICE,
            ),
            # "Invoice To" is an address, not a title: still a PO.
            (
                "ACME LTD\nInvoice To: Foo Pvt Ltd\nPURCHASE ORDER\nPO No 5\n"
                "GSTIN 29AAA",
                "purchase_order",
            ),
            ("ACME LTD\nInvoice No: 4\nGOODS RECEIPT NOTE", "goods_receipt"),
            # An invoice marker on the same line as a supporting one wins.
            ("PURCHASE ORDER / TAX INVOICE\nGSTIN 29AAA", TAX_INVOICE),
            ("TAX INVOICE CUM DELIVERY CHALLAN\nGSTIN 29AAA", TAX_INVOICE),
            ("INVOICE AGAINST PURCHASE ORDER\nPO-1", NON_TAX_INVOICE),
            # Untitled: the markers are only in the body, so nothing is told.
            (
                "ACME TRADERS\nPlot 4\nPeenya\nBengaluru\nPh 080\n"
                "GRN No: 5\nPurchase Order: PO-1",
                "other",
            ),
        ],
    )
    def test_it_reads_the_title(self, text, expected):
        assert (
            ClassifierAgent()._classify_by_keyword(text).document_type
            == expected
        )

    def test_only_a_payable_document_is_counted_as_an_invoice(self):
        result = ClassifierAgent()._classify_by_keyword("QUOTATION\nTotal 100")
        assert result.has_invoice is False
        assert result.invoice_count == 0

    def test_an_empty_page_is_unreadable_not_an_invoice(self):
        result = ClassifierAgent()._classify_by_keyword("   ")
        assert result.document_type == "unreadable"
        assert result.has_invoice is False


class TestGoodsReceipt:
    """A GRN is its own kind: receipt evidence for the three-way match, not a
    payable and not a delivery challan."""

    def test_it_is_a_known_non_payable_type(self):
        assert "goods_receipt" in general_invoice_agent.DOCUMENT_TYPES
        assert "goods_receipt" in general_invoice_agent.NON_PAYABLE_TYPES

    def test_the_prompt_names_it(self, monkeypatch):
        seen = {}

        def capture(prompt, model, schema):
            seen["prompt"] = prompt
            return schema(document_type="goods_receipt"), None

        monkeypatch.setattr(general_invoice_agent, "call_gemini", capture)
        ClassifierAgent()._analyze_document("GRN", "grn.pdf")
        assert '"goods_receipt"' in seen["prompt"]

    def test_a_receipt_note_is_not_counted_as_an_invoice(self):
        result = ClassifierAgent()._classify_by_keyword("GOODS RECEIPT NOTE")
        assert result.has_invoice is False
        assert result.invoice_count == 0
