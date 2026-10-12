"""Bring the database up to date at startup.

Nothing did. The image starts uvicorn directly, the entrypoint only
materialises a credential, and neither the API nor the worker ran Alembic --
so production is on `b1f3c7a90e42` because somebody ran it by hand once. The
next migration would not have applied itself, and a fresh environment starts
happily and fails on the first query.

Two properties make running it automatically safe enough to prefer over
remembering:

* **It is a no-op when there is nothing to do.** Alembic compares the version
  table and returns immediately.
* **Only one process migrates at a time.** Every replica of the API and the
  worker starts at once on a deploy; a Postgres advisory lock means the others
  wait and then find the work already done, rather than racing each other
  through the same DDL. SQLite gets the same guarantee from an exclusive lock
  on a file beside the database: the local stack starts the API and the worker
  together against one file, and the loser of that race used to die on
  `duplicate column name` halfway through a migration the winner had applied.

Deployed environments set `RUN_MIGRATIONS=0` and run
`python -m invoice_processing.db.migrate` as the owner role in a release step
(Railway: the pre-deploy command); the app's connection has no DDL rights.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text

from .session import get_engine, is_postgres

logger = logging.getLogger(__name__)

# One arbitrary, stable number. Any process taking this lock is trying to
# migrate this database, which is exactly who should wait for whom.
_MIGRATION_LOCK_ID = 8_274_411_903


def enabled() -> bool:
    return os.getenv("RUN_MIGRATIONS", "1").lower() not in (
        "0",
        "false",
        "no",
    )


def upgrade_to_head() -> str | None:
    """Apply outstanding migrations. Returns the revision now in place.

    Raises on failure: a process serving traffic against a schema it could not
    migrate is the failure this exists to prevent, and starting anyway would
    turn a clear startup error into a stream of 500s.
    """
    if not enabled():
        logger.info("RUN_MIGRATIONS is off; leaving the schema alone.")
        return None

    from alembic import command  # noqa: PLC0415
    from alembic.config import Config  # noqa: PLC0415

    from .. import paths  # noqa: PLC0415

    engine = get_engine()
    # Built without the ini file on purpose. `env.py` calls `fileConfig()`
    # whenever Alembic was given one, which reconfigures the root logger and
    # silences every logger the host process had already set up -- including
    # the startup checks that run beside this. The only setting we need from
    # the ini is the script location, and that is passed directly.
    config = Config()
    config.set_main_option(
        "script_location",
        str(paths.AGENT_ROOT / "invoice_processing" / "db" / "migrations"),
    )

    if not is_postgres(engine):
        with _sqlite_migration_lock(engine.url.database):
            command.upgrade(config, "head")
        return current_revision()

    with engine.connect() as connection:
        connection.execute(
            text("select pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_ID}
        )
        connection.commit()
        try:
            command.upgrade(config, "head")
        finally:
            connection.execute(
                text("select pg_advisory_unlock(:key)"),
                {"key": _MIGRATION_LOCK_ID},
            )
            connection.commit()

    revision = current_revision()
    logger.info("Database schema is at %s", revision)
    return revision


@contextmanager
def _sqlite_migration_lock(database: str | None) -> Iterator[None]:
    """Hold an exclusive lock on `<database>.migrate-lock` while migrating.

    SQLite has no advisory locks, so the lock is a file next to the database.
    The second process blocks here, then finds Alembic's version table already
    at head and does nothing. An in-memory database is private to its
    connection, so there is nothing to share and nothing to lock.
    """
    if not database or database == ":memory:":
        yield
        return
    # ponytail: fcntl is POSIX only (macOS, Linux, the Docker image). A Windows
    # host would need msvcrt.locking or the `filelock` package here.
    import fcntl  # noqa: PLC0415

    with open(f"{database}.migrate-lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def current_revision() -> str | None:
    from alembic.migration import MigrationContext  # noqa: PLC0415

    with get_engine().connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def main(argv: list[str] | None = None) -> int:
    """`python -m invoice_processing.db.migrate`: the release step.

    The API and worker connect as a role that owns nothing and cannot run
    DDL (see db/bootstrap.sql). Migrations run here, as the owner, before a
    deploy serves traffic. MIGRATE_* name the owner's connection so the
    app's own settings stay the least-privilege ones.
    """
    for source, target in (
        ("MIGRATE_DATABASE_URL", "DATABASE_URL"),
        ("MIGRATE_CLOUD_SQL_USER", "CLOUD_SQL_USER"),
    ):
        if os.getenv(source):
            os.environ[target] = os.environ[source]
    os.environ["RUN_MIGRATIONS"] = "1"
    logging.basicConfig(level=logging.INFO)
    try:
        print(upgrade_to_head())
    except Exception:
        logger.exception(
            "Migration failed; nothing is serving a schema it could not reach."
        )
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
