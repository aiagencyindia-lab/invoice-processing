"""Rate limits and idempotency keys on /v2 (stored in the database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from invoice_processing.api import v2_guard
from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from tests.test_trust_api_routes import bearer, keyed
from tests.test_trust_api_routes import env as api_env  # noqa: F401 - fixture

ACME = "tenant-acme"


@pytest.fixture
def env(api_env):  # noqa: F811
    """The /v2 route tests' two tenants, keys and seeded agent."""
    return api_env


@pytest.fixture
def limit3(monkeypatch):
    monkeypatch.setattr(v2_guard, "limit_for", lambda tenant_id: 3)


def agents_named(url, name):
    with tenant_session(ACME, url) as s:
        return s.scalars(
            select(models.Agent).where(models.Agent.name == name)
        ).all()


# -- rate limits ---------------------------------------------------------------


def test_every_v2_answer_says_where_the_caller_stands(env, limit3):
    client = keyed(env["acme"]["keys"]["viewer"])
    r = client.get("/v2/findings")
    assert r.status_code == 200, r.text
    assert r.headers["X-RateLimit-Limit"] == "3"
    assert r.headers["X-RateLimit-Remaining"] == "2"
    assert 0 < int(r.headers["X-RateLimit-Reset"]) <= 60


def test_past_the_budget_is_429_with_retry_after(env, limit3):
    client = keyed(env["acme"]["keys"]["viewer"])
    for _ in range(3):
        assert client.get("/v2/findings").status_code == 200
    r = client.get("/v2/findings")
    assert r.status_code == 429
    assert 0 < int(r.headers["Retry-After"]) <= 60
    assert r.headers["X-RateLimit-Remaining"] == "0"
    # Another caller has its own budget.
    other = keyed(env["acme"]["keys"]["clerk"])
    assert other.get("/v2/findings").status_code == 200


def test_api_routes_and_failed_auth_are_not_counted(env, limit3):
    client = bearer("acme:viewer")
    for _ in range(5):
        assert client.get("/api/trust/findings").status_code == 200
    assert "X-RateLimit-Limit" not in client.get("/api/trust/findings").headers
    nobody = keyed("lk_not-a-real-key")
    for _ in range(5):
        assert nobody.get("/v2/findings").status_code == 401
    with tenant_session(ACME, env["url"]) as s:
        assert s.scalars(select(models.ApiRateWindow)).all() == []


def test_the_default_budget_is_the_tenant_threshold(env):
    assert v2_guard.limit_for(ACME) == 120


# -- idempotency keys ----------------------------------------------------------


BODY = {"name": "idem-bot", "owner": "ops@acme.test"}


def test_a_retried_post_is_answered_from_the_first(env):
    admin = bearer("acme:admin")
    first = admin.post(
        "/v2/agents", json=BODY, headers={"Idempotency-Key": "k-1"}
    )
    assert first.status_code == 201, first.text
    again = admin.post(
        "/v2/agents", json=BODY, headers={"Idempotency-Key": "k-1"}
    )
    assert again.status_code == 201
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replay"] == "true"
    assert "Idempotent-Replay" not in first.headers
    assert len(agents_named(env["url"], "idem-bot")) == 1


def test_the_same_key_with_another_body_is_422(env):
    admin = bearer("acme:admin")
    assert (
        admin.post(
            "/v2/agents", json=BODY, headers={"Idempotency-Key": "k-2"}
        ).status_code
        == 201
    )
    r = admin.post(
        "/v2/agents",
        json={**BODY, "name": "other-bot"},
        headers={"Idempotency-Key": "k-2"},
    )
    assert r.status_code == 422
    assert agents_named(env["url"], "other-bot") == []


def test_a_retry_while_the_first_is_running_is_409(env):
    with tenant_session(ACME, env["url"]) as s:
        s.add(
            models.ApiIdempotency(
                tenant_id=ACME,
                principal="u-acme:admin",
                key="k-3",
                request_hash="x" * 64,
                status="in_progress",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    r = bearer("acme:admin").post(
        "/v2/agents", json=BODY, headers={"Idempotency-Key": "k-3"}
    )
    assert r.status_code == 409
    assert agents_named(env["url"], "idem-bot") == []


def test_a_failed_first_attempt_leaves_the_key_free(env):
    admin = bearer("acme:admin")
    bad = admin.post(
        "/v2/agents",
        json={**BODY, "owner": "not-an-email"},
        headers={"Idempotency-Key": "k-4"},
    )
    assert bad.status_code == 400
    good = admin.post(
        "/v2/agents", json=BODY, headers={"Idempotency-Key": "k-4"}
    )
    assert good.status_code == 201, good.text


def test_an_expired_key_runs_again(env):
    admin = bearer("acme:admin")
    admin.post("/v2/agents", json=BODY, headers={"Idempotency-Key": "k-5"})
    with tenant_session(ACME, env["url"]) as s:
        for row in s.scalars(select(models.ApiIdempotency)):
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    r = admin.post(
        "/v2/agents",
        json={**BODY, "name": "idem-bot-2"},
        headers={"Idempotency-Key": "k-5"},
    )
    assert r.status_code == 201, r.text


def test_keys_are_scoped_to_the_caller_and_the_tenant(env):
    first = bearer("acme:admin").post(
        "/v2/agents", json=BODY, headers={"Idempotency-Key": "shared"}
    )
    assert first.status_code == 201
    other = bearer("globex:admin").post(
        "/v2/agents", json=BODY, headers={"Idempotency-Key": "shared"}
    )
    assert other.status_code == 201
    assert other.json()["id"] != first.json()["id"]
    assert "Idempotent-Replay" not in other.headers


@pytest.mark.parametrize("key", ["", "x" * 129, "bad\nkey"])
def test_a_malformed_key_is_400(env, key):
    r = bearer("acme:admin").post(
        "/v2/agents", json=BODY, headers={"Idempotency-Key": key}
    )
    assert r.status_code == 400
    assert agents_named(env["url"], "idem-bot") == []


def test_no_key_changes_nothing(env):
    admin = bearer("acme:admin")
    assert admin.post("/v2/agents", json=BODY).status_code == 201
    assert admin.post("/v2/agents", json=BODY).status_code == 201
    assert len(agents_named(env["url"], "idem-bot")) == 2


def test_expired_keys_and_old_windows_are_purged(env):
    now = datetime.now(UTC)
    with tenant_session(ACME, env["url"]) as s:
        for key, expires in (
            ("old", now - timedelta(seconds=1)),
            ("live", now + timedelta(hours=1)),
        ):
            s.add(models.ApiIdempotency(
                tenant_id=ACME, principal="p", key=key, request_hash="h" * 64,
                status="done", expires_at=expires,
            ))  # fmt: skip
        for start in (
            now - timedelta(days=2),
            now.replace(second=0, microsecond=0),
        ):
            s.add(models.ApiRateWindow(
                tenant_id=ACME, principal="p", window_start=start, count=1,
            ))  # fmt: skip
    assert v2_guard.purge_expired(ACME) == 2
    with tenant_session(ACME, env["url"]) as s:
        assert [r.key for r in s.scalars(select(models.ApiIdempotency))] == [
            "live"
        ]
        assert len(s.scalars(select(models.ApiRateWindow)).all()) == 1
