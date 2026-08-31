"""Pytest configuration for invoice_processing tests."""

import os
import sys
from pathlib import Path

import pytest

# Ensure the agent package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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
