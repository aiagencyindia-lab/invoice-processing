"""The six trust dimensions (FR-25): pure, table-driven rules."""

from decimal import Decimal

import pytest

from invoice_processing.trust.modes import ENGINES
from invoice_processing.trust.score import dimensions as dm

ALL = set(ENGINES)


def f(type_, status="detected", impact=None, fid=None, **details):
    return {
        "id": fid or f"f-{type_}",
        "type": type_,
        "status": status,
        "impact": impact,
        "details": details,
    }


def dims(findings=(), *, enforcing=(), running=ALL, **kw):
    base = {
        "findings": list(findings),
        "amount": Decimal("1000.00"),
        "approvals": [],
        "decision": None,
        "overridden": False,
        "live": True,
        "reality": None,
        "document": None,
        "has_contract": False,
        "has_history": True,
    }
    base.update(kw)
    return dm.dimensions(
        dm.Inputs(**base), enforcing=set(enforcing), running=set(running)
    )


def value(dim, *args, **kw):
    return dims(*args, **kw)[dim]["value"]


@pytest.mark.parametrize(
    ("findings", "expected"),
    [
        ([], 1.0),
        ([f("PAY-UNVERIFIED")], 0.0),
        ([f("PAY-NAME-MISMATCH")], 0.0),
        ([f("PAY-BEC")], 0.0),
        ([f("GST-INACTIVE")], 0.0),
        ([f("GST-UNVERIFIED")], 0.0),
        ([f("GST-NONFILER")], 0.6),
        ([f("GST-ITC-RISK"), f("GST-SHARED-IDENTITY")], 0.6),
        ([f("PAY-UNVERIFIED", status="rejected")], 1.0),
    ],
)
def test_identity(findings, expected):
    assert value("identity", findings) == expected


def ap(status, by=None):
    return {"status": status, "decided_by": by}


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"approvals": [ap("approved", "a@x"), ap("approved", "b@x")]}, 1.0),
        (
            {"approvals": [ap("approved", "a@x")], "overridden": True},
            0.7,
        ),
        ({"approvals": [ap("approved", "a@x"), ap("pending")]}, 0.5),
        ({"approvals": [ap("approved", "a@x"), ap("rejected", "b@x")]}, 0.0),
        ({"approvals": [ap("approved")]}, 0.5),  # nobody decided it
        ({"decision": "approve"}, 1.0),
        ({"decision": "approve", "overridden": True}, 0.7),
        ({"decision": "reject"}, 0.0),
        ({}, 0.5),
        ({"live": False}, None),
    ],
)
def test_authorization(kw, expected):
    assert value("authorization", **kw) == expected


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("CORROBORATED", 1.0),
        ("PARTIAL", 0.6),
        ("NO_EVIDENCE", 0.4),
        ("CONTRADICTED", 0.0),
        ("SOMETHING_NEW", None),
    ],
)
def test_reality(result, expected):
    assert value("reality", reality={"result": result}) == expected


def test_reality_without_a_check_is_null():
    d = dims()["reality"]
    assert d["value"] is None and d["note"]


def test_an_open_contradiction_is_zero_whatever_the_node_says():
    assert (
        value(
            "reality",
            [f("REAL-CONTRADICTED")],
            reality={"result": "PARTIAL"},
        )
        == 0.0
    )


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"has_contract": True}, 1.0),
        (
            {
                "has_contract": True,
                "findings": [
                    f("LEAK-RATE", impact="100.0000", fid="a"),
                    f("LEAK-TIER", impact="150.0000", fid="b"),
                    f(
                        "LEAK-TERM", impact="999.0000", fid="c"
                    ),  # not price leak
                ],
            },
            0.75,
        ),
        (
            {
                "has_contract": True,
                "findings": [f("LEAK-RATE", impact="5000.0000")],
            },
            0.0,
        ),
        (
            {
                "has_contract": True,
                "amount": Decimal("0"),
                "findings": [f("LEAK-RATE", impact="1.0000")],
            },
            0.0,
        ),
        (
            {
                "has_contract": True,
                "amount": None,
                "findings": [f("LEAK-RATE", impact="1.0000")],
            },
            0.0,
        ),
        ({"findings": [f("REAL-PRICE-JUMP")]}, 0.3),
        ({}, 1.0),
        ({"has_history": False}, None),
    ],
)
def test_price(kw, expected):
    assert value("price", **kw) == expected


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"document": {"score": 0.82}}, 0.82),
        ({"document": {"score": 0.82}, "findings": [f("PROV-TAMPER")]}, 0.0),
        ({"document": {}}, None),
        ({}, None),
    ],
)
def test_authenticity(kw, expected):
    assert value("authenticity", **kw) == expected


@pytest.mark.parametrize(
    ("findings", "expected"),
    [
        ([], 1.0),
        ([f("PAY-ANOMALY", signals={"first_time_vendor": {}})], 0.5),
        ([f("PAY-ANOMALY", signals={"amount_zscore": {}, "cadence": {}})], 0.0),
        ([f("REAL-VOLUME-SPIKE")], 0.4),
        (
            [
                f("PAY-ANOMALY", signals={"cadence": {}}),
                f("REAL-VOLUME-SPIKE"),
            ],
            0.4,
        ),
    ],
)
def test_consistency(findings, expected):
    assert value("consistency", findings) == expected


def test_drivers_are_the_open_findings_of_the_dimension():
    d = dims(
        [
            f("PAY-UNVERIFIED", fid="p1"),
            f("GST-NONFILER", fid="g1"),
            f("PAY-BEC", status="rejected", fid="p2"),
            f("DUP-NEAR", fid="d1"),
        ]
    )
    assert sorted(d["identity"]["drivers"]) == ["g1", "p1"]
    assert d["consistency"]["drivers"] == ["d1"]


def test_a_hold_counts_only_when_its_engine_enforces():
    findings = [f("PAY-UNVERIFIED", engine="payment", fid="p1")]
    assert dims(findings, enforcing={"payment"})["identity"]["hold"] is True
    assert dims(findings, enforcing={"gst_itc"})["identity"]["hold"] is False
    assert dims(findings)["identity"]["hold"] is False
    assert dm.holds(findings, {"payment"}) == ["p1"]
    assert dm.holds(findings, set()) == []


def test_a_dimension_whose_engines_are_off_is_null():
    d = dims(running={"provenance"}, document={"score": 0.9})
    assert d["identity"]["value"] is None
    assert d["price"]["value"] is None
    assert d["consistency"]["value"] is None
    assert d["authenticity"]["value"] == 0.9
    # A finding of a stopped engine still speaks.
    assert (
        dims([f("PAY-UNVERIFIED")], running={"provenance"})["identity"]["value"]
        == 0.0
    )


def test_composite_is_the_mean_of_the_known_values():
    d = dims(document={"score": 0.5}, reality={"result": "PARTIAL"})
    known = [v["value"] for v in d.values() if v["value"] is not None]
    assert dm.composite(d) == round(sum(known) / len(known), 4)
    assert dm.composite({k: {"value": None} for k in d}) is None
