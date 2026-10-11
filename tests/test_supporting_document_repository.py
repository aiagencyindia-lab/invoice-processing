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
    StatusChanged,
    UploadOwned,
)
from invoice_processing.db.session import tenant_session  # noqa: E402
from invoice_processing.ports import ConcurrentWrite  # noqa: E402

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
        extracted={"fixed": True}, expect=("pending",),
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
        repo.mark(
            "t2", a["id"], status="rejected", reviewed_by="x",
            expect=("blocked",),
        )  # fmt: skip


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


def test_upload_after_a_scan_takes_ownership(env):
    url, _, ids, _ = env
    po = PgPurchaseOrderRepository(url)
    po.upsert_orders(
        "t1", [PO], uploaded_by="a", source="document", document_id=ids["t1"][0]
    )
    assert po.po_owned_by_upload("t1", "PO-1") is False
    po.upsert_orders("t1", [PO], uploaded_by="a")
    o = po.get_order("t1", "PO-1")
    assert o["source"] == "upload" and o["document_id"] is None
    assert po.po_owned_by_upload("t1", "PO-1") is True


def test_a_scan_cannot_overwrite_an_uploaded_po(env):
    url, _, ids, _ = env
    po = PgPurchaseOrderRepository(url)
    po.upsert_orders("t1", [PO], uploaded_by="erp")
    scanned = {**PO, "vendor_name": "Scanned"}
    with pytest.raises(UploadOwned) as refused:
        po.upsert_orders(
            "t1", [scanned], uploaded_by="c", source="document",
            document_id=ids["t1"][0],
        )  # fmt: skip
    assert refused.value.po_number == "PO-1"
    o = po.get_order("t1", "PO-1")
    assert o["vendor_name"] == "V" and o["source"] == "upload"
    assert o["document_id"] is None
    # A scan may still update a scanned PO, and the other tenant's own.
    po.upsert_orders("t2", [scanned], uploaded_by="c", source="document")
    po.upsert_orders("t2", [scanned], uploaded_by="c", source="document")
    assert po.get_order("t2", "PO-1")["vendor_name"] == "Scanned"


def test_mark_is_compare_and_set(env):
    _, _, ids, repo = env
    a = _up(repo, "t1", ids["t1"][0])
    done = repo.mark(
        "t1", a["id"], status="accepted", reviewed_by="first",
        expect=("pending",),
    )  # fmt: skip
    assert done["status"] == "accepted"
    with pytest.raises(StatusChanged) as late:
        repo.mark(
            "t1", a["id"], status="rejected", reviewed_by="second",
            expect=("pending", "blocked"),
        )  # fmt: skip
    assert late.value.status == "accepted"
    kept = repo.get("t1", a["id"])
    assert kept["status"] == "accepted" and kept["reviewed_by"] == "first"
    with pytest.raises(KeyError):
        repo.mark("t1", "nope", status="x", reviewed_by="y", expect=("a",))


def test_revert_to_pending_restores_the_reading(env):
    _, _, ids, repo = env
    a = _up(repo, "t1", ids["t1"][0])
    repo.mark(
        "t1", a["id"], status="accepted", reviewed_by="c", note="n",
        extracted={"fixed": 1}, expect=("pending",),
    )  # fmt: skip
    back = repo.revert_to_pending("t1", a["id"], extracted={"a": 1})
    assert back["status"] == "pending" and back["extracted"] == {"a": 1}
    assert back["reviewed_by"] is None and back["reviewed_at"] is None
    assert back["note"] is None
    with pytest.raises(StatusChanged):
        repo.revert_to_pending("t1", a["id"], extracted={})


def test_revert_puts_a_blocked_row_back_to_blocked(env):
    _, _, ids, repo = env
    a = _up(repo, "t1", ids["t1"][0], blocked_reason="PO-1 not on file")
    repo.mark(
        "t1", a["id"], status="accepted", reviewed_by="c",
        expect=("pending", "blocked"),
    )  # fmt: skip
    back = repo.revert_to_pending(
        "t1", a["id"], extracted={}, status="blocked",
        blocked_reason="PO-1 not on file",
    )  # fmt: skip
    assert back["status"] == "blocked"
    assert back["blocked_reason"] == "PO-1 not on file"


