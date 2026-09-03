"""Pytest configuration for invoice_processing tests."""

import os
import sys
from pathlib import Path

import pytest

from invoice_processing.api import service

# Ensure the agent package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# So `tests/golden.py` is importable from the test modules beside it.
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture
def scratch_db(tmp_path):
    """A URL for a throwaway database: SQLite, or Postgres when CI says so.

    The suite runs on SQLite because it is free and needs no service. That is
    also how three real bugs shipped: SQLite coerces `numeric >= varchar` where
    Postgres refuses outright, it parses a thousands separator Postgres will
    not, and a query per row is invisible when queries cost nothing. None of
    those could fail on the test dialect.

    So the DB-backed modules take their URL from here, and one CI job runs them
    again with `TEST_DATABASE_URL` pointing at a real Postgres. Same tests,
    second dialect -- rather than a parallel suite that would drift.
    """

    def make(name: str = "test.db") -> str:
        return os.getenv("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / name}"

    return make


@pytest.fixture(autouse=True)
def a_project_id_that_is_not_real(monkeypatch):
    """Give Vertex a project so construction never needs a configured machine.

    `google.cloud.aiplatform` resolves its project at client construction and
    raises when it cannot find one. Anything that builds a model -- including
    `RuleDiscoveryEngine`, whose cache-key test only wants a hash -- therefore
    failed on a machine with no `GOOGLE_CLOUD_PROJECT`, while passing on a
    developer's because `.env` supplies one.

    Set unconditionally rather than only when absent, so the suite behaves the
    same everywhere and can never construct a client pointed at a real project.
    No network call happens at construction; the other guards in this file stop
    anything that would.
    """
    for name in (
        "GOOGLE_CLOUD_PROJECT",
        "GCP_PROJECT",
        "GOOGLE_CLOUD_PROJECT_ID",
    ):
        monkeypatch.setenv(name, "test-project-not-real")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")


@pytest.fixture(autouse=True)
def resolve_adapters_afresh(monkeypatch):
    """Clear the memoized adapters between tests.

    They are cached after first use, which is right in a process serving
    traffic and wrong in a suite: a store resolved under one test's
    environment -- its temp data directory, its database URL -- would
    otherwise be handed to the next test, which configured something else and
    is entitled to get it.
    """
    for name in (
        "_cases",
        "_documents",
        "_audit",
        "_corrections",
        "_claim_store",
        "_locks",
        "_rules",
        "_tenant_settings",
        "_claims_writer",
    ):
        monkeypatch.setattr(service, name, None, raising=False)


@pytest.fixture(autouse=True)
def never_touch_real_cloud_storage(monkeypatch):
    """Fail loudly if a test would construct a real GCS client.

    `GCS_BUCKET` is set in `.env` so the app talks to the real bucket in
    development. That makes an unguarded test a live write to production
    storage: any test that reaches the ingestion path without injecting a fake
    blob store would upload into the customer's bucket, and the first sign of it
    would be objects appearing there.

    Tests inject fakes today, so this guard is invisible. It exists so that a
    test written later cannot quietly stop doing so.
    """

    def refuse(*args, **kwargs):
        raise AssertionError(
            "A test tried to open a real Cloud Storage client. Inject a fake "
            "blob store (see FakeBlobs in test_ingestion.py) or patch "
            "service.get_blobs instead."
        )

    monkeypatch.setattr("google.cloud.storage.Client", refuse, raising=False)


@pytest.fixture(autouse=True)
def never_touch_cloud_sql(monkeypatch):
    """Fail loudly if a test would open a Cloud SQL connection.

    `.env` carries `CLOUD_SQL_INSTANCE`, so any code path calling `get_engine()`
    without an explicit URL would reach the live instance. Tests pass explicit
    SQLite URLs today; this makes a test that forgets to fail loudly rather than
    running DDL against production.
    """
    monkeypatch.setenv("DATABASE_URL", os.getenv("DATABASE_URL", "sqlite://"))
    monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)


@pytest.fixture(autouse=True)
def never_touch_pubsub(monkeypatch):
    """Fail loudly if a test would open a real Pub/Sub client.

    `get_queue()` returns a real `PubSubQueue` whenever `PUBSUB_TOPIC` and
    `GOOGLE_CLOUD_PROJECT` are both set, and `.env` already carries the project.
    One added line in `.env` would turn a test run into live publishes against a
    production subscription.
    """

    def refuse(*args, **kwargs):
        raise AssertionError(
            "A test tried to open a real Pub/Sub client. Inject fake "
            "publisher/subscriber objects into PubSubQueue instead."
        )

    monkeypatch.delenv("PUBSUB_TOPIC", raising=False)
    for name in ("PublisherClient", "SubscriberClient"):
        monkeypatch.setattr(
            f"google.cloud.pubsub_v1.{name}", refuse, raising=False
        )


@pytest.fixture(autouse=True)
def never_open_the_cloud_sql_connector(monkeypatch):
    """Block the connector itself, not just the environment that selects it.

    The `never_touch_cloud_sql` guard works by clearing `CLOUD_SQL_INSTANCE`,
    but several modules call `load_dotenv()` at import time -- so a module first
    imported inside a test body can put the variable straight back. Blocking the
    Connector class closes that path regardless of import order.
    """

    def refuse(*args, **kwargs):
        raise AssertionError(
            "A test tried to open a Cloud SQL connection. Pass an explicit "
            "sqlite:// URL to get_engine() instead."
        )

    monkeypatch.setattr(
        "google.cloud.sql.connector.Connector", refuse, raising=False
    )
