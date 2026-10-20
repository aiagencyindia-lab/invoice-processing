from fresh_schema import fresh_schema
from sqlalchemy import inspect, text

from invoice_processing.db import models
from invoice_processing.db.session import (
    get_engine,
    is_postgres,
    tenant_session,
)


def test_second_call_empties_the_tables_without_rebuilding_them(scratch_db):
    url = scratch_db("fresh.db")
    fresh_schema(url)
    with tenant_session("t1", url) as s:
        s.add(models.Tenant(id="t1", name="A", slug="a", status="active"))
    engine = get_engine(url)
    oid = "SELECT oid FROM pg_class WHERE relname = 'tenants'"
    before = (
        engine.connect().execute(text(oid)).scalar()
        if is_postgres(engine)
        else 0
    )

    fresh_schema(url)

    assert set(models.Base.metadata.tables) <= set(
        inspect(engine).get_table_names()
    )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM tenants")).scalar() == 0
        if is_postgres(engine):
            # Same relation: truncated, not dropped and recreated.
            assert conn.execute(text(oid)).scalar() == before
