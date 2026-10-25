"""Webhook delivery from the outbox (PRD §9): signed, backed off, dead-lettered."""

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import requests
from fresh_schema import fresh_schema
from sqlalchemy import select

from invoice_processing.adapters import JsonlAuditSink
from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.findings.model import Citation, FindingSpec
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.webhooks import deliver, outbox, target

ACME, GLOBEX = "tenant-acme", "tenant-globex"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
SECRET = "whsec_test"


class Response:
    def __init__(self, status):
        self.status_code = status


class FakePost:
    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.calls = []

    def __call__(self, url, *, data, headers, timeout):
        self.calls.append({"url": url, "data": data, "headers": headers})
        assert timeout == 10
        status = self.statuses.pop(0) if self.statuses else 200
        if isinstance(status, Exception):
            raise status
        return Response(status)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    # Every test host resolves outward unless a test says otherwise.
    monkeypatch.setattr(target, "addresses", lambda host: ["93.184.216.34"])


@pytest.fixture
def env(scratch_db, tmp_path):
    url = scratch_db("deliver.db")
    fresh_schema(url)
    for tid, slug in ((ACME, "acme"), (GLOBEX, "globex")):
        with tenant_session(tid, url) as s:
            s.add(models.Tenant(id=tid, name=slug, slug=slug, status="active"))
    path = tmp_path / "audit.jsonl"
    return url, JsonlAuditSink(path), path


def endpoint(url, tenant=ACME, events=("*",), active=True, target=None):
    with tenant_session(tenant, url) as s:
        ep = models.WebhookEndpoint(
            tenant_id=tenant,
            url=target or f"https://{tenant}.example/hook",
            secret=SECRET,
            events=list(events),
            active=active,
        )
        s.add(ep)
        s.flush()
        return ep.id


def emit(url, tenant=ACME, event="ping", data=None, now=NOW):
    with tenant_session(tenant, url) as s:
        return outbox.emit(s, tenant, event, data or {}, now=now)


def rows(url, tenant=ACME):
    with tenant_session(tenant, url) as s:
        return s.scalars(
            select(models.WebhookOutbox).order_by(
                models.WebhookOutbox.created_at
            )
        ).all()


def test_a_due_row_is_sent_signed_and_marked_delivered(env):
    url, audit, _ = env
    endpoint(url)
    emit(url, event="ping")
    post = FakePost(204)
    assert deliver.due(NOW, post=post, audit=audit, url=url) == 1
    (call,) = post.calls
    h = call["headers"]
    row = rows(url)[0]
    assert call["url"] == f"https://{ACME}.example/hook"
    assert h["X-Trust-Event"] == "ping"
    assert h["X-Trust-Delivery"] == row.id
    assert h["Content-Type"] == "application/json"
    ts = h["X-Trust-Timestamp"]
    expected = hmac.new(
        SECRET.encode(), f"{ts}.{call['data']}".encode(), hashlib.sha256
    ).hexdigest()
    assert h["X-Trust-Signature"] == f"sha256={expected}"
    assert json.loads(call["data"])["event"] == "ping"
    assert row.delivered_at is not None and row.last_status == 204
    # Delivered: never sent again.
    assert deliver.due(NOW + timedelta(hours=1), post=post, url=url) == 0


def test_a_failure_backs_off_exponentially(env):
    url, audit, _ = env
    endpoint(url)
    emit(url)
    post = FakePost(500, 503)
    assert deliver.due(NOW, post=post, audit=audit, url=url) == 1
    row = rows(url)[0]
    assert row.attempts == 1 and row.last_status == 500
    assert row.delivered_at is None
    assert _aware(row.next_attempt_at) == NOW + timedelta(minutes=2)
    # Not due yet a minute later; due at two.
    assert deliver.due(NOW + timedelta(minutes=1), post=post, url=url) == 0
    assert deliver.due(NOW + timedelta(minutes=2), post=post, url=url) == 1
    row = rows(url)[0]
    assert row.attempts == 2
    assert _aware(row.next_attempt_at) == NOW + timedelta(minutes=6)


def test_the_backoff_is_capped_at_64_minutes(env):
    url, audit, _ = env
    endpoint(url)
    emit(url)
    with tenant_session(ACME, url) as s:
        s.scalars(select(models.WebhookOutbox)).one().attempts = 6
    deliver.due(NOW, post=FakePost(500), audit=audit, url=url)
    row = rows(url)[0]
    assert row.attempts == 7
    assert _aware(row.next_attempt_at) == NOW + timedelta(minutes=64)


