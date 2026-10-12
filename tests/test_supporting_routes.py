"""A scanned PO or receipt feeds the match only once a person confirms it, and
confirming it goes through the same checks an ERP import does."""

import copy
import json

import pytest
from fastapi.testclient import TestClient

from invoice_processing.adapters import JsonlAuditSink
from invoice_processing.api import auth, po_routes, service
from invoice_processing.api.service import app
from invoice_processing.db import models
from invoice_processing.db.repositories import (
    PgCaseRepository,
    PgDocumentRepository,
    PgPurchaseOrderRepository,
    PgSupportingDocumentRepository,
    entity_type_for,
)
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.worker.queue import LocalQueue

TENANT = "tenant-acme"

ORDER = {
    "po_number": "PO-9001",
    "vendor_name": "Acme Pty Ltd",
    "total_amount": 1000,
    "lines": [
        {
            "line_number": 1,
            "item_code": "B-6205",
            "description": "Ball bearing 6205",
            "qty_ordered": 10,
            "unit_price": 100,
        }
    ],
}
GRN_READ = {
    "kind": "goods_receipt",
    "fields": {
        "grn_number": "GRN-77",
        "received_on": "2026-10-01",
        "po_number": "PO-9001",
        "vendor_name": "Acme Pty Ltd",
        "received_by": "Ravi",
    },
    "lines": [
        {
            "line_number": 1,
            "item_code": "B-6205",
            "description": "Ball bearing 6205",
            "quantity_received": 6.0,
            "unit": "nos",
            "remarks": None,
        }
    ],
    "_evidence": {"_summary": {"min_confidence": 0.9}},
    "_repairs": [],
}
PO_READ = {
    "kind": "purchase_order",
    "fields": {
        "po_number": "PO-4471",
        "po_date": "2026-09-01",
        "valid_from": None,
        "valid_to": None,
        "vendor_name": "Globe Supplies",
        "vendor_tax_id": None,
        "customer_name": "Acme",
        "currency": "INR",
        "total_amount": 500.0,
        "amount_basis": None,
    },
    "lines": [
        {
            "line_number": 1,
            "item_code": "C-1",
            "description": "Cable",
            "quantity": 5.0,
            "unit": "m",
            "unit_price": 100.0,
            "hsn_sac": None,
        }
    ],
    "_evidence": {},
    "_repairs": [],
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


def _client(token="clerk"):
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def env(tmp_path, monkeypatch, scratch_db):
    url = scratch_db("supporting_routes.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(TENANT, url) as session:
        session.add(
            models.Tenant(id=TENANT, name="Acme", slug="acme", status="active")
        )
    orders = PgPurchaseOrderRepository(url)
    supporting = PgSupportingDocumentRepository(url)
    documents = PgDocumentRepository(url)
    monkeypatch.setattr(service, "_purchase_orders", orders)
    monkeypatch.setattr(service, "_supporting_documents", supporting)
    monkeypatch.setattr(service, "_documents", documents)
    monkeypatch.setattr(service, "_cases", PgCaseRepository(url))
    monkeypatch.setattr(
        service, "get_queue", lambda: LocalQueue(tmp_path / "queue")
    )
    monkeypatch.setenv("DATABASE_URL", url)
    audit = _auth(tmp_path, monkeypatch)
    rematched = []
    monkeypatch.setattr(
        po_routes,
        "queue_rematches",
        lambda principal, pos: rematched.append(pos) or len(pos),
    )

    def events(name=None):
        rows = [json.loads(x) for x in audit.read_text().splitlines()]
        return [e for e in rows if name is None or e["event_type"] == name]

    def scan(read, blocked_reason=None, n=[0]):  # noqa: B006
        n[0] += 1
        doc = documents.create(
            TENANT, storage_key=f"k{n[0]}", sha256=f"s{n[0]}",
            filename=f"scan{n[0]}.pdf", byte_size=1,
        )  # fmt: skip
        documents.set_kind(TENANT, doc, read["kind"])
        return supporting.upsert_from_extraction(
            TENANT, document_id=doc, kind=read["kind"],
            extracted=copy.deepcopy(read),
            po_number=read["fields"]["po_number"],
            blocked_reason=blocked_reason, extraction_error=None,
        )  # fmt: skip

    return {
        "orders": orders,
        "supporting": supporting,
        "events": events,
        "scan": scan,
        "rematched": rematched,
    }


def _grn_body(**fields):
    return {
        "fields": {**GRN_READ["fields"], **fields},
        "lines": [{**GRN_READ["lines"][0], "po_line_number": 1}],
    }


def _accept(row, body, token="clerk"):
    return _client(token).post(
        f"/api/supporting-documents/{row['id']}/accept", json=body
    )


def test_list_and_detail_show_the_queue_with_the_pdf(env):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    grn = env["scan"](GRN_READ)
    env["scan"](PO_READ, blocked_reason=None)
    waiting = env["scan"](
        {**GRN_READ, "fields": {**GRN_READ["fields"], "po_number": "PO-1"}},
        blocked_reason="Purchase order PO-1 is not on file",
    )
    listed = _client().get("/api/supporting-documents").json()["documents"]
    assert len(listed) == 3
    first = listed[0]
    assert first["filename"] == "scan1.pdf"
    assert first["document_url"] == (
        f"/api/invoices/{first['document_id']}/document"
    )
    only = _client().get("/api/supporting-documents?status=blocked").json()
    assert [d["id"] for d in only["documents"]] == [waiting["id"]]
    assert (
        _client().get("/api/supporting-documents?status=nope").status_code
        == 400
    )

    detail = _client().get(f"/api/supporting-documents/{grn['id']}").json()
    assert detail["po_on_file"] is True and detail["paired"] == [1]
    assert detail["extracted"]["fields"]["grn_number"] == "GRN-77"
    assert detail["document_url"].endswith(f"{grn['document_id']}/document")
    off = _client().get(f"/api/supporting-documents/{waiting['id']}").json()
    assert off["po_on_file"] is False and off["paired"] is None
    assert _client().get("/api/supporting-documents/nope").status_code == 404


def test_viewer_cannot_see_or_decide(env):
    row = env["scan"](GRN_READ)
    v = _client("viewer")
    assert v.get("/api/supporting-documents").status_code == 403
    assert v.get(f"/api/supporting-documents/{row['id']}").status_code == 403
    assert _accept(row, _grn_body(), "viewer").status_code == 403
    assert (
        v.post(
            f"/api/supporting-documents/{row['id']}/reject",
            json={"reason": "x"},
        ).status_code
        == 403
    )


def test_accepting_a_receipt_records_it_from_the_scan(env):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    row = env["scan"](GRN_READ)
    r = _accept(row, _grn_body(received_on="2026-10-02"))
    assert r.status_code == 200, r.text
    assert r.json() == {"recorded": 1, "rematch_queued": 1}
    assert env["rematched"] == [["PO9001"]]

    [receipt] = env["orders"].list_receipts(TENANT)
    assert receipt["source"] == "document"
    assert receipt["document_id"] == row["document_id"]
    assert receipt["received_on"] == "2026-10-02"
    assert receipt["lines"][0]["qty_received"] == 6.0

    done = env["supporting"].get(TENANT, row["id"])
    assert (
        done["status"] == "accepted" and done["reviewed_by"] == "clerk@x.test"
    )
    assert done["extracted"]["fields"]["received_on"] == "2026-10-02"
    assert done["extracted"]["lines"][0]["po_line_number"] == 1
    assert done["extracted"]["_evidence"] == GRN_READ["_evidence"]

    [event] = env["events"]("supporting_document_accepted")
    assert event["payload"]["kind"] == "goods_receipt"
    assert event["payload"]["number"] == "GRN-77"
    assert event["payload"]["changed"] == {
        "received_on": {"before": "2026-10-01", "after": "2026-10-02"}
    }
    assert event["payload"]["lines_changed"] == 0

    again = _accept(row, _grn_body())
    assert again.status_code == 409
    assert again.json()["detail"] == "This document was already accepted."


def test_a_bad_quantity_is_named_and_nothing_is_stored(env):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    row = env["scan"](GRN_READ)
    body = _grn_body()
    body["lines"][0]["quantity_received"] = "six"
    r = _accept(row, body)
    assert r.status_code == 400
    assert "qty_received must be a number" in r.json()["detail"]
    assert env["orders"].list_receipts(TENANT) == []
    assert env["supporting"].get(TENANT, row["id"])["status"] == "pending"


def test_a_receipt_scanned_twice_is_already_recorded(env):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    assert _accept(env["scan"](GRN_READ), _grn_body()).status_code == 200
    second = env["scan"](GRN_READ)
    r = _accept(second, _grn_body())
    assert r.status_code == 400
    assert r.json()["detail"] == "Goods receipt GRN-77 is already recorded."
    assert env["supporting"].get(TENANT, second["id"])["status"] == "pending"


def test_a_blocked_receipt_waits_until_its_po_is_imported(env):
    row = env["scan"](
        GRN_READ, blocked_reason="Purchase order PO-9001 is not on file"
    )
    r = _accept(row, _grn_body())
    assert r.status_code == 400
    assert "'PO-9001' is not on file" in r.json()["detail"]
    assert env["supporting"].get(TENANT, row["id"])["status"] == "blocked"

    imported = _client().post(
        "/api/purchase-orders/import", json={"purchase_orders": [ORDER]}
    )
    assert imported.status_code == 201
    assert env["supporting"].get(TENANT, row["id"])["status"] == "pending"
    [event] = env["events"]("supporting_document_unblocked")
    assert event["payload"]["id"] == row["id"]
    assert _accept(row, _grn_body()).status_code == 200


def test_replacing_the_po_set_also_unblocks(env):
    row = env["scan"](
        GRN_READ, blocked_reason="Purchase order PO-9001 is not on file"
    )
    r = _client("admin").put(
        "/api/purchase-orders", json={"purchase_orders": [ORDER]}
    )
    assert r.status_code == 200
    assert env["supporting"].get(TENANT, row["id"])["status"] == "pending"


def test_a_failed_unblock_never_fails_the_import(env, monkeypatch):
    class Broken:
        def list(self, *a, **k):
            raise RuntimeError("down")

    monkeypatch.setattr(service, "_supporting_documents", Broken())
    imported = _client().post(
        "/api/purchase-orders/import", json={"purchase_orders": [ORDER]}
    )
    assert imported.status_code == 201
    [event] = env["events"]("supporting_document_unblock_failed")
    assert event["payload"]["error"] == "down"


def test_accepting_a_po_creates_it_and_an_erp_upload_replaces_it(env):
    row = env["scan"](PO_READ)
    body = {"fields": PO_READ["fields"], "lines": PO_READ["lines"]}
    r = _accept(row, body)
    assert r.status_code == 201, r.text
    assert r.json() == {"created": 1, "updated": 0, "lines": 1}
    order = env["orders"].get_order(TENANT, "PO-4471")
    assert order["source"] == "document"
    assert order["document_id"] == row["document_id"]
    assert order["lines"][0]["qty_ordered"] == 5.0

    erp = {**ORDER, "po_number": "PO-4471", "vendor_name": "Globe (ERP)"}
    _client().post(
        "/api/purchase-orders/import", json={"purchase_orders": [erp]}
    )
    order = env["orders"].get_order(TENANT, "PO-4471")
    assert order["source"] == "upload" and order["document_id"] is None

    rescan = env["scan"](PO_READ)
    r = _accept(rescan, body)
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "PO PO-4471 came from your ERP upload; a scan cannot change it."
    )
    assert env["orders"].get_order(TENANT, "PO-4471")["vendor_name"] == (
        "Globe (ERP)"
    )


def test_accepting_a_po_unblocks_its_receipts(env):
    waiting = env["scan"](
        {**GRN_READ, "fields": {**GRN_READ["fields"], "po_number": "PO-4471"}},
        blocked_reason="Purchase order PO-4471 is not on file",
    )
    row = env["scan"](PO_READ)
    body = {"fields": PO_READ["fields"], "lines": PO_READ["lines"]}
    assert _accept(row, body).status_code == 201
    assert env["supporting"].get(TENANT, waiting["id"])["status"] == "pending"


def test_a_po_with_no_lines_is_refused(env):
    row = env["scan"](PO_READ)
    r = _accept(row, {"fields": PO_READ["fields"], "lines": []})
    assert r.status_code == 400
    assert r.json()["detail"] == (
        "No lines could be read from this purchase order; add them or reject it."
    )
    assert env["orders"].get_order(TENANT, "PO-4471") is None


def test_reject_needs_a_reason_and_is_final(env):
    row = env["scan"](PO_READ)
    url = f"/api/supporting-documents/{row['id']}/reject"
    assert _client().post(url, json={"reason": "  "}).status_code == 422
    assert _client().post(url, json={}).status_code == 422
    r = _client().post(url, json={"reason": "Duplicate of PO-4470"})
    assert r.status_code == 200 and r.json() == {"status": "rejected"}
    done = env["supporting"].get(TENANT, row["id"])
    assert (
        done["status"] == "rejected" and done["note"] == "Duplicate of PO-4470"
    )
    [event] = env["events"]("supporting_document_rejected")
    assert event["payload"]["reason"] == "Duplicate of PO-4470"
    r = _accept(row, {"fields": PO_READ["fields"], "lines": PO_READ["lines"]})
    assert r.status_code == 409
    assert r.json()["detail"] == "This document was already rejected."
    assert _client().post(url, json={"reason": "again"}).status_code == 409


def test_unknown_document_is_404_on_every_route(env):
    c = _client()
    assert _accept({"id": "nope"}, _grn_body()).status_code == 404
    assert (
        c.post(
            "/api/supporting-documents/nope/reject", json={"reason": "x"}
        ).status_code
        == 404
    )


def test_503_without_database(tmp_path, monkeypatch):
    _auth(tmp_path, monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(service, "_database_configured", lambda: False)
    c = _client()
    for r in (
        c.get("/api/supporting-documents"),
        c.get("/api/supporting-documents/x"),
        c.post("/api/supporting-documents/x/accept", json=_grn_body()),
        c.post("/api/supporting-documents/x/reject", json={"reason": "r"}),
        _client("admin").post("/api/supporting-documents/x/reopen"),
        c.post("/api/supporting-documents/x/process-as-invoice"),
    ):
        assert r.status_code == 503 and "needs a database" in r.json()["detail"]


def test_documents_listing_shows_kind_and_review_stage(env):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    pending = env["scan"](GRN_READ)
    blocked = env["scan"](
        {**GRN_READ, "fields": {**GRN_READ["fields"], "po_number": "PO-1"}},
        blocked_reason="Purchase order PO-1 is not on file",
    )
    accepted = env["scan"](PO_READ)
    rejected = env["scan"](PO_READ)
    env["supporting"].mark(
        TENANT, accepted["id"], status="accepted", reviewed_by="c",
        expect=("pending",),
    )  # fmt: skip
    env["orders"].upsert_orders(
        TENANT, [{**ORDER, "po_number": "PO-4471"}], uploaded_by="c",
        source="document", document_id=accepted["document_id"],
    )  # fmt: skip
    env["supporting"].mark(
        TENANT, rejected["id"], status="rejected", reviewed_by="c", note="x",
        expect=("pending",),
    )  # fmt: skip
    rows = {
        d["document_id"]: d
        for d in _client().get("/api/documents").json()["documents"]
    }

    def seen(row):
        d = rows[row["document_id"]]
        return d["kind"], d["stage"], d["explanation"]

    assert seen(pending) == (
        "goods_receipt",
        "awaiting_confirmation",
        "Awaiting confirmation on Purchase orders.",
    )
    assert seen(blocked) == (
        "goods_receipt",
        "blocked",
        "Blocked: Purchase order PO-1 is not on file",
    )
    assert seen(accepted) == ("purchase_order", "done", "Confirmed.")
    assert seen(rejected) == ("purchase_order", "done", "Rejected.")


def test_review_events_name_the_supporting_document():
    for name in (
        "supporting_document_extracted",
        "supporting_document_accepted",
        "supporting_document_rejected",
        "supporting_document_unblocked",
        "supporting_document_unblock_failed",
        "supporting_document_reopened",
        "supporting_document_sent_to_invoices",
        "supporting_document_reclassified",
    ):
        assert entity_type_for(name) == "supporting_document", name


def test_a_scan_cannot_take_a_po_an_upload_just_stored(env):
    """The ERP upload lands after any pre-check would have passed: the
    refusal has to come from the write itself."""
    _client().post(
        "/api/purchase-orders/import",
        json={"purchase_orders": [{**ORDER, "po_number": "PO-4471"}]},
    )
    row = env["scan"](PO_READ)
    r = _accept(row, {"fields": PO_READ["fields"], "lines": PO_READ["lines"]})
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "PO PO-4471 came from your ERP upload; a scan cannot change it."
    )
    order = env["orders"].get_order(TENANT, "PO-4471")
    assert (
        order["source"] == "upload" and order["vendor_name"] == "Acme Pty Ltd"
    )
    assert env["supporting"].get(TENANT, row["id"])["status"] == "pending"


def test_a_blocked_document_can_be_rejected(env):
    row = env["scan"](
        GRN_READ, blocked_reason="Purchase order PO-X is not on file"
    )
    r = _client().post(
        f"/api/supporting-documents/{row['id']}/reject",
        json={"reason": "Not ours"},
    )
    assert r.status_code == 200
    assert env["supporting"].get(TENANT, row["id"])["status"] == "rejected"


def test_a_decision_racing_another_is_refused(env, monkeypatch):
    row = env["scan"](PO_READ)
    stale = env["supporting"].get(TENANT, row["id"])
    env["supporting"].mark(
        TENANT, row["id"], status="accepted", reviewed_by="other",
        expect=("pending",),
    )  # fmt: skip
    monkeypatch.setattr(env["supporting"], "get", lambda *a: dict(stale))
    r = _client().post(
        f"/api/supporting-documents/{row['id']}/reject", json={"reason": "x"}
    )
    assert r.status_code == 409
    assert r.json()["detail"] == RACED
    monkeypatch.undo()
    assert env["supporting"].get(TENANT, row["id"])["reviewed_by"] == "other"


def test_a_changed_po_line_counts_as_a_changed_line(env):
    order = {
        **ORDER,
        "lines": [
            *ORDER["lines"],
            {"line_number": 2, "qty_ordered": 1, "unit_price": 1},
        ],
    }
    env["orders"].upsert_orders(TENANT, [order], uploaded_by="u")
    row = env["scan"](GRN_READ)
    body = _grn_body()
    body["lines"][0]["po_line_number"] = 2  # pairing said 1
    assert _accept(row, body).status_code == 200
    [event] = env["events"]("supporting_document_accepted")
    assert event["payload"]["lines_changed"] == 1


def test_a_reject_racing_an_accept_loses(env, monkeypatch):
    """The accept claims the row before it stores, so a reject arriving
    while the receipt is being written is refused, never left contradicted
    by a stored receipt."""
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    row = env["scan"](GRN_READ)
    real = env["orders"].record_receipts
    raced = []

    def record_while_rejected(*a, **kw):
        raced.append(
            _client("approver").post(
                f"/api/supporting-documents/{row['id']}/reject",
                json={"reason": "late"},
            )
        )
        return real(*a, **kw)

    monkeypatch.setattr(env["orders"], "record_receipts", record_while_rejected)
    assert _accept(row, _grn_body()).status_code == 200
    assert raced[0].status_code == 409
    assert raced[0].json()["detail"] == RACED
    assert len(env["orders"].list_receipts(TENANT)) == 1
    assert env["supporting"].get(TENANT, row["id"])["status"] == "accepted"


def test_a_failed_store_puts_the_row_back_as_it_was(env):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    assert _accept(env["scan"](GRN_READ), _grn_body()).status_code == 200
    second = env["scan"](GRN_READ)
    r = _accept(second, _grn_body(received_on="2026-10-05"))
    assert r.status_code == 400
    back = env["supporting"].get(TENANT, second["id"])
    assert back["status"] == "pending"
    assert back["extracted"] == GRN_READ
    assert back["reviewed_by"] is None and back["reviewed_at"] is None
    assert env["events"]("supporting_document_accepted")[1:] == []


def test_an_unexpected_store_failure_also_puts_the_row_back(env, monkeypatch):
    row = env["scan"](PO_READ)

    def broken(*a, **kw):
        raise RuntimeError("db went away")

    monkeypatch.setattr(env["orders"], "upsert_orders", broken)
    # A 500 with the usual headers (the middleware catches what escapes a
    # route); what matters is that the row went back to pending.
    r = _accept(row, {"fields": PO_READ["fields"], "lines": PO_READ["lines"]})
    assert r.status_code == 500
    assert env["supporting"].get(TENANT, row["id"])["status"] == "pending"


def test_a_concurrent_insert_is_a_409_not_a_500(env, monkeypatch):
    from invoice_processing.ports import ConcurrentWrite  # noqa: PLC0415

    row = env["scan"](PO_READ)

    def collided(*a, **kw):
        raise ConcurrentWrite("purchase order")

    monkeypatch.setattr(env["orders"], "upsert_orders", collided)
    r = _accept(row, {"fields": PO_READ["fields"], "lines": PO_READ["lines"]})
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "Another import stored this purchase order at the same moment; "
        "reload and try again."
    )
    assert env["supporting"].get(TENANT, row["id"])["status"] == "pending"


