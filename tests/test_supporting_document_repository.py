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
