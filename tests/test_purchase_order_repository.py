"""Purchase orders, their lines and goods receipts are tenant tables."""

from sqlalchemy import inspect

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine


def test_tables_are_tenant_tables_and_build(scratch_db):
    url = scratch_db("po.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    names = ("purchase_orders", "po_lines", "goods_receipts", "grn_lines")
    assert set(names) <= set(models.TENANT_TABLES)
    assert all(inspect(get_engine(url)).has_table(n) for n in names)