def test_a_failed_pairing_read_does_not_fail_an_accept(env, monkeypatch):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    row = env["scan"](GRN_READ)
    calls = []
    real = env["orders"].get_order

    def flaky(*a, **kw):
        calls.append(1)
        raise RuntimeError("read failed")

    monkeypatch.setattr(env["orders"], "get_order", flaky)
    r = _accept(row, _grn_body())
    monkeypatch.setattr(env["orders"], "get_order", real)
    assert r.status_code == 200 and calls
    [event] = env["events"]("supporting_document_accepted")
    assert event["payload"]["lines_changed"] is None


ORDER_2 = {
    **ORDER,
    "po_number": "PO-9002",
    "lines": [
        {
            "line_number": 1,
            "item_code": "X-1",
            "qty_ordered": 1,
            "unit_price": 1,
        },
        {**ORDER["lines"][0], "line_number": 2},
    ],
}


def test_a_changed_po_number_needs_its_lines_chosen_again(env):
    """The pairing was read against the scanned PO; sent to another PO it
    would book the receipt to whatever line has the same number there."""
    env["orders"].upsert_orders(TENANT, [ORDER, ORDER_2], uploaded_by="u")
    row = env["scan"](GRN_READ)
    stale = _grn_body(po_number="PO-9002")  # still PO-9001's line 1
    r = _accept(row, stale)
    assert r.status_code == 400
    assert r.json()["detail"] == (
        "The PO number changed from PO-9001 to PO-9002; "
        "choose the PO lines again."
    )
    assert env["orders"].list_receipts(TENANT) == []
    assert env["supporting"].get(TENANT, row["id"])["status"] == "pending"

    chosen = _grn_body(po_number="PO-9002")
    chosen["lines"][0]["po_line_number"] = 2
    r = _accept(row, {**chosen, "lines_rechosen": True})
    assert r.status_code == 200, r.text
    [receipt] = env["orders"].list_receipts(TENANT)
    assert receipt["po_number"] == "PO-9002"
    assert receipt["lines"][0]["line_number"] == 2


