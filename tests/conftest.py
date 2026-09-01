"""Pytest configuration for invoice_processing tests."""

import os
import sys
from pathlib import Path

import pytest

# Ensure the agent package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# So `tests/golden.py` is importable from the test modules beside it.
sys.path.insert(0, str(Path(__file__).resolve().parent))


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
