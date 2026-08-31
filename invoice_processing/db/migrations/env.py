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
        context.configure(
            connection=connection, target_metadata=target_metadata
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
