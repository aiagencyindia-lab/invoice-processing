"""Trust Audit (Wave 2): ledger + fee, scope and sampling, report, store, batch."""

from decimal import Decimal

import pytest

from invoice_processing.trust.audit import ledger

# -- ledger + fee -------------------------------------------------------------

TERMS = {
    "recovered_pct": "25",
    "prevented_pct": "10",
    "cap": None,
    "minimum": None,
}


@pytest.mark.parametrize(
    ("terms", "recovered", "prevented", "expected"),
    [
        (
            TERMS,
            "1000",
            "500",
            ("250.00", "50.00", "300.00", "300.00", "300.00"),
        ),
        (TERMS, "0", "0", ("0.00", "0.00", "0.00", "0.00", "0.00")),
        # ROUND_HALF_UP at the half cent: 0.125 -> 0.13, 0.1 * 0.05 = 0.005 -> 0.01
        (TERMS, "0.5", "0.05", ("0.13", "0.01", "0.14", "0.14", "0.14")),
        (
            {**TERMS, "cap": "100"},
            "1000",
            "0",
            ("250.00", "0.00", "250.00", "100.00", "100.00"),
        ),
        (
            {**TERMS, "minimum": "400"},
            "1000",
            "0",
            ("250.00", "0.00", "250.00", "250.00", "400.00"),
        ),
        # cap then minimum: the minimum wins over the cap when it is higher
        (
            {**TERMS, "cap": "100", "minimum": "150"},
            "1000",
            "0",
            ("250.00", "0.00", "250.00", "100.00", "150.00"),
        ),
        (
            {"recovered_pct": "12.5", "prevented_pct": "0"},
            "10",
            "99",
            ("1.25", "0.00", "1.25", "1.25", "1.25"),
        ),
    ],
)
def test_fee_table(terms, recovered, prevented, expected):
    out = ledger.fee(terms, Decimal(recovered), Decimal(prevented))
    assert (
        out["fee_recovered"],
        out["fee_prevented"],
        out["gross"],
        out["capped"],
        out["due"],
    ) == expected
    assert all(isinstance(v, str) for v in out.values())


def _event(fid, to, amount, **kw):
    return {
        "finding_id": fid,
        "type": kw.get("type", "LEAK-RATE"),
        "to_status": to,
        "amount": amount,
        "actor": kw.get("actor", "a@x.test"),
        "at": kw.get("at", "2026-10-01T00:00:00+00:00"),
        "reason": kw.get("reason"),
    }


def test_ledger_rows_keep_only_scope_money_events_and_totals_add_up():
    events = [
        _event("f1", "detected", "100.0000"),
        _event("f1", "claimed", "100.0000"),
        _event("f1", "recovered", "80.5000", reason="partial"),
        _event("f2", "prevented", "20.0000"),
        _event("f3", "recovered", "999"),  # out of scope
        _event("f4", "written_off", None, reason="gone"),
    ]
    rows = ledger.rows({"f1": "Acme Steel", "f2": None, "f4": "Globex"}, events)
    assert [r["to_status"] for r in rows] == [
        "claimed",
        "recovered",
        "prevented",
        "written_off",
    ]
    assert rows[1] == {
        "finding_id": "f1",
        "type": "LEAK-RATE",
        "vendor": "Acme Steel",
        "to_status": "recovered",
        "amount": "80.50",
        "attested_by": "a@x.test",
        "at": "2026-10-01T00:00:00+00:00",
        "reason": "partial",
    }
    assert rows[3]["amount"] is None
    assert ledger.totals(rows) == {
        "recovered": "80.50",
        "prevented": "20.00",
        "written_off": "0.00",
        "claimed": "100.00",
    }
