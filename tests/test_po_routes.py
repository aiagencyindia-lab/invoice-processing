"""Purchase orders and goods receipts arrive as a whole file or not at all,
every bad row is named, receipts are immutable, and the existing list/replace
routes read and write the database once there is one."""

import json

import pytest
from fastapi.testclient import TestClient

from invoice_processing.adapters import JsonlAuditSink
from invoice_processing.api import auth, po_routes, service
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.repositories import (
    PgCaseRepository,
    PgPurchaseOrderRepository,
)
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.worker.queue import LocalQueue

TENANT = "tenant-acme"

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


class FakeVerifier:
    def verify(self, token):
        if token not in ("viewer", "clerk", "approver", "admin"):
            raise auth.AuthError("Invalid token")
        return {
            "sub": f"u-{token}",
            "email": f"{token}@x.test",
            "email_verified": True,
            "tenant_id": TENANT,
            "role": token,
        }


def _auth(tmp_path, monkeypatch):
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(service, "_audit", JsonlAuditSink(audit))
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    return audit


def _client(token):
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("po_routes.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(TENANT, url) as session:
        session.add(
            models.Tenant(id=TENANT, name="Acme", slug="acme", status="active")
        )
    repo = PgPurchaseOrderRepository(url)
    monkeypatch.setattr(service, "_purchase_orders", repo)
    monkeypatch.setenv("DATABASE_URL", url)
    audit = _auth(tmp_path, monkeypatch)

    def events():
        return [json.loads(x) for x in audit.read_text().splitlines()]

    return {"client": _client, "repo": repo, "events": events}


def po_import(env, body, token="clerk"):
    return env["client"](token).post("/api/purchase-orders/import", json=body)


def grn_import(env, body, token="clerk"):
    return env["client"](token).post("/api/goods-receipts/import", json=body)


def test_clerk_imports_json_and_lists_and_reads_detail(env):
    r = po_import(env, {"purchase_orders": [ORDER]})
    assert r.status_code == 201
    assert r.json() == {"created": 1, "updated": 0, "lines": 2}
    listed = env["client"]("clerk").get("/api/purchase-orders").json()
    assert listed["source"] == "database"
    row = listed["purchase_orders"][0]
    assert row["po_number"] == "PO-9001" and row["line_count"] == 2
    assert row["remaining"] == 1000.0 and row["open"] is True
    detail = env["client"]("clerk").get("/api/purchase-orders/PO-9001").json()
    assert detail["lines"][0]["remaining"] == 8.0
    assert (
        env["client"]("viewer").get("/api/purchase-orders").status_code == 403
    )
    assert (
        env["client"]("viewer").get("/api/purchase-orders/PO-9001").status_code
        == 403
    )
    assert (
        po_import(env, {"purchase_orders": [ORDER]}, "viewer").status_code
        == 403
    )
    imported = [
        e
        for e in env["events"]()
        if e["event_type"] == "purchase_orders_imported"
    ]
    assert imported[0]["payload"] == {"created": 1, "updated": 0, "lines": 2}
    assert imported[0]["actor"] == "clerk@x.test"


def test_unknown_po_is_404(env):
    assert (
        env["client"]("clerk").get("/api/purchase-orders/PO-NOPE").status_code
        == 404
    )


def test_csv_import_one_row_per_line(env):
    csv_text = (
        "po_number,vendor_name,total_amount,currency,line_number,description,"
        "qty_ordered,unit_price,receipt_required\n"
        "PO-1,Acme,100,INR,1,Bolt,10,5,yes\n"
        "PO-1,Acme,100,INR,2,Freight,1,50,no\n"
    )
    r = env["client"]("clerk").post(
        "/api/purchase-orders/import",
        files={"file": ("po.csv", csv_text, "text/csv")},
    )
    assert r.status_code == 201 and r.json()["lines"] == 2
    lines = env["repo"].get_order(TENANT, "PO-1")["lines"]
    assert [ln["receipt_required"] for ln in lines] == [True, False]


def test_csv_errors_point_at_spreadsheet_rows(env):
    csv_text = (
        "po_number,vendor_name,total_amount,line_number,qty_ordered,unit_price\n"
        "PO-1,Acme,100,1,10,5\n"
        "PO-1,Other,100,2,-1,5\n"
    )
    r = env["client"]("clerk").post(
        "/api/purchase-orders/import",
        files={"file": ("po.csv", csv_text, "text/csv")},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "row 3: vendor_name differs from row 2" in detail
    assert "row 3: qty_ordered must be a number ≥ 0" in detail


def test_every_bad_row_is_named_and_nothing_is_stored(env):
    bad_line = {"line_number": 1, "description": "x", "qty_ordered": -1}
    bad = {
        "purchase_orders": [
            {**ORDER, "lines": [{**bad_line, "unit_price": "abc"}]},
            {**ORDER, "po_number": "PO-2", "vendor_name": " ", "currency": "R"},
            {**ORDER, "po_number": "PO-3", "valid_to": "31/12/2026"},
        ]
    }
    r = po_import(env, bad)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "row 1: qty_ordered must be a number ≥ 0" in detail
    assert "row 1: unit_price" in detail
    assert "row 2: vendor_name must not be blank" in detail
    assert "row 2: currency must be a 3-letter code" in detail
    assert "row 3: valid_to must be a date as YYYY-MM-DD" in detail
    assert (
        env["client"]("clerk").get("/api/purchase-orders").json()["count"] == 0
    )


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"vendor_name": "x" * 513}, "vendor_name must be at most 512"),
        ({"po_number": "--"}, "po_number must contain letters or digits"),
        ({"total_amount": "NaN"}, "total_amount must be a number"),
        (
            {"lines": [ORDER["lines"][0], ORDER["lines"][0]]},
            "line_number 1 appears twice",
        ),
        (
            {"lines": [{**ORDER["lines"][0], "line_number": 0}]},
            "line_number must be a whole number above 0",
        ),
        (
            {"lines": [{**ORDER["lines"][0], "description": "d" * 8001}]},
            "description must be at most 8000",
        ),
    ],
)
def test_a_bad_field_is_refused(env, change, said):
    r = po_import(env, {"purchase_orders": [{**ORDER, **change}]})
    assert r.status_code == 400 and said in r.json()["detail"]


