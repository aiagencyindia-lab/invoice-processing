"""The KPI routes: who may read them, what windows exist, and that they
refuse plainly without a database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from fresh_schema import fresh_schema

from invoice_processing.api import auth
from invoice_processing.api.service import app
from invoice_processing.db.models import Invoice, Tenant
from invoice_processing.db.session import tenant_session

TENANT = "tenant-acme"
CLAIMS = {
    f"tok-{role}": {
        "user_id": f"u-{role}",
        "email": f"{role}@acme.test",
        "email_verified": True,
        "tenant_id": TENANT,
        "role": role,
    }
    for role in ("viewer", "clerk", "approver", "admin")
}


class FakeVerifier:
    def verify(self, token):
        if token not in CLAIMS:
            raise auth.AuthError("Invalid token")
        return CLAIMS[token]


@pytest.fixture
def db(scratch_db, monkeypatch):
    url = scratch_db("kpi-api.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
    fresh_schema(url)
    with tenant_session(TENANT, url) as session:
        session.add(Tenant(id=TENANT, slug="acme", name="Acme"))
        session.flush()
        session.add(
            Invoice(
                tenant_id=TENANT,
                case_id="c1",
                routing="REVIEW",
                decision="ACCEPT",
                created_at=datetime.now(timezone.utc) - timedelta(days=2),
                fields={},
            )
        )
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    return url


def client(role="viewer"):
    return TestClient(app, headers={"Authorization": f"Bearer tok-{role}"})


def test_a_viewer_can_read_the_window(db):
    body = client("viewer").get("/api/kpis").json()
    assert body["window"]["days"] == 30
    assert body["window"]["since"] is not None
    assert body["received"] == 1
    assert body["exceptions"]["low_confidence"] == 1


def test_all_time_has_no_since(db):
    body = client().get("/api/kpis", params={"days": 0}).json()
    assert body["window"] == {"days": 0, "since": None}


def test_only_the_known_windows_are_accepted(db):
    assert client().get("/api/kpis", params={"days": 31}).status_code == 422
    assert client().get("/api/kpis", params={"days": "week"}).status_code == 422


def test_cases_behind_a_reason(db):
    body = (
        client()
        .get("/api/kpis/cases", params={"reason": "low_confidence", "days": 7})
        .json()
    )
    assert body == {"case_ids": ["c1"], "truncated": False}


def test_an_unknown_reason_is_refused(db):
    response = client().get("/api/kpis/cases", params={"reason": "vibes"})
    assert response.status_code == 422


def test_no_token_is_refused(db):
    assert TestClient(app).get("/api/kpis").status_code == 401


def test_without_a_database_it_says_so(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    for path in ("/api/kpis", "/api/kpis/cases?reason=other"):
        response = client().get(path)
        assert response.status_code == 503
        assert "needs a database" in response.json()["detail"]