def test_a_blocked_receipt_is_accepted_once_its_po_number_is_corrected(env):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    row = env["scan"](
        {**GRN_READ, "fields": {**GRN_READ["fields"], "po_number": "PO-1"}},
        blocked_reason="Purchase order PO-1 is not on file",
    )
    r = _accept(row, _grn_body(po_number="PO-1"))
    assert r.status_code == 400
    assert "'PO-1' is not on file" in r.json()["detail"]
    back = env["supporting"].get(TENANT, row["id"])
    assert back["status"] == "blocked"
    assert back["blocked_reason"] == "Purchase order PO-1 is not on file"
    assert env["orders"].list_receipts(TENANT) == []

    # The corrected number pairs the line the clerk sent, so no re-choice.
    r = _accept(row, _grn_body())
    assert r.status_code == 200, r.text
    [receipt] = env["orders"].list_receipts(TENANT)
    assert receipt["po_number"] == "PO-9001"
    done = env["supporting"].get(TENANT, row["id"])
    assert done["status"] == "accepted" and done["blocked_reason"] is None


def test_replacing_the_po_set_keeps_scan_confirmed_orders(env):
    row = env["scan"](PO_READ)
    body = {"fields": PO_READ["fields"], "lines": PO_READ["lines"]}
    assert _accept(row, body).status_code == 201
    r = _client("admin").put(
        "/api/purchase-orders", json={"purchase_orders": [ORDER]}
    )
    assert r.status_code == 200, r.text
    assert env["orders"].get_order(TENANT, "PO-4471")["source"] == "document"
    # Named in the export, the ERP's version replaces the scan's.
    erp = {**ORDER, "po_number": "PO-4471"}
    r = _client("admin").put(
        "/api/purchase-orders", json={"purchase_orders": [erp]}
    )
    assert r.status_code == 200, r.text
    assert env["orders"].get_order(TENANT, "PO-4471")["source"] == "upload"
    assert env["orders"].get_order(TENANT, "PO-9001") is None