def test_a_concurrent_po_insert_is_a_conflict(env, monkeypatch):
    url, _, _, _ = env
    po = PgPurchaseOrderRepository(url)
    po.upsert_orders("t1", [PO], uploaded_by="a")
    # As if another import inserted it after this one looked.
    monkeypatch.setattr(
        PgPurchaseOrderRepository, "_order", staticmethod(lambda *a, **k: None)
    )
    with pytest.raises(ConcurrentWrite):
        po.upsert_orders("t1", [PO], uploaded_by="a")


def test_for_documents_reads_only_those_documents(env):
    _, _, ids, repo = env
    a = _up(repo, "t1", ids["t1"][0])
    _up(repo, "t1", ids["t1"][1])
    _up(repo, "t2", ids["t2"][0])
    got = repo.for_documents("t1", [ids["t1"][0], ids["t2"][0]])
    assert [r["id"] for r in got] == [a["id"]]
    assert repo.for_documents("t1", []) == []


def test_json_fallback_matches_the_contract(tmp_path):
    repo = JsonSupportingDocumentRepository(tmp_path / "sd.json")
    a = _up(repo, "t1", "d1", blocked_reason="x")
    assert a["status"] == "blocked"
    assert _up(repo, "t1", "d1")["id"] == a["id"]
    assert repo.unblock("t1", "PO 1") == []  # already pending again
    _up(repo, "t1", "d1", blocked_reason="x")
    assert repo.unblock("t1", "po-1") == [a["id"]]
    m = repo.mark(
        "t1", a["id"], status="rejected", reviewed_by="c", extracted={"q": 1},
        expect=("pending",),
    )  # fmt: skip
    assert m["extracted"] == {"q": 1}
    with pytest.raises(StatusChanged):
        repo.mark(
            "t1", a["id"], status="x", reviewed_by="c", expect=("pending",)
        )
    assert repo.get("t1", a["id"])["status"] == "rejected"
    with pytest.raises(StatusChanged):
        repo.revert_to_pending("t1", a["id"], extracted={})
    assert [r["id"] for r in repo.for_documents("t1", ["d1", "d2"])] == [
        a["id"]
    ]
    assert _up(repo, "t1", "d1") is None
    assert repo.list("t2", ("rejected",)) == []
    assert [r["id"] for r in repo.list("t1", ("rejected",))] == [a["id"]]
    docs = JsonDocumentRepository(tmp_path / "docs.json")
    d = docs.create("t1", filename="f")
    docs.set_kind("t1", d, "goods_receipt")
    assert docs.list_documents("t1")[0]["kind"] == "goods_receipt"


# --- schema defaults, migration, and the CAS details -------------------------
import os  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from sqlalchemy.exc import IntegrityError  # noqa: E402


def test_a_bare_row_defaults_to_pending_and_empty_and_is_unique(env):
    url, _, ids, _ = env
    with tenant_session("t1", url) as s:
        s.add(
            models.SupportingDocument(
                tenant_id="t1", document_id=ids["t1"][0], kind="goods_receipt"
            )
        )
    with tenant_session("t1", url) as s:
        row = s.scalar(select_sd("t1"))
        assert row.status == "pending" and row.extracted == {}
    with pytest.raises(IntegrityError):
        with tenant_session("t1", url) as s:
            s.add(
                models.SupportingDocument(
                    tenant_id="t1", document_id=ids["t1"][0], kind="x"
                )
            )


def select_sd(tenant_id):
    from sqlalchemy import select  # noqa: PLC0415

    return select(models.SupportingDocument).where(
        models.SupportingDocument.tenant_id == tenant_id
    )


def test_the_migration_round_trips_on_sqlite(tmp_path):
    db = tmp_path / "m.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db}",
        "CLOUD_SQL_INSTANCE": "",
        "GCS_BUCKET": "",
    }
    repo_root = Path(__file__).resolve().parents[1]

    def alembic(*args):
        done = subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=repo_root, env=env,
            capture_output=True, text=True, timeout=180, check=False,
        )  # fmt: skip
        assert done.returncode == 0, done.stderr

    def tables():
        with sqlite3.connect(db) as c:
            return {
                r[0]
                for r in c.execute(
                    "select name from sqlite_master where type='table'"
                )
            }

    alembic("upgrade", "head")
    assert "supporting_documents" in tables()
    with sqlite3.connect(db) as c:
        cols = {r[1] for r in c.execute("pragma table_info(documents)")}
    assert "kind" in cols
    alembic("downgrade", "base")
    assert tables() == {"alembic_version"}


