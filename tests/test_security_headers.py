"""Every response says what a browser may do with it."""

import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from invoice_processing.api import auth
from invoice_processing.api.service import app

CLAIMS = {
    "sub": "u-1",
    "email": "clerk@acme.test",
    "email_verified": True,
    "tenant_id": "tenant-acme",
    "role": "clerk",
}


class FakeVerifier:
    def verify(self, token):
        if token != "tok":
            raise auth.AuthError("Invalid token")
        return CLAIMS


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    return TestClient(app, headers={"Authorization": "Bearer tok"})


FIXED = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


class TestFixedHeaders:
    def test_json_responses_carry_them(self, client):
        headers = client.get("/api/me").headers
        for key, value in FIXED.items():
            assert headers[key] == value, key

    def test_an_error_response_carries_them_too(self, client):
        headers = TestClient(app).get("/api/me").headers  # 401
        for key, value in FIXED.items():
            assert headers[key] == value, key
        assert headers["cache-control"] == "no-store"

    def test_html_carries_them_and_a_csp(self, client):
        headers = client.get("/").headers
        for key, value in FIXED.items():
            assert headers[key] == value, key
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert "cdn.jsdelivr.net" in headers["content-security-policy"]


class TestCaching:
    def test_api_json_is_never_stored(self, client):
        assert client.get("/api/me").headers["cache-control"] == "no-store"

    def test_html_is_not_marked_no_store(self, client):
        assert "no-store" not in client.get("/").headers.get(
            "cache-control", ""
        )


class TestHsts:
    def test_only_where_tls_is_real(self, client, monkeypatch):
        monkeypatch.setenv("ENV", "local")
        assert "strict-transport-security" not in client.get("/api/me").headers
        monkeypatch.setenv("ENV", "production")
        assert (
            client.get("/api/me").headers["strict-transport-security"]
            == "max-age=31536000; includeSubDomains"
        )


class TestDocs:
    def test_served_locally(self, client):
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200

    def test_absent_when_deployed(self):
        """ENV is read when the app module is imported, so prove it in a
        process of its own."""
        code = (
            "from fastapi.testclient import TestClient\n"
            "from invoice_processing.api.service import app\n"
            "c = TestClient(app)\n"
            "print(c.get('/docs').status_code,"
            " c.get('/openapi.json').status_code,"
            " c.get('/redoc').status_code)\n"
        )
        env = {
            **os.environ,
            "ENV": "production",
            "DATABASE_URL": "sqlite://",
            "CLOUD_SQL_INSTANCE": "",
            "GCS_BUCKET": "",
            "PUBSUB_TOPIC": "",
        }
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=env,
        )
        assert out.returncode == 0, out.stderr[-2000:]
        assert out.stdout.strip().splitlines()[-1] == "404 404 404"