# --- deferred minors (batch A) ---------------------------------------------
RACED = (
    "This document is being decided by someone else; reload to see the result."
)


def test_a_po_rescan_dropping_a_received_line_is_named_and_reverted(env):
    row = env["scan"](PO_READ)
    body = {"fields": PO_READ["fields"], "lines": PO_READ["lines"]}
    assert _accept(row, body).status_code == 201
    env["orders"].record_receipts(
        TENANT,
        [
            {
                "grn_number": "G-1",
                "po_number": "PO-4471",
                "received_on": "2026-10-01",
                "lines": [{"line_number": 1, "qty_received": 2}],
            }
        ],
        recorded_by="u",
    )
    rescan = env["scan"](PO_READ)
    moved = {**PO_READ["lines"][0], "line_number": 2}
    r = _accept(rescan, {"fields": PO_READ["fields"], "lines": [moved]})
    assert r.status_code == 400
    assert r.json()["detail"] == (
        "PO PO-4471 line 1 has goods received and cannot be removed"
    )
    back = env["supporting"].get(TENANT, rescan["id"])
    assert back["status"] == "pending" and back["reviewed_by"] is None


@pytest.mark.parametrize("where", ["field", "line"])
def test_true_or_false_is_never_a_number(env, where):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    row = env["scan"](GRN_READ)
    body = _grn_body()
    if where == "field":
        body["fields"]["grn_number"] = True
        named = "grn_number must be a number or text, not true or false."
    else:
        body["lines"][0]["quantity_received"] = True
        named = (
            "line 1: quantity_received must be a number or text, "
            "not true or false."
        )
    r = _accept(row, body)
    assert r.status_code == 400
    assert r.json()["detail"] == named
    assert env["orders"].list_receipts(TENANT) == []