def test_a_po_twice_in_one_upload_is_refused(env):
    r = po_import(
        env, {"purchase_orders": [ORDER, {**ORDER, "po_number": "po 9001"}]}
    )
    assert r.status_code == 400
    assert "row 2: PO po 9001 appears twice" in r.json()["detail"]


def test_import_over_5mb_is_refused(env):
    big = "po_number,vendor_name,total_amount\n" + "PO-1,Acme,1\n" * 500_000
    assert len(big) > po_routes.MAX_IMPORT_BYTES
    r = env["client"]("clerk").post(
        "/api/purchase-orders/import",
        files={"file": ("po.csv", big, "text/csv")},
    )
    assert r.status_code == 413 and r.json()["detail"] == "Import is over 5 MB."
    body = json.dumps(
        {"purchase_orders": [{"x": "y" * po_routes.MAX_IMPORT_BYTES}]}
    )
    assert (
        env["client"]("clerk")
        .post(
            "/api/purchase-orders/import",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        .status_code
        == 413
    )


def test_unreadable_uploads_are_refused(env):
    c = env["client"]("clerk")
    r = c.post(
        "/api/purchase-orders/import",
        files={"file": ("po.xlsx", b"PK", "application/octet-stream")},
    )
    assert r.status_code == 400 and ".csv or .json" in r.json()["detail"]
    r = c.post(
        "/api/purchase-orders/import",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert po_import(env, {"purchase_orders": []}).status_code == 400


def test_grn_import_refuses_unknown_po_and_is_idempotent(env):
    po_import(env, {"purchase_orders": [ORDER]})
    first = grn_import(env, {"goods_receipts": [GRN]})
    assert first.status_code == 200
    assert first.json() == {
        "recorded": 1,
        "skipped_duplicates": 0,
        "rematch_queued": 0,
    }
    again = grn_import(env, {"goods_receipts": [GRN]}).json()
    assert again["recorded"] == 0 and again["skipped_duplicates"] == 1
    nope = {
        "goods_receipts": [{**GRN, "grn_number": "G2", "po_number": "PO-NOPE"}]
    }
    r = grn_import(env, nope)
    assert r.status_code == 400 and "not on file" in r.json()["detail"]
    listed = env["client"]("clerk").get("/api/goods-receipts?po_number=PO-9001")
    assert [g["grn_number"] for g in listed.json()["goods_receipts"]] == [
        "GRN-1"
    ]
    recorded = [
        e["payload"]
        for e in env["events"]()
        if e["event_type"] == "goods_receipt_recorded"
    ]
    assert recorded == [{"grn_number": "GRN-1", "po_number": "PO-9001"}]


def test_grn_problems_are_all_named(env):
    po_import(env, {"purchase_orders": [ORDER]})
    bad = {
        "goods_receipts": [
            {**GRN, "received_on": "yesterday", "lines": [{"qty_received": 1}]},
            {
                **GRN,
                "grn_number": "G2",
                "lines": [{"line_number": 1, "qty_received": -1}],
            },
            {**GRN, "grn_number": "G3", "lines": []},
        ]
    }
    r = grn_import(env, bad)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "row 1: received_on must be a date" in detail
    assert "row 1: needs a line_number or an item_code" in detail
    assert "row 2: a negative qty_received needs a reason" in detail
    assert "row 3: a goods receipt needs lines" in detail
    assert env["repo"].list_receipts(TENANT) == []


def test_grn_csv_and_a_repository_refusal_point_at_the_file_row(env):
    po_import(env, {"purchase_orders": [ORDER]})
    csv_text = (
        "grn_number,po_number,received_on,reason,line_number,item_code,qty_received\n"
        "GRN-1,PO-9001,2026-10-01,,,B-6205,4\n"
        "GRN-1,PO-9001,2026-10-01,,2,,1\n"
        "GRN-2,PO-9001,2026-10-02,,9,,1\n"
    )
    r = env["client"]("clerk").post(
        "/api/goods-receipts/import",
        files={"file": ("grn.csv", csv_text, "text/csv")},
    )
    assert r.status_code == 400
    assert r.json()["detail"].startswith("row 4: PO PO-9001 has no line 9")
    assert env["repo"].list_receipts(TENANT) == []
    fixed = csv_text.replace(",9,,1", ",1,,1")
    r = env["client"]("clerk").post(
        "/api/goods-receipts/import",
        files={"file": ("grn.csv", fixed, "text/csv")},
    )
    assert r.json()["recorded"] == 2
    received = env["repo"].get_order(TENANT, "PO-9001")["lines"]
    assert [ln["received"] for ln in received] == [5.0, 1.0]


def test_503_without_database(tmp_path, monkeypatch):
    _auth(tmp_path, monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(service, "_database_configured", lambda: False)
    c = _client("clerk")
    for r in (
        c.post(
            "/api/purchase-orders/import", json={"purchase_orders": [ORDER]}
        ),
        c.get("/api/purchase-orders/PO-9001"),
        c.post("/api/goods-receipts/import", json={"goods_receipts": [GRN]}),
        c.get("/api/goods-receipts"),
    ):
        assert r.status_code == 503 and "needs a database" in r.json()["detail"]


def test_503_when_the_database_has_no_tables(tmp_path, monkeypatch):
    """DATABASE_URL set (conftest's sqlite://) but unmigrated."""
    _auth(tmp_path, monkeypatch)
    r = _client("clerk").get("/api/goods-receipts")
    assert r.status_code == 503 and "needs a database" in r.json()["detail"]


def test_put_replace_all_writes_the_database(env):
    po_import(env, {"purchase_orders": [ORDER]})
    r = env["client"]("admin").put(
        "/api/purchase-orders",
        json={
            "purchase_orders": [
                {"po_number": "PO-2", "vendor_name": "B", "total_amount": 5}
            ]
        },
    )
    assert r.status_code == 200 and r.json()["count"] == 1
    listed = env["client"]("clerk").get("/api/purchase-orders").json()
    assert [p["po_number"] for p in listed["purchase_orders"]] == ["PO-2"]
    assert "purchase_orders_replaced" in [
        e["event_type"] for e in env["events"]()
    ]
    assert (
        env["client"]("clerk")
        .put("/api/purchase-orders", json={"purchase_orders": []})
        .status_code
        == 403
    )


def test_put_never_deletes_a_po_with_goods_received(env):
    po_import(env, {"purchase_orders": [ORDER]})
    grn_import(env, {"goods_receipts": [GRN]})
    r = env["client"]("admin").put(
        "/api/purchase-orders",
        json={
            "purchase_orders": [
                {"po_number": "PO-2", "vendor_name": "B", "total_amount": 5}
            ]
        },
    )
    assert r.status_code == 400 and "goods received" in r.json()["detail"]
    assert [
        o["po_number_display"] for o in env["repo"].list_orders(TENANT)
    ] == ["PO-9001"]


def test_put_validation_names_every_row(env):
    r = env["client"]("admin").put(
        "/api/purchase-orders",
        json={
            "purchase_orders": [
                {"po_number": "", "vendor_name": "B", "total_amount": 5},
                {"po_number": "PO-3", "vendor_name": "", "total_amount": 5},
            ]
        },
    )
    assert r.status_code == 400
    assert "row 1: po_number" in r.json()["detail"]
    assert "row 2: vendor_name" in r.json()["detail"]


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"total_amount": 1e20}, "row 1: total_amount must be at most"),
        (
            {"lines": [{**ORDER["lines"][0], "qty_ordered": 1e20}]},
            "row 1: qty_ordered must be at most",
        ),
        (
            {"lines": [{**ORDER["lines"][0], "line_number": 3_000_000_000}]},
            "row 1: line_number must be a whole number from 1 to 2147483647",
        ),
    ],
)
def test_numbers_beyond_the_columns_are_refused_by_row(env, change, said):
    r = po_import(env, {"purchase_orders": [{**ORDER, **change}]})
    assert r.status_code == 400 and said in r.json()["detail"]


