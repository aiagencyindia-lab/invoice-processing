"""Alembic environment.

The connection is resolved at runtime rather than written into alembic.ini, so
no connection string -- and no password -- is ever committed.

Connections go through `get_engine()`, the same function the application uses,
rather than through `engine_from_config`. That matters for Cloud SQL: the
instance is reached with the Google connector and an IAM token, which has no URL
representation at all. Building an engine from a URL string here would quietly
bypass the connector and try to open a direct socket that is not listening.
"""

from logging.config import fileConfig

from alembic import context

from invoice_processing.db.models import Base
from invoice_processing.db.session import (
    cloud_sql_instance,
    database_url,
    get_engine,
)

config = context.config
if not cloud_sql_instance():
    config.set_main_option("sqlalchemy.url", database_url())

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    if cloud_sql_instance():
        raise RuntimeError(
            "Offline mode cannot render SQL for a Cloud SQL connector "
            "connection: there is no URL to render against."
        )
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = get_engine()
    with connectable.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            # SQLite alters a table by rebuilding it: copy, drop the old one,
            # rename. The app's engine enforces foreign keys on every
            # connection, and dropping a table other rows still reference
            # then fails -- so a database holding a single PO line could not
            # be migrated. SQLite's documented answer is enforcement off for
            # the rebuild, then a check afterwards. The pragma is ignored
            # inside a transaction, hence before Alembic opens one.
            _set_sqlite_foreign_keys(connection, on=False)
        try:
            if sqlite:
                # Checked first: SQLite's driver does not hold DDL in
                # Alembic's transaction, so a failure after migrating cannot
                # be rolled back. Broken data is refused with nothing changed.
                _refuse_broken_references(connection, "Nothing was migrated.")
            context.configure(
                connection=connection, target_metadata=target_metadata
            )
            with context.begin_transaction():
                context.run_migrations()
            if sqlite:
                # A migration that itself orphaned rows, said plainly.
                _refuse_broken_references(
                    connection,
                    "The migration ran and SQLite cannot undo it; restore "
                    "the database from a copy.",
                )
        finally:
            if sqlite:
                # The engine is the app's; this connection goes back to its
                # pool, and must not come out again with enforcement off.
                _set_sqlite_foreign_keys(connection, on=True)


def _set_sqlite_foreign_keys(connection, *, on: bool) -> None:
    connection.exec_driver_sql(f"PRAGMA foreign_keys={'ON' if on else 'OFF'}")
    connection.commit()


def _refuse_broken_references(connection, outcome: str) -> None:
    """Stop if any row points at a row that does not exist."""
    broken = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    # Close the transaction the read opened. Left open, Alembic would run
    # inside it instead of its own, and the closing pragma would be ignored.
    connection.commit()
    if broken:
        tables = sorted({row[0] for row in broken})
        raise RuntimeError(
            f"{len(broken)} row(s) reference rows that do not exist, in: "
            f"{', '.join(tables)}. {outcome}"
        )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
