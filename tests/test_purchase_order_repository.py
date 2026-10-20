"""Purchase orders, their lines and goods receipts are tenant tables."""

from datetime import datetime, timezone

import pytest
from fresh_schema import fresh_schema
from sqlalchemy import inspect

from invoice_processing.db import models
from invoice_processing.db.repositories import PgPurchaseOrderRepository
from invoice_processing.db.session import get_engine, tenant_session


def test_tables_are_tenant_tables_and_build(scratch_db):
    url = scratch_db("po.db")
    fresh_schema(url)
    names = ("purchase_orders", "po_lines", "goods_receipts", "grn_lines")
    assert set(names) <= set(models.TENANT_TABLES)
    assert all(inspect(get_engine(url)).has_table(n) for n in names)


@pytest.fixture
def repo(scratch_db):
    url = scratch_db("po_repo.db")
    fresh_schema(url)
    for tenant_id in ("t1", "t2"):
        with tenant_session(tenant_id, url) as session:
            session.add(
                models.Tenant(id=tenant_id, name=tenant_id, slug=tenant_id)
            )
    return PgPurchaseOrderRepository(url), url


ORDER = {
    "po_number": "PO-9001",
    "vendor_name": "Acme Pty Ltd",
    "total_amount": 1000,
    "currency": "INR",
    "lines": [
        {
            "line_number": 1,
            "item_code": "B-6205",
            "description": "Ball bearing 6205",
            "qty_ordered": 10,
            "unit_price": 100,
            "qty_invoiced": 2,
        },
        {
            "line_number": 2,
            "description": "Freight",
            "qty_ordered": 1,
            "unit_price": 50,
            "receipt_required": False,
        },
    ],
}
GRN = {
    "grn_number": "GRN-1",
    "po_number": "PO-9001",
    "received_on": "2026-10-01",
    "lines": [{"line_number": 1, "qty_received": 6}],
}


def test_upsert_updates_lines_and_resets_opening(repo):
    r, _ = repo
    assert r.upsert_orders("t1", [ORDER], uploaded_by="a@x") == {
        "created": 1,
        "updated": 0,
        "lines": 2,
    }
    again = {
        **ORDER,
        "lines": [
            {
                "line_number": 1,
                "description": "Ball bearing 6205",
                "qty_ordered": 12,
                "unit_price": 100,
                "qty_invoiced": 5,
            }
        ],
    }
    assert r.upsert_orders("t1", [again], uploaded_by="a@x") == {
        "created": 0,
        "updated": 1,
        "lines": 1,
    }
    order = r.get_order("t1", "po9001")
    assert [x["line_number"] for x in order["lines"]] == [1]
    assert (
        order["lines"][0]["billed"] == 5.0
        and order["lines"][0]["qty_ordered"] == 12.0
    )


def test_dropping_a_received_line_is_refused_and_changes_nothing(repo):
    r, _ = repo
    r.upsert_orders("t1", [ORDER], uploaded_by="a@x")
    r.record_receipts("t1", [GRN], recorded_by="c@x")
    shrunk = {**ORDER, "total_amount": 5, "lines": [ORDER["lines"][1]]}
    with pytest.raises(
        ValueError,
        match="row 1: PO PO-9001 line 1 has goods received and cannot be removed",
    ):
        r.upsert_orders("t1", [shrunk], uploaded_by="a@x")
    order = r.get_order("t1", "PO-9001")
    assert order["total_amount"] == 1000.0 and len(order["lines"]) == 2


def test_dropping_an_unreceived_line_works_and_keeps_receipts_on_others(repo):
    r, _ = repo
    r.upsert_orders("t1", [ORDER], uploaded_by="a@x")
    r.record_receipts("t1", [GRN], recorded_by="c@x")
    r.upsert_orders(
        "t1", [{**ORDER, "lines": [ORDER["lines"][0]]}], uploaded_by="a@x"
    )
    assert r.book("t1", "PO-9001")["received"] == {1: 6.0}


def test_duplicate_po_in_one_upload_is_refused(repo):
    r, _ = repo
    with pytest.raises(ValueError, match="row 2"):
        r.upsert_orders(
            "t1", [ORDER, {**ORDER, "po_number": "po 9001"}], uploaded_by="a@x"
        )
    assert r.has_orders("t1") is False