def test_a_failed_audit_after_storing_still_answers_success(env, monkeypatch):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    row = env["scan"](GRN_READ)

    class Broken:
        def emit(self, *a, **k):
            raise RuntimeError("audit down")

    monkeypatch.setattr(po_routes, "_audit", Broken)
    r = _accept(row, _grn_body())
    assert r.status_code == 200, r.text
    assert len(env["orders"].list_receipts(TENANT)) == 1


def test_a_changed_po_counts_lines_against_the_new_po_s_pairing(env):
    env["orders"].upsert_orders(TENANT, [ORDER, ORDER_2], uploaded_by="u")
    row = env["scan"](GRN_READ)
    chosen = _grn_body(po_number="PO-9002")
    chosen["lines"][0]["po_line_number"] = 2  # what PO-9002's pairing says
    r = _accept(row, {**chosen, "lines_rechosen": True})
    assert r.status_code == 200, r.text
    [event] = env["events"]("supporting_document_accepted")
    assert event["payload"]["lines_changed"] == 0


def test_a_failed_store_restores_the_status_the_claim_replaced(
    env, monkeypatch
):
    """Read as blocked, unblocked before the claim: the undo puts it back to
    pending, what the claim actually replaced."""
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    assert _accept(env["scan"](GRN_READ), _grn_body()).status_code == 200
    second = env["scan"](GRN_READ)
    stale = {
        **env["supporting"].get(TENANT, second["id"]),
        "status": "blocked",
        "blocked_reason": "Purchase order PO-9001 is not on file",
    }
    real_get = env["supporting"].get
    monkeypatch.setattr(
        env["supporting"],
        "get",
        lambda t, i: dict(stale) if i == second["id"] else real_get(t, i),
    )
    r = _accept(second, _grn_body())
    assert r.status_code == 400  # GRN-77 is already recorded
    monkeypatch.undo()
    back = env["supporting"].get(TENANT, second["id"])
    assert back["status"] == "pending" and back["blocked_reason"] is None


