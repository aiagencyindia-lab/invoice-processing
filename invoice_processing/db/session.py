"""Engine and tenant-scoped sessions.

Every session that touches tenant data must be opened through
`tenant_session()`. It sets the Postgres setting the RLS policies read, so a
query that forgets its `WHERE tenant_id = ...` returns nothing rather than
another tenant's data.

`DATABASE_URL` selects the backend. Tests use SQLite, which has no RLS -- so the
repositories filter explicitly as well, and the isolation tests assert that
filtering directly rather than relying on the database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base
from .rls import set_tenant_statement

_engine: Engine | None = None
_Session: sessionmaker | None = None


def database_url() -> str:
    """The configured URL, or local SQLite.

    `or` rather than a getenv default: a default applies only when the variable
    is *absent*, and `DATABASE_URL=` is how this project's own run commands and
    CI blank a setting. Set-but-empty therefore reached create_engine as "" and
    failed with "Could not parse SQLAlchemy URL", which names neither the
    variable nor the fix.
    """
    return os.getenv("DATABASE_URL") or "sqlite://"


def cloud_sql_instance() -> str:
    """`project:region:instance`, when connecting through the Cloud SQL connector."""
    return os.getenv("CLOUD_SQL_INSTANCE", "")


def _cloud_sql_engine(**kwargs) -> Engine:
    """An engine that reaches Cloud SQL through the Google connector.

    Preferred over a host and port for two reasons that matter here:

    * **No password.** With `CLOUD_SQL_IAM_AUTH` the service account itself
      authenticates, so there is no database password to put in `.env`, rotate,
      or leak. The credential becomes an IAM grant, which can be revoked
      centrally and is audited.
    * **No IP allowlist.** The connector opens a TLS tunnel using IAM, so the
      instance needs no public IP and no authorized-network list that has to be
      updated whenever someone's address changes.

    The same code path works locally and on Cloud Run; only the ambient
    credentials differ.
    """
    from google.cloud.sql.connector import Connector, IPTypes  # noqa: PLC0415

    instance = cloud_sql_instance()
    user = os.getenv("CLOUD_SQL_USER", "postgres")
    database = os.getenv("CLOUD_SQL_DATABASE", "postgres")
    password = os.getenv("CLOUD_SQL_PASSWORD", "")
    iam_auth = os.getenv("CLOUD_SQL_IAM_AUTH", "").lower() in (
        "1",
        "true",
        "yes",
    )
    ip_type = (
        IPTypes.PRIVATE
        if os.getenv("CLOUD_SQL_PRIVATE_IP", "").lower() in ("1", "true", "yes")
        else IPTypes.PUBLIC
    )

    connector = Connector(ip_type=ip_type)

    def connect():
        return connector.connect(
            instance,
            "pg8000",
            user=user,
            db=database,
            enable_iam_auth=iam_auth,
            **({} if iam_auth else {"password": password}),
        )

    engine = create_engine(
        "postgresql+pg8000://",
        creator=connect,
        future=True,
        # Cloud SQL drops idle connections; recycling below that keeps a
        # long-lived worker from picking up a dead one mid-transaction.
        pool_pre_ping=True,
        pool_recycle=1800,
        **kwargs,
    )
    # Kept so the connector's background threads are closed with the engine
    # rather than leaking for the life of the process.
    engine._cloud_sql_connector = connector
    return engine


def get_engine(url: str | None = None, **kwargs) -> Engine:
    """Resolve an engine, most explicit source winning.

    Precedence is deliberate: an explicit argument, then an explicit
    `DATABASE_URL`, then the Cloud SQL connector, then SQLite. `DATABASE_URL`
    outranks `CLOUD_SQL_INSTANCE` because `.env` carries the Cloud SQL settings
    for everyday development -- if that silently outranked an explicitly set
    URL, a test or a CI job pointing at a scratch database would instead open a
    connection to the production instance.
    """
    global _engine, _Session
    if url is None and not os.getenv("DATABASE_URL") and cloud_sql_instance():
        target = f"cloudsql://{cloud_sql_instance()}"
        if _engine is None or getattr(_engine, "_target", None) != target:
            _engine = _cloud_sql_engine(**kwargs)
            _engine._target = target
            _Session = sessionmaker(bind=_engine, expire_on_commit=False)
        return _engine

    target = url or database_url()
    # Compared against the string we built the engine from, never against
    # str(engine.url): that masks the password, so on any Postgres URL it
    # never matched and every call disposed the pool and reconnected.
    if _engine is None or getattr(_engine, "_target", None) != target:
        if _engine is not None:
            # Release the old pool now, not at garbage collection: on Postgres
            # each switch otherwise holds connections until the server
            # refuses new clients.
            _engine.dispose()
        _engine = create_engine(target, future=True, **kwargs)
        _engine._target = target
        if _engine.dialect.name == "sqlite":
            _enforce_sqlite_foreign_keys(_engine)
        _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def _enforce_sqlite_foreign_keys(engine: Engine) -> None:
    """Make SQLite refuse what Postgres refuses.

    SQLite ships with foreign keys off, per connection. The suite runs on
    SQLite and production runs on Postgres, so every reference the tests write
    in the wrong order is accepted here and rejected there -- which is how a
    fixture writing an invoice before its tenant passed for months and then
    failed the first time CI met a real database.

    On for every connection this engine opens, so the two dialects disagree
    about one less thing.
    """
    from sqlalchemy import event  # noqa: PLC0415

    @event.listens_for(engine, "connect")
    def _pragma(connection, _record):  # pragma: no cover - trivial
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_all(url: str | None = None) -> Engine:
    """Create the schema. Alembic owns this in production; tests use it directly."""
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    return engine


def is_postgres(engine: Engine) -> bool:
    return engine.dialect.name == "postgresql"


@contextmanager
def tenant_session(tenant_id: str, url: str | None = None) -> Iterator[Session]:
    """A session scoped to one tenant.

    On Postgres this sets `app.tenant_id`, which every RLS policy compares
    against. On other backends it is a no-op and the repositories' own filtering
    is the only isolation -- which is why that filtering is tested explicitly.
    """
    engine = get_engine(url)
    if _Session is None:  # asserts vanish under python -O
        raise RuntimeError("No session factory: get_engine() did not run.")
    session = _Session()
    try:
        if is_postgres(engine):
            session.execute(
                text(set_tenant_statement()), {"tenant_id": tenant_id}
            )
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