def test_receipts_are_idempotent_and_sum_with_corrections(repo):
    r, _ = repo
    r.upsert_orders("t1", [ORDER], uploaded_by="a@x")
    assert r.record_receipts("t1", [GRN], recorded_by="c@x")["recorded"] == [
        "GRN-1"
    ]
    assert r.record_receipts("t1", [GRN], recorded_by="c@x")[
        "skipped_duplicates"
    ] == ["GRN-1"]
    fix = {
        **GRN,
        "grn_number": "GRN-1-FIX",
        "reason": "miscount",
        "lines": [{"item_code": "b6205", "qty_received": -1}],
    }
    r.record_receipts("t1", [fix], recorded_by="c@x")
    assert r.book("t1", "PO-9001")["received"] == {1: 5.0, 2: 0.0}
    assert [g["grn_number"] for g in r.list_receipts("t1", "PO-9001")] == [
        "GRN-1",
        "GRN-1-FIX",
    ]


def test_negative_receipt_needs_a_reason(repo):
    r, _ = repo
    r.upsert_orders("t1", [ORDER], uploaded_by="a@x")
    neg = {**GRN, "lines": [{"line_number": 1, "qty_received": -1}]}
    with pytest.raises(ValueError, match="reason"):
        r.record_receipts("t1", [neg], recorded_by="c@x")


def test_receipt_for_unknown_po_or_line_is_refused(repo):
    r, _ = repo
    r.upsert_orders("t1", [ORDER], uploaded_by="a@x")
    with pytest.raises(ValueError, match="PO-NOPE"):
        r.record_receipts(
            "t1",
            [{**GRN, "grn_number": "G", "po_number": "PO-NOPE"}],
            recorded_by="c@x",
        )
    with pytest.raises(ValueError, match="line 9"):
        r.record_receipts(
            "t1",
            [
                {
                    **GRN,
                    "grn_number": "G",
                    "lines": [{"line_number": 9, "qty_received": 1}],
                }
            ],
            recorded_by="c@x",
        )


def test_receipt_line_must_belong_to_the_receipts_po_and_batch_is_atomic(repo):
    r, _ = repo
    other = {**ORDER, "po_number": "PO-9002"}
    r.upsert_orders("t1", [ORDER, other], uploaded_by="a@x")
    ok = {**GRN, "grn_number": "G-OK"}
    bad = {
        **GRN,
        "grn_number": "G-BAD",
        "po_number": "PO-9002",
        "lines": [{"line_number": 7, "qty_received": 1}],
    }
    with pytest.raises(ValueError, match="row 2"):
        r.record_receipts("t1", [ok, bad], recorded_by="c@x")
    assert r.list_receipts("t1") == []


def test_other_tenant_sees_nothing(repo):
    r, _ = repo
    r.upsert_orders("t1", [ORDER], uploaded_by="a@x")
    r.record_receipts("t1", [GRN], recorded_by="c@x")
    assert (
        r.list_orders("t2") == []
        and r.book("t2", "PO-9001") is None
        and r.has_orders("t2") is False
    )
    assert r.get_order("t2", "PO-9001") is None and r.list_receipts("t2") == []
    assert r.has_orders("t1") is True
    row = r.list_orders("t1")[0]
    assert (row["line_count"], row["received_total"]) == (2, 6.0)