def test_an_unreadable_new_po_is_a_503(env, monkeypatch):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    row = env["scan"](GRN_READ)

    def down(*a, **k):
        raise RuntimeError("read failed")

    monkeypatch.setattr(env["orders"], "get_order", down)
    r = _accept(row, _grn_body(po_number="PO-9002"))
    assert r.status_code == 503
    assert r.json()["detail"] == (
        "Purchase-order lines could not be read; try again."
    )
    assert env["supporting"].get(TENANT, row["id"])["status"] == "pending"


def test_a_cleared_po_number_says_so(env):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    row = env["scan"](GRN_READ)
    r = _accept(row, _grn_body(po_number=None))
    assert r.status_code == 400
    assert r.json()["detail"] == (
        "The PO number was cleared; enter the PO number and choose the PO "
        "lines again."
    )


def test_a_scanned_correction_carries_its_reason(env):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    assert _accept(env["scan"](GRN_READ), _grn_body()).status_code == 200
    fix = env["scan"](
        {**GRN_READ, "fields": {**GRN_READ["fields"], "grn_number": "GRN-78"}}
    )
    body = _grn_body(grn_number="GRN-78")
    body["lines"][0]["quantity_received"] = -2
    refused = _accept(fix, body)
    assert refused.status_code == 400
    assert "a negative qty_received needs a reason" in refused.json()["detail"]

    body["fields"]["reason"] = "2 returned damaged"
    r = _accept(fix, body)
    assert r.status_code == 200, r.text
    stored = {g["grn_number"]: g for g in env["orders"].list_receipts(TENANT)}
    assert stored["GRN-78"]["reason"] == "2 returned damaged"
    assert stored["GRN-78"]["lines"][0]["qty_received"] == -2


def _orphan(env, read=GRN_READ):
    """Accepted, as a crash between the claim and the store leaves it."""
    row = env["scan"](read)
    env["supporting"].mark(
        TENANT, row["id"], status="accepted", reviewed_by="c",
        expect=("pending",),
    )  # fmt: skip
    return row


@pytest.fixture
def settled(monkeypatch):
    from datetime import timedelta  # noqa: PLC0415

    from invoice_processing.api import supporting_routes  # noqa: PLC0415

    monkeypatch.setattr(supporting_routes, "STILL_STORING", timedelta(0))


def test_an_accepted_row_says_whether_anything_was_stored(env):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    done = env["scan"](GRN_READ)
    assert _accept(done, _grn_body()).status_code == 200
    orphan = _orphan(
        env,
        {**GRN_READ, "fields": {**GRN_READ["fields"], "grn_number": "G-2"}},
    )
    c = _client()
    assert c.get(f"/api/supporting-documents/{done['id']}").json()["stored"]
    assert not c.get(f"/api/supporting-documents/{orphan['id']}").json()[
        "stored"
    ]
    listed = {
        d["id"]: d["stored"]
        for d in c.get("/api/supporting-documents?status=accepted").json()[
            "documents"
        ]
    }
    assert listed == {done["id"]: True, orphan["id"]: False}
    rows = {
        d["document_id"]: d for d in c.get("/api/documents").json()["documents"]
    }
    assert rows[orphan["document_id"]]["stage"] == "accepted_unstored"
    assert rows[orphan["document_id"]]["explanation"] == (
        "Accepted but nothing was stored; an admin can reopen it."
    )
    assert rows[done["document_id"]]["stage"] == "done"