def test_a_grn_quantity_beyond_the_column_is_refused_by_row(env):
    po_import(env, {"purchase_orders": [ORDER]})
    for qty in (1e20, -1e20):
        line = {"line_number": 1, "qty_received": qty}
        r = grn_import(
            env, {"goods_receipts": [{**GRN, "reason": "x", "lines": [line]}]}
        )
        assert r.status_code == 400
        assert "row 1: qty_received must be" in r.json()["detail"]


@pytest.mark.parametrize("junk", [5, [{}], "x", None])
def test_internal_keys_in_a_json_body_are_ignored(env, junk):
    """`_row`/`_conflicts` only ever come from the CSV parser."""
    order = {
        **ORDER,
        "_row": junk,
        "_conflicts": junk,
        "lines": [{**ln, "_row": junk} for ln in ORDER["lines"]],
    }
    r = po_import(env, {"purchase_orders": [order]})
    assert r.status_code == 201, r.text
    bad = {**order, "po_number": "PO-2", "vendor_name": ""}
    r = po_import(env, {"purchase_orders": [bad]})
    assert r.status_code == 400 and "row 1: vendor_name" in r.json()["detail"]
    grn = {**GRN, "_row": junk, "_conflicts": junk}
    assert grn_import(env, {"goods_receipts": [grn]}).status_code == 200
    r = env["client"]("admin").put(
        "/api/purchase-orders", json={"purchase_orders": [order]}
    )
    assert r.status_code == 200, r.text