def test_the_eighth_failure_dead_letters_and_audits(env):
    url, audit, path = env
    endpoint(url)
    emit(url)
    with tenant_session(ACME, url) as s:
        s.scalars(select(models.WebhookOutbox)).one().attempts = 7
    deliver.due(NOW, post=FakePost(410), audit=audit, url=url)
    row = rows(url)[0]
    assert row.attempts == 8 and row.dead_at is not None
    (event,) = [json.loads(line) for line in path.read_text().splitlines()]
    assert event["event_type"] == "webhook_dead_lettered"
    assert event["tenant_id"] == ACME
    assert event["payload"] == {
        "event": "ping",
        "attempts": 8,
        "last_status": 410,
    }
    assert deliver.due(NOW + timedelta(days=1), post=FakePost(), url=url) == 0


def test_a_raising_post_is_a_failed_attempt_with_a_masked_error(env):
    url, audit, _ = env
    endpoint(url)
    endpoint(url, target="https://ok.example/h")
    emit(url)  # one row for each endpoint
    boom = requests.ConnectionError(
        "refused for account 123456789012 " + "x" * 600
    )
    post = FakePost(boom, 200)
    assert deliver.due(NOW, post=post, audit=audit, url=url) == 2
    failed, sent = sorted(rows(url), key=lambda r: r.delivered_at is not None)
    assert failed.attempts == 1 and failed.last_status is None
    assert "123456789012" not in failed.last_error
    assert len(failed.last_error) <= 500
    assert sent.delivered_at is not None


def test_only_due_live_rows_of_active_endpoints_are_sent_up_to_the_limit(env):
    url, audit, _ = env
    endpoint(url)
    endpoint(url, active=False, target="https://off.example/h")
    for _ in range(3):
        emit(url)  # 3 rows for the active endpoint only
    emit(url, now=NOW + timedelta(minutes=5))  # not yet due
    post = FakePost()
    assert deliver.due(NOW, post=post, limit=2, audit=audit, url=url) == 2
    assert deliver.due(NOW, post=post, limit=2, audit=audit, url=url) == 1
    assert {c["url"] for c in post.calls} == {f"https://{ACME}.example/hook"}


def test_a_paused_endpoints_queued_rows_wait(env):
    url, audit, _ = env
    ep = endpoint(url)
    emit(url)
    with tenant_session(ACME, url) as s:
        s.get(models.WebhookEndpoint, ep).active = False
    assert deliver.due(NOW, post=FakePost(), audit=audit, url=url) == 0


def test_every_active_tenant_is_swept_and_a_suspended_one_waits(env):
    url, audit, _ = env
    endpoint(url, ACME)
    endpoint(url, GLOBEX)
    emit(url, ACME)
    emit(url, GLOBEX)
    with tenant_session(GLOBEX, url) as s:
        s.get(models.Tenant, GLOBEX).status = "suspended"
    post = FakePost()
    assert deliver.due(NOW, post=post, audit=audit, url=url) == 1
    assert post.calls[0]["url"].startswith(f"https://{ACME}")


def test_one_tenant_failing_does_not_stop_the_next(env, monkeypatch):
    url, audit, _ = env
    endpoint(url, ACME)
    endpoint(url, GLOBEX)
    emit(url, ACME)
    emit(url, GLOBEX)
    real = deliver._sweep_tenant

    def flaky(tenant_id, *a, **kw):
        if tenant_id == ACME:
            raise RuntimeError("db hiccup")
        return real(tenant_id, *a, **kw)

    monkeypatch.setattr(deliver, "_sweep_tenant", flaky)
    assert deliver.due(NOW, post=FakePost(), audit=audit, url=url) == 1


def test_a_finding_reaches_a_subscribed_endpoint_end_to_end(env):
    url, _audit, _ = env
    endpoint(url, events=("finding.created",))
    ctx = RunContext(tenant_id=ACME, case_id="", run_id="r", actor="engine")
    node = GraphStore(url).upsert_node(ctx, kind="invoice", natural_key="i1")
    FindingStore(url).upsert(
        ctx,
        FindingSpec(
            type="BENCH-IMPORT",
            transaction_node=node,
            dedupe_key="k",
            impact=Decimal("10.00"),
            currency="INR",
            confidence=0.9,
            engine_version="1",
            rule_version="r",
            model_version="m",
            summary="s",
            citations=(Citation(node),),
        ),
    )
    post = FakePost()
    assert deliver.due(datetime.now(UTC), post=post, url=url) == 1
    body = json.loads(post.calls[0]["data"])
    assert body["event"] == "finding.created"
    assert body["tenant"] == ACME
    assert body["data"]["transaction_node"] == node