def test_an_admin_reopens_an_orphaned_acceptance(env, settled):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    orphan = _orphan(env)
    url = f"/api/supporting-documents/{orphan['id']}/reopen"
    assert _client("clerk").post(url).status_code == 403
    r = _client("admin").post(url)
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "pending"}
    back = env["supporting"].get(TENANT, orphan["id"])
    assert back["status"] == "pending" and back["reviewed_by"] is None
    [event] = env["events"]("supporting_document_reopened")
    assert event["payload"]["id"] == orphan["id"]
    again = _client("admin").post(url)
    assert again.status_code == 409
    assert again.json()["detail"] == (
        "Only an accepted document can be reopened; this one is pending."
    )
    assert _accept(back, _grn_body()).status_code == 200


def test_a_stored_acceptance_cannot_be_reopened(env, settled):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    row = env["scan"](GRN_READ)
    assert _accept(row, _grn_body()).status_code == 200
    r = _client("admin").post(f"/api/supporting-documents/{row['id']}/reopen")
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "Something was stored from this document; it cannot be reopened."
    )
    assert env["supporting"].get(TENANT, row["id"])["status"] == "accepted"


def test_a_fresh_acceptance_may_still_be_storing(env):
    orphan = _orphan(env)
    r = _client("admin").post(
        f"/api/supporting-documents/{orphan['id']}/reopen"
    )
    assert r.status_code == 409
    assert "may still be storing" in r.json()["detail"]
    assert env["supporting"].get(TENANT, orphan["id"])["status"] == "accepted"