def test_billed_counts_only_invoices_approved_after_upload(repo):
    r, url = repo
    r.upsert_orders(
        "t1",
        [ORDER],
        uploaded_by="a@x",
        now=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    lines23 = [{"line_number": 1, "po_line_number": 1, "quantity": 3}]
    with tenant_session("t1", url) as s:
        for case, decision, when in (
            ("c-old", "approve", "2026-09-30T00:00:00+00:00"),
            ("c-new", "approve", "2026-10-02T00:00:00+00:00"),
            ("c-rej", "reject", "2026-10-03T00:00:00+00:00"),
        ):
            s.add(
                models.Invoice(
                    tenant_id="t1",
                    case_id=case,
                    vendor_name="Acme",
                    invoice_number=case,
                    invoice_date="2026-10-01",
                    currency="INR",
                    total_ex_tax=1,
                    tax_amount=0,
                    total_inc_tax=1,
                    po_number="po 9001",
                    vendor_tax_id="x",
                    due_date="2026-11-01",
                    min_confidence=1,
                    human_decision=decision,
                    fields={
                        "extraction": {"invoice": {"po_number": "po 9001"}},
                        "workflow": {"decision": decision, "decided_at": when},
                        "phase2": {
                            "validations": [{"step": "2.3", "lines": lines23}]
                        },
                    },
                )
            )
    assert r.book("t1", "PO-9001")["billed"] == {1: 5.0, 2: 0.0}
    book = r.book("t1", "PO-9001")
    assert book["po"]["vendor_name"] == "Acme Pty Ltd" and [
        x["line_number"] for x in book["lines"]
    ] == [1, 2]


def test_replace_deletes_absent_orders_but_never_received_ones(repo):
    r, _ = repo
    other = {**ORDER, "po_number": "PO-2", "lines": []}
    r.upsert_orders("t1", [ORDER, other], uploaded_by="a@x")
    r.upsert_orders("t2", [ORDER], uploaded_by="b@x")
    out = r.upsert_orders("t1", [ORDER], uploaded_by="a@x", replace=True)
    assert out["deleted"] == 1
    assert [o["po_number"] for o in r.list_orders("t1")] == ["PO9001"]
    assert r.has_orders("t2")  # another tenant's orders are not "absent"
    r.record_receipts("t1", [GRN], recorded_by="a@x")
    with pytest.raises(ValueError, match="goods received"):
        r.upsert_orders("t1", [other], uploaded_by="a@x", replace=True)
    # Refused whole: PO-2 was not created, PO-9001 is still there.
    assert [o["po_number"] for o in r.list_orders("t1")] == ["PO9001"]


def test_replace_keeps_scan_confirmed_orders_it_does_not_name(repo):
    r, _ = repo
    scanned = {**ORDER, "po_number": "PO-SCAN"}
    r.upsert_orders("t1", [scanned], uploaded_by="c@x", source="document")
    out = r.upsert_orders("t1", [ORDER], uploaded_by="a@x", replace=True)
    assert out["deleted"] == 0
    assert sorted(o["po_number"] for o in r.list_orders("t1")) == [
        "PO9001",
        "POSCAN",
    ]


def approved(url, case, when, *, po="PO-9001", quantity=3):
    """An invoice approved on the record only: no projected columns."""
    with tenant_session("t1", url) as s:
        s.add(
            models.Invoice(
                tenant_id="t1",
                case_id=case,
                fields={
                    "extraction": {"invoice": {"po_number": po}},
                    "workflow": {"decision": "approve", "decided_at": when},
                    "phase2": {
                        "validations": [
                            {
                                "step": "2.3",
                                "lines": [
                                    {
                                        "line_number": 1,
                                        "po_line_number": 1,
                                        "quantity": quantity,
                                    }
                                ],
                            }
                        ]
                    },
                },
            )
        )


def test_billed_reads_the_record_not_the_projected_columns(repo):
    r, url = repo
    r.upsert_orders(
        "t1",
        [ORDER],
        uploaded_by="a@x",
        now=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    approved(url, "c-unprojected", "2026-10-02T00:00:00+00:00")
    approved(url, "c-other-po", "2026-10-02T00:00:00+00:00", po="PO-2")
    assert r.book("t1", "PO-9001")["billed"][1] == 5.0  # 2 opening + 3


def test_a_reupload_never_moves_the_billed_counting_point(repo):
    r, url = repo
    r.upsert_orders(
        "t1",
        [ORDER],
        uploaded_by="a@x",
        now=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    approved(url, "c-before", "2026-10-02T00:00:00+00:00")
    for replace in (False, True):
        r.upsert_orders(
            "t1",
            [ORDER],
            uploaded_by="b@x",
            now=datetime(2026, 10, 5, tzinfo=timezone.utc),
            replace=replace,
        )
        # Opening still comes from the upload; the approval still counts.
        assert r.book("t1", "PO-9001")["billed"][1] == 5.0
    assert r.get_order("t1", "PO-9001")["uploaded_at"].startswith("2026-10-01")


def test_an_item_code_on_two_po_lines_needs_a_line_number(repo):
    r, _ = repo
    twice = {
        **ORDER,
        "lines": [
            {**ORDER["lines"][0], "line_number": 1},
            {**ORDER["lines"][0], "line_number": 2, "item_code": "b6205"},
        ],
    }
    r.upsert_orders("t1", [twice], uploaded_by="a@x")
    by_code = {**GRN, "lines": [{"item_code": "B-6205", "qty_received": 1}]}
    with pytest.raises(
        ValueError,
        match="row 1: item code B-6205 matches several lines of PO PO-9001; "
        "give line_number",
    ):
        r.record_receipts("t1", [by_code], recorded_by="c@x")
    assert r.list_receipts("t1") == []