def test_sign_matches_the_documented_recipe():
    sig = deliver.sign("k", "1700000000", '{"a":1}')
    want = hmac.new(b"k", b'1700000000.{"a":1}', hashlib.sha256).hexdigest()
    assert sig == f"sha256={want}"


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def test_a_redirect_is_a_failed_attempt(env):
    url, audit, _ = env
    endpoint(url)
    emit(url)
    deliver.due(NOW, post=FakePost(307), audit=audit, url=url)
    row = rows(url)[0]
    assert row.delivered_at is None and row.attempts == 1
    assert row.last_status == 307


def test_each_post_is_signed_with_the_time_it_is_sent(env):
    url, audit, _ = env
    endpoint(url)
    emit(url, now=NOW - timedelta(hours=2))
    post = FakePost()
    deliver.due(NOW - timedelta(hours=1), post=post, audit=audit, url=url)
    ts = int(post.calls[0]["headers"]["X-Trust-Timestamp"])
    assert abs(ts - time.time()) < 60


def test_an_endpoint_that_times_out_is_skipped_for_the_rest_of_the_sweep(env):
    url, audit, _ = env
    endpoint(url)
    endpoint(url, target="https://ok.example/h")
    for _ in range(3):
        emit(url)  # three rows for each endpoint
    calls = []

    def post(target, **kw):
        calls.append(target)
        if "acme" in target:
            raise requests.Timeout("slow")
        return Response(200)

    assert deliver.due(NOW, post=post, audit=audit, url=url) == 4
    assert calls.count(f"https://{ACME}.example/hook") == 1
    assert calls.count("https://ok.example/h") == 3


def test_one_tenants_backlog_does_not_starve_the_next(env):
    url, audit, _ = env
    endpoint(url, ACME)
    endpoint(url, GLOBEX)
    for _ in range(deliver.PER_TENANT + 5):
        emit(url, ACME)
    emit(url, GLOBEX)
    post = FakePost()
    deliver.due(NOW, post=post, audit=audit, url=url)
    assert any(c["url"].startswith(f"https://{GLOBEX}") for c in post.calls)


def test_a_row_deleted_mid_sweep_is_skipped(env):
    url, audit, _ = env
    endpoint(url)
    emit(url)
    emit(url)

    def post(target, **kw):
        with tenant_session(ACME, url) as s:
            for r in s.scalars(select(models.WebhookOutbox)):
                s.delete(r)
        return Response(200)

    assert deliver.due(NOW, post=post, audit=audit, url=url) == 2


def test_a_host_resolving_inward_is_a_failed_attempt_and_never_posted(
    env, monkeypatch
):
    url, audit, _ = env
    endpoint(url, target="https://rebound.example/h")
    endpoint(url, target="https://ok.example/h")
    emit(url)
    emit(url)
    monkeypatch.setattr(
        target,
        "addresses",
        lambda host: ["10.0.0.7"] if host == "rebound.example" else ["8.8.8.8"],
    )
    post = FakePost()
    deliver.due(NOW, post=post, audit=audit, url=url)
    assert {c["url"] for c in post.calls} == {"https://ok.example/h"}
    bad = [r for r in rows(url) if r.delivered_at is None]
    tried = [r for r in bad if r.attempts]
    assert tried[0].last_error == "The url is internal or ambiguous; not sent."
    # Its second row waits for the next sweep, like an unreachable host's.
    assert sorted(r.attempts for r in bad) == [0, 1]


def test_a_backslash_url_saved_before_the_check_is_never_posted(env):
    url, audit, _ = env
    endpoint(url, target="https://10.0.0.1\\@example.com/")
    emit(url)
    post = FakePost()
    deliver.due(NOW, post=post, audit=audit, url=url)
    assert post.calls == []
    assert rows(url)[0].attempts == 1


def test_deliveries_go_out_through_the_pinned_send(env, monkeypatch):
    """No second DNS lookup between the check and the connection."""
    url, audit, _ = env
    endpoint(url)
    emit(url)
    opened = []

    class Pool:
        def urlopen(self, method, path, **kw):
            assert kw["redirect"] is False  # a 3xx is a failed attempt
            return type("R", (), {"status": 204})()

    def pool(scheme, address, port, name, timeout):
        opened.append((scheme, address, name))
        return Pool()

    monkeypatch.setattr(target, "_pool", pool)
    assert deliver.due(NOW, audit=audit, url=url) == 1
    assert opened == [("https", "93.184.216.34", "tenant-acme.example")]
    assert rows(url)[0].delivered_at is not None


def test_a_name_that_rebinds_at_send_time_is_not_sent(env):
    """The check passed; the send's own lookup answered inward."""
    url, audit, _ = env
    endpoint(url)
    emit(url)

    def post(target_url, **kw):
        raise target.Refused(target_url)

    assert deliver.due(NOW, post=post, audit=audit, url=url) == 1
    (row,) = rows(url)
    assert row.delivered_at is None
    assert row.last_error == "The url is internal or ambiguous; not sent."
