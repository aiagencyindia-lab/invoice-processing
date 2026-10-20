"""One empty schema per test, without rebuilding it on Postgres.

Every DB-backed module used to open its fixture with `drop_all` + `create_all`.
On SQLite that is free: each test gets a new file. On Postgres it is forty
tables of DDL per test -- around a second each, and the job that reruns the
suite there passed thirty minutes. So on Postgres the schema is built once per
process and emptied after. Emptied with DELETE, not TRUNCATE: TRUNCATE gives
every table and index a new file, which costs about as much as the DDL it
replaces, while DELETE on near-empty tables is ten milliseconds.
"""

from sqlalchemy import inspect

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, is_postgres


def fresh_schema(url: str) -> None:
    """Leave `url` holding the current schema and no rows."""
    engine = get_engine(url)
    metadata = models.Base.metadata
    if is_postgres(engine) and set(metadata.tables) <= set(
        inspect(engine).get_table_names()
    ):
        with engine.begin() as conn:
            # Children before parents, so no foreign key objects.
            for table in reversed(metadata.sorted_tables):
                conn.execute(table.delete())
        return
    metadata.drop_all(engine)
    create_all(url)
