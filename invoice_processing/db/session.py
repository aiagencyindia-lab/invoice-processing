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
    return os.getenv("DATABASE_URL", "sqlite://")


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
    if _engine is None or str(_engine.url) != target:
        _engine = create_engine(target, future=True, **kwargs)
        _engine._target = target
        _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


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
    assert _Session is not None
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