def test_an_unreadable_store_refuses_the_reopen(env, settled, monkeypatch):
    orphan = _orphan(env)

    def down(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(env["orders"], "stored_documents", down)
    r = _client("admin").post(
        f"/api/supporting-documents/{orphan['id']}/reopen"
    )
    assert r.status_code == 503
    assert env["supporting"].get(TENANT, orphan["id"])["status"] == "accepted"


def test_a_misclassified_scan_is_sent_to_the_invoice_queue(
    env, tmp_path, monkeypatch
):
    from invoice_processing.db.repositories import PgClaimStore  # noqa: PLC0415

    url = env["orders"].url
    claims = PgClaimStore(url)
    monkeypatch.setattr(service, "_claim_store", claims)
    row = env["scan"](GRN_READ)
    claims.claim(TENANT, row["document_id"])
    claims.complete(TENANT, row["document_id"])
    go = f"/api/supporting-documents/{row['id']}/process-as-invoice"
    assert _client("viewer").post(go).status_code == 403

    r = _client().post(go)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected" and r.json()["queued"]
    done = env["supporting"].get(TENANT, row["id"])
    assert done["status"] == "rejected"
    assert done["note"] == "Sent to the invoice queue"
    assert done["reviewed_by"] == "clerk@x.test"
    assert service.get_documents().get(TENANT, row["document_id"])["kind"] == (
        "invoice"
    )
    assert claims.state(TENANT, row["document_id"]) is None
    [message] = LocalQueue(tmp_path / "queue").pull()
    assert message.payload["force_invoice"] is True
    assert message.payload["document_id"] == row["document_id"]
    [event] = env["events"]("supporting_document_sent_to_invoices")
    assert event["payload"]["id"] == row["id"]

    again = _client().post(go)
    assert again.status_code == 409
    assert again.json()["detail"] == "This document was already rejected."


# --- review fixes -----------------------------------------------------------
def test_a_reopen_never_reverts_a_fresh_reacceptance(env, settled, monkeypatch):
    orphan = _orphan(env)
    stale = env["supporting"].get(TENANT, orphan["id"])
    # Meanwhile: reopened by someone else and accepted again.
    env["supporting"].revert_to_pending(TENANT, orphan["id"], extracted={})
    env["supporting"].mark(
        TENANT, orphan["id"], status="accepted", reviewed_by="other",
        expect=("pending",),
    )  # fmt: skip
    monkeypatch.setattr(env["supporting"], "get", lambda *a: dict(stale))
    r = _client("admin").post(
        f"/api/supporting-documents/{orphan['id']}/reopen"
    )
    monkeypatch.undo()
    assert r.status_code == 409
    assert r.json()["detail"] == RACED
    now = env["supporting"].get(TENANT, orphan["id"])
    assert now["status"] == "accepted" and now["reviewed_by"] == "other"


def _sent(env, tmp_path, monkeypatch):
    from invoice_processing.db.repositories import PgClaimStore  # noqa: PLC0415

    monkeypatch.setattr(
        service, "_claim_store", PgClaimStore(env["orders"].url)
    )
    row = env["scan"](GRN_READ)
    return row, f"/api/supporting-documents/{row['id']}/process-as-invoice"


def test_a_failed_kind_change_still_retries_as_an_invoice(
    env, tmp_path, monkeypatch
):
    row, go = _sent(env, tmp_path, monkeypatch)
    documents = service.get_documents()

    def broken(*a, **k):
        raise RuntimeError("down")

    real = documents.set_kind
    monkeypatch.setattr(documents, "set_kind", broken)
    r = _client().post(go)
    assert r.status_code == 503
    monkeypatch.setattr(documents, "set_kind", real)
    assert documents.get(TENANT, row["document_id"])["kind"] == "goods_receipt"
    retried = _client("approver").post(
        f"/api/documents/{row['document_id']}/retry"
    )
    assert retried.status_code == 200, retried.text
    [message] = LocalQueue(tmp_path / "queue").pull()
    assert message.payload["force_invoice"] is True


def test_an_unreadable_review_row_does_not_retry_unforced(
    env, tmp_path, monkeypatch
):
    """Not knowing whether a clerk sent it to the invoice queue is not the
    same as knowing they did not: an unforced retry could divert it back."""
    row, _ = _sent(env, tmp_path, monkeypatch)

    def down(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(
        service.get_supporting_documents(), "for_documents", down
    )
    retried = _client("approver").post(
        f"/api/documents/{row['document_id']}/retry"
    )
    assert retried.status_code == 503
    assert retried.json()["detail"] == (
        "Could not check whether this document was sent to the invoice "
        "queue; try again."
    )
    assert LocalQueue(tmp_path / "queue").pull() == []


def test_a_stored_acceptance_is_not_sent_to_invoices(
    env, tmp_path, monkeypatch
):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    row, go = _sent(env, tmp_path, monkeypatch)
    assert _accept(row, _grn_body()).status_code == 200
    r = _client().post(go)
    assert r.status_code == 409
    assert r.json()["detail"] == "This document was already accepted."
    kind = service.get_documents().get(TENANT, row["document_id"])["kind"]
    assert kind == "goods_receipt"
    assert LocalQueue(tmp_path / "queue").pull() == []


def test_a_missing_file_record_is_never_queued(env, tmp_path, monkeypatch):
    row, go = _sent(env, tmp_path, monkeypatch)
    monkeypatch.setattr(service.get_documents(), "get", lambda *a: None)
    r = _client().post(go)
    assert r.status_code == 404
    assert r.json()["detail"] == (
        "This document's file record is missing; it cannot be sent to the "
        "invoice queue."
    )
    monkeypatch.undo()
    assert env["supporting"].get(TENANT, row["id"])["status"] == "pending"
    assert LocalQueue(tmp_path / "queue").pull() == []


def test_an_unreadable_store_reads_as_stored_on_list_and_detail(
    env, monkeypatch
):
    row = _orphan(env)

    def down(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(env["orders"], "stored_documents", down)
    c = _client()
    listed = c.get("/api/supporting-documents?status=accepted")
    assert listed.status_code == 200
    assert listed.json()["documents"][0]["stored"] is True
    detail = c.get(f"/api/supporting-documents/{row['id']}")
    assert detail.status_code == 200 and detail.json()["stored"] is True


def test_a_lost_compare_and_set_on_a_decidable_row_is_neutral(env, monkeypatch):
    from invoice_processing.ports import StatusChanged  # noqa: PLC0415

    row = env["scan"](PO_READ)

    def lost(*a, **k):
        raise StatusChanged("pending")

    monkeypatch.setattr(env["supporting"], "mark", lost)
    r = _client().post(
        f"/api/supporting-documents/{row['id']}/reject", json={"reason": "x"}
    )
    assert r.status_code == 409
    assert r.json()["detail"] == RACED


def test_stored_false_lists_only_orphaned_acceptances(env, settled):
    env["orders"].upsert_orders(TENANT, [ORDER], uploaded_by="u")
    done = env["scan"](GRN_READ)
    assert _accept(done, _grn_body()).status_code == 200
    orphan = _orphan(
        env,
        {**GRN_READ, "fields": {**GRN_READ["fields"], "grn_number": "G-2"}},
    )
    env["scan"](GRN_READ)  # pending: nothing stored, but not an acceptance
    r = _client().get(
        "/api/supporting-documents?status=pending,accepted&stored=false"
    )
    assert r.status_code == 200, r.text
    assert [d["id"] for d in r.json()["documents"]] == [orphan["id"]]


def test_an_unreadable_store_lists_no_orphans(env, monkeypatch):
    _orphan(env)

    def down(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(env["orders"], "stored_documents", down)
    r = _client().get("/api/supporting-documents?status=accepted&stored=false")
    assert r.status_code == 200 and r.json()["documents"] == []


def test_the_list_is_capped(env):
    for _ in range(3):
        env["scan"](GRN_READ)
    c = _client()
    assert (
        len(c.get("/api/supporting-documents?limit=2").json()["documents"]) == 2
    )
    assert len(c.get("/api/supporting-documents").json()["documents"]) == 3
    assert c.get("/api/supporting-documents?limit=501").status_code == 422
    assert c.get("/api/supporting-documents?limit=0").status_code == 422


def test_a_fresh_acceptance_is_not_listed_as_an_orphan(env):
    """It may still be storing, and its reopen would be refused."""
    _orphan(env)
    r = _client().get("/api/supporting-documents?status=accepted&stored=false")
    assert r.status_code == 200 and r.json()["documents"] == []


def test_a_capped_list_says_it_was_cut(env):
    for _ in range(3):
        env["scan"](GRN_READ)
    c = _client()
    cut = c.get("/api/supporting-documents?limit=2").json()
    assert len(cut["documents"]) == 2 and cut["truncated"] is True
    whole = c.get("/api/supporting-documents?limit=3").json()
    assert len(whole["documents"]) == 3 and whole["truncated"] is False


def test_other_statuses_never_crowd_out_an_orphan(env, settled):
    orphan = _orphan(env)  # the oldest row
    for _ in range(3):
        env["scan"](GRN_READ)
    r = _client().get(
        "/api/supporting-documents?status=pending,accepted&stored=false&limit=2"
    )
    assert r.status_code == 200, r.text
    assert [d["id"] for d in r.json()["documents"]] == [orphan["id"]]
    assert r.json()["truncated"] is False