def test_grn_import_queues_a_rematch_per_held_case(env, tmp_path, monkeypatch):
    cases = PgCaseRepository(env["repo"].url)
    queue = LocalQueue(tmp_path / "q")
    monkeypatch.setattr(service, "_cases", cases)
    monkeypatch.setattr(service, "get_queue", lambda: queue)

    def seed(case_id, po_number, passed, workflow=None):
        ctx = RunContext(tenant_id=TENANT, case_id=case_id)
        cases.put_artifact(
            ctx,
            "extraction",
            {"invoice": {"po_number": po_number, "_routing": "REVIEW"}},
        )
        cases.put_artifact(
            ctx, "phase2", {"validations": [{"step": "2.3", "passed": passed}]}
        )
        if workflow:
            cases.put_artifact(ctx, "workflow", workflow)
        cases.project_case(ctx, cases.get_artifacts(ctx))

    seed("held", "po 9001", False)
    seed("passing", "PO-9001", True)
    seed("decided", "PO-9001", False, {"decision": "rejected"})
    seed("other-po", "PO-9002", False)

    po_import(env, {"purchase_orders": [ORDER]})
    r = grn_import(env, {"goods_receipts": [GRN]})
    assert r.status_code == 200 and r.json()["rematch_queued"] == 1
    assert queue.depth() == 1
    [message] = queue.pull()
    assert message.payload == {
        "type": "case.rematch",
        "tenant_id": TENANT,
        "case_id": "held",
        "po_number": "PO9001",  # as the receipt store reports it, normalised
    }
