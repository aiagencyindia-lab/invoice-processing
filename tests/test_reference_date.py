"""Step 3.3 needs a reference date it can be held to.

"Is this invoice dated in the future?" was answered against `date.today()`,
read at the moment of validation. Two consequences: the same document gets a
different verdict depending on when it is processed, and a corpus with a stated
reference date -- an evaluation set, a regression fixture, a replay of last
month's queue -- cannot be reproduced at all.

`INVOICE_REFERENCE_DATE` is deliberately an environment variable and not a
tenant setting: a customer able to move the clock could accept invoices dated
into next year, which is precisely the fraud the check exists to stop.
"""

from datetime import date, timedelta

import pytest

from invoice_processing.shared_libraries.acting import general_invoice_agent
from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    Phase3ValidatorAgent,
    parse_date,
)


def extraction(invoice_date: str) -> dict:
    return {
        "invoice": {
            "invoice_number": "INV-1",
            "invoice_date": invoice_date,
            "vendor_name": "Vendor",
            "vendor_tax_id": "51824753556",
            "line_items": [],
        }
    }


def step_33(tmp_path, invoice_date: str) -> dict | None:
    result = Phase3ValidatorAgent(seen_invoices=lambda exclude_case_id="": []).run(
        tmp_path, extraction(invoice_date), {}
    )
    return next((v for v in result["validations"] if v["step"] == "3.3"), None)


class TestTheReferenceDate:
    def test_it_comes_from_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INVOICE_REFERENCE_DATE", "2026-09-03")
        assert step_33(tmp_path, "2026-09-04")["passed"] is False
        assert step_33(tmp_path, "2026-09-03")["passed"] is True

    def test_unset_means_today(self, tmp_path, monkeypatch):
        monkeypatch.delenv("INVOICE_REFERENCE_DATE", raising=False)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        assert step_33(tmp_path, tomorrow)["passed"] is False
        assert step_33(tmp_path, date.today().isoformat())["passed"] is True

    def test_an_unreadable_setting_falls_back_to_today_rather_than_failing(
        self, tmp_path, monkeypatch
    ):
        """A typo in an environment variable must not stop invoices being
        processed, and must not silently accept future-dated ones either."""
        monkeypatch.setenv("INVOICE_REFERENCE_DATE", "not-a-date")
        assert general_invoice_agent._reference_date() == date.today()


class TestDateFormats:
    @pytest.mark.parametrize(
        "printed",
        [
            "2026-08-09",
            "09/08/2026",
            "09-08-2026",
            "2026/08/09",
            "09-Aug-2026",
            "09.08.2026",
            "09 August 2026",
            "Aug 09, 2026",
        ],
    )
    def test_the_formats_real_invoices_print(self, printed):
        """Seven date formats appear across the benchmark corpus alone, and an
        unparseable date means step 3.3 emits no verdict at all -- a future
        date passes unremarked because nobody looked."""
        assert parse_date(printed) == date(2026, 8, 9)

    def test_an_unreadable_date_is_still_none(self):
        assert parse_date("sometime last spring") is None
