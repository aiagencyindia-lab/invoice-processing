"""Scanned POs and receipts wait in supporting_documents for a human."""

from sqlalchemy import inspect

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine


def test_table_is_a_tenant_table_and_builds_with_the_links(scratch_db):
    url = scratch_db("sd.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    insp = inspect(get_engine(url))
    assert "supporting_documents" in models.TENANT_TABLES
    assert insp.has_table("supporting_documents")
    cols = lambda t: {c["name"] for c in insp.get_columns(t)}  # noqa: E731
    assert "kind" in cols("documents")
    assert "document_id" in cols("purchase_orders")
    assert "document_id" in cols("goods_receipts")
    uniques = {
        u["name"] for u in insp.get_unique_constraints("supporting_documents")
    }
    assert "uq_supporting_documents_document" in uniques


# --- repositories -----------------------------------------------------------
import pytest  # noqa: E402

from invoice_processing.adapters import (  # noqa: E402
    JsonDocumentRepository,
    JsonSupportingDocumentRepository,
)
from invoice_processing.db.repositories import (  # noqa: E402
    PgDocumentRepository,
    PgPurchaseOrderRepository,
    PgSupportingDocumentRepository,
)
from invoice_processing.db.session import tenant_session  # noqa: E402

PO = {
    "po_number": "PO-1",
    "vendor_name": "V",
    "total_amount": 50,
    "lines": [{"line_number": 1, "qty_ordered": 5, "unit_price": 10}],
}
GRN = {
    "grn_number": "G1",
    "po_number": "PO-1",
    "received_on": "2026-10-01",
    "lines": [{"line_number": 1, "qty_received": 2}],
}


@pytest.fixture
def env(scratch_db):
    url = scratch_db("sdr.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    docs = PgDocumentRepository(url)
    ids = {}
    for t in ("t1", "t2"):
        with tenant_session(t, url) as s:
            s.add(models.Tenant(id=t, name=t, slug=t))
        ids[t] = [
            docs.create(
                t, storage_key=f"{t}{i}", sha256=f"{t}{i}", filename=f"f{i}.pdf",
                byte_size=1,
            )
            for i in range(2)
        ]  # fmt: skip
    return url, docs, ids, PgSupportingDocumentRepository(url)


def _up(repo, t, doc, **kw):
    args = {
        "document_id": doc, "kind": "purchase_order", "extracted": {"a": 1},
        "po_number": "po-1", "blocked_reason": None, "extraction_error": None,
    }  # fmt: skip
    return repo.upsert_from_extraction(t, **{**args, **kw})


def test_upsert_creates_then_overwrites_pending(env):
    _, _, ids, repo = env
    a = _up(repo, "t1", ids["t1"][0])
    assert a["status"] == "pending" and a["filename"] == "f0.pdf"
    assert a["po_number"] == "PO1"
    b = _up(
        repo, "t1", ids["t1"][0], extracted={"a": 2}, blocked_reason="no PO"
    )
    assert b["id"] == a["id"] and b["extracted"] == {"a": 2}
    assert b["status"] == "blocked"
    assert len(repo.list("t1", ("pending", "blocked"))) == 1


def test_decided_rows_are_not_overwritten_and_mark_stores_correction(env):
    _, _, ids, repo = env
    a = _up(repo, "t1", ids["t1"][0])
    m = repo.mark(
        "t1", a["id"], status="accepted", reviewed_by="c@x", note="ok",
        extracted={"fixed": True},
    )  # fmt: skip
    assert m["status"] == "accepted" and m["extracted"] == {"fixed": True}
    assert m["reviewed_at"] and m["reviewed_by"] == "c@x"
    assert _up(repo, "t1", ids["t1"][0], extracted={"z": 1}) is None
    assert repo.get("t1", a["id"])["extracted"] == {"fixed": True}
    assert repo.list("t1", ("pending",)) == []


def test_unblock_moves_only_the_matching_normalised_po(env):
    _, _, ids, repo = env
    a = _up(repo, "t1", ids["t1"][0], blocked_reason="PO not on file")
    b = _up(
        repo,
        "t1",
        ids["t1"][1],
        po_number="PO-2",
        blocked_reason="PO not on file",
    )
    assert repo.unblock("t1", " PO-1 ") == [a["id"]]
    assert repo.get("t1", a["id"])["status"] == "pending"
    assert repo.get("t1", a["id"])["blocked_reason"] is None
    assert repo.get("t1", b["id"])["status"] == "blocked"


def test_tenants_are_isolated(env):
    _, _, ids, repo = env
    a = _up(repo, "t1", ids["t1"][0], blocked_reason="x")
    assert repo.get("t2", a["id"]) is None
    assert repo.list("t2", ("pending", "blocked")) == []
    assert repo.unblock("t2", "po-1") == []
    with pytest.raises(KeyError):
        repo.mark("t2", a["id"], status="rejected", reviewed_by="x")


def test_set_kind_and_listing(env):
    _, docs, ids, _ = env
    docs.set_kind("t1", ids["t1"][0], "goods_receipt")
    docs.set_kind("t2", ids["t1"][1], "goods_receipt")  # wrong tenant: no-op
    kinds = {d["id"]: d["kind"] for d in docs.list_documents("t1")}
    assert kinds[ids["t1"][0]] == "goods_receipt"
    assert kinds[ids["t1"][1]] is None


def test_document_link_and_upload_ownership(env):
    url, _, ids, _ = env
    po = PgPurchaseOrderRepository(url)
    po.upsert_orders("t1", [PO], uploaded_by="a", source="document",
                     document_id=ids["t1"][0])  # fmt: skip
    po.upsert_orders("t1", [{**PO, "po_number": "PO-U"}], uploaded_by="a")
    po.record_receipts("t1", [GRN], recorded_by="a", source="document",
                       document_id=ids["t1"][1])  # fmt: skip
    o = po.get_order("t1", "PO-1")
    assert o["document_id"] == ids["t1"][0] and o["source"] == "document"
    assert o["receipts"][0]["document_id"] == ids["t1"][1]
    assert po.list_receipts("t1")[0]["document_id"] == ids["t1"][1]
    assert po.get_order("t1", "PO-U")["document_id"] is None
    assert po.po_owned_by_upload("t1", "po-u") is True
    assert po.po_owned_by_upload("t1", "PO-1") is False
    assert po.po_owned_by_upload("t1", "nope") is False
    assert po.po_owned_by_upload("t2", "PO-U") is False


def test_json_fallback_matches_the_contract(tmp_path):
    repo = JsonSupportingDocumentRepository(tmp_path / "sd.json")
    a = _up(repo, "t1", "d1", blocked_reason="x")
    assert a["status"] == "blocked"
    assert _up(repo, "t1", "d1")["id"] == a["id"]
    assert repo.unblock("t1", "PO 1") == []  # already pending again
    _up(repo, "t1", "d1", blocked_reason="x")
    assert repo.unblock("t1", "po-1") == [a["id"]]
    m = repo.mark(
        "t1", a["id"], status="rejected", reviewed_by="c", extracted={"q": 1}
    )
    assert m["extracted"] == {"q": 1}
    assert _up(repo, "t1", "d1") is None
    assert repo.list("t2", ("rejected",)) == []
    assert [r["id"] for r in repo.list("t1", ("rejected",))] == [a["id"]]
    docs = JsonDocumentRepository(tmp_path / "docs.json")
    d = docs.create("t1", filename="f")
    docs.set_kind("t1", d, "goods_receipt")
    assert docs.list_documents("t1")[0]["kind"] == "goods_receipt"