def test_a_concurrent_first_insert_falls_back_to_the_overwrite_rule(
    env, monkeypatch
):
    _, _, ids, repo = env
    first = _up(repo, "t1", ids["t1"][0])
    real = PgSupportingDocumentRepository._existing
    calls = []

    def missed_once(*a, **k):
        # As if another worker inserted the row after this one looked.
        calls.append(1)
        return None if len(calls) == 1 else real(*a, **k)

    monkeypatch.setattr(
        PgSupportingDocumentRepository, "_existing", staticmethod(missed_once)
    )
    again = _up(repo, "t1", ids["t1"][0], extracted={"a": 2})
    assert again["id"] == first["id"] and again["extracted"] == {"a": 2}
    repo.mark(
        "t1", first["id"], status="accepted", reviewed_by="c",
        expect=("pending",),
    )  # fmt: skip
    calls.clear()
    assert _up(repo, "t1", ids["t1"][0], extracted={"z": 1}) is None


@pytest.mark.parametrize("kind", ["pg", "json"])
def test_mark_needs_the_expected_statuses(env, tmp_path, kind):
    _, _, ids, pg = env
    repo = (
        pg
        if kind == "pg"
        else JsonSupportingDocumentRepository(tmp_path / "sd.json")
    )
    doc = ids["t1"][0] if kind == "pg" else "d1"
    a = _up(repo, "t1", doc)
    with pytest.raises(TypeError):
        repo.mark("t1", a["id"], status="rejected", reviewed_by="c")


@pytest.mark.parametrize("kind", ["pg", "json"])
def test_mark_reports_what_it_replaced_and_accepting_clears_the_block(
    env, tmp_path, kind
):
    _, _, ids, pg = env
    repo = (
        pg
        if kind == "pg"
        else JsonSupportingDocumentRepository(tmp_path / "sd.json")
    )
    doc = ids["t1"][0] if kind == "pg" else "d1"
    a = _up(repo, "t1", doc, blocked_reason="PO-1 not on file")
    m = repo.mark(
        "t1", a["id"], status="accepted", reviewed_by="c",
        expect=("pending", "blocked"),
    )  # fmt: skip
    assert m["previous"] == {
        "status": "blocked",
        "blocked_reason": "PO-1 not on file",
    }
    assert m["blocked_reason"] is None
    back = repo.revert_to_pending(
        "t1", a["id"], extracted={}, status="blocked",
        blocked_reason="PO-1 not on file",
    )  # fmt: skip
    assert back["status"] == "blocked"
    assert back["blocked_reason"] == "PO-1 not on file"


def test_json_rows_carry_the_document_filename(tmp_path):
    docs = JsonDocumentRepository(tmp_path / "documents.json")
    d = docs.create("t1", filename="grn.pdf")
    repo = JsonSupportingDocumentRepository(tmp_path / "sd.json")
    a = _up(repo, "t1", d)
    assert a["filename"] == "grn.pdf"
    assert repo.get("t1", a["id"])["filename"] == "grn.pdf"
    assert repo.list("t1", ("pending",))[0]["filename"] == "grn.pdf"
    assert repo.for_documents("t1", [d])[0]["filename"] == "grn.pdf"


def test_stored_documents_names_what_a_po_or_receipt_came_from(env):
    url, _, ids, _ = env
    po = PgPurchaseOrderRepository(url)
    d0, d1 = ids["t1"]
    assert po.stored_documents("t1", [d0, d1]) == set()
    po.upsert_orders(
        "t1", [PO], uploaded_by="a", source="document", document_id=d0
    )
    po.record_receipts(
        "t1", [GRN], recorded_by="a", source="document", document_id=d1
    )
    assert po.stored_documents("t1", [d0, d1]) == {d0, d1}
    assert po.stored_documents("t2", [d0, d1]) == set()
    # A later upload takes the PO over: still stored, by its number.
    po.upsert_orders("t1", [PO], uploaded_by="erp")
    assert po.stored_documents("t1", [d0]) == set()
    assert po.stored_documents("t1", [d0], {d0: "po 1"}) == {d0}
    assert po.stored_documents("t1", [], {}) == set()
