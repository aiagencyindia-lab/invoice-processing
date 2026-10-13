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


class TestACrash:
    def test_a_server_error_still_carries_the_headers(self, monkeypatch):
        """An exception escaped the middleware, so Starlette's 500 went out
        with none of them, not even the request id."""
        from invoice_processing.api import service  # noqa: PLC0415

        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        monkeypatch.setattr(auth, "_verifier", FakeVerifier())

        def boom():
            raise RuntimeError("store down")

        monkeypatch.setattr(service, "get_cases", boom)
        response = TestClient(
            app,
            headers={"Authorization": "Bearer tok"},
            raise_server_exceptions=False,
        ).get("/api/invoices")
        assert response.status_code == 500
        for key, value in FIXED.items():
            assert response.headers[key] == value, key
        assert response.headers["x-request-id"]
        assert response.headers["cache-control"] == "no-store"


class TestTheRequestLine:
    def _get(self, client, caplog, path="/api/me", **headers):
        with caplog.at_level("INFO", logger="invoice_processing.api.service"):
            response = client.get(path, headers=headers)
        records = [r for r in caplog.records if r.getMessage() == "request"]
        return response, records

    def test_one_line_per_request_with_the_id(self, client, caplog):
        response, records = self._get(client, caplog)
        [record] = records
        assert record.method == "GET" and record.route == "/api/me"
        assert record.status == 200 and record.duration_ms >= 0
        assert record.request_id == response.headers["x-request-id"]

    def test_a_valid_inbound_id_is_kept(self, client, caplog):
        response, [record] = self._get(
            client, caplog, **{"X-Request-Id": "abc-123_XYZ"}
        )
        assert response.headers["x-request-id"] == "abc-123_XYZ"
        assert record.request_id == "abc-123_XYZ"

    @pytest.mark.parametrize("bad", ["x" * 65, "has space", "a%2Fb", "a;b", ""])
    def test_a_bad_inbound_id_is_replaced(self, client, caplog, bad):
        response, [record] = self._get(client, caplog, **{"X-Request-Id": bad})
        issued = response.headers["x-request-id"]
        assert (
            issued != bad and len(issued) == 32 and record.request_id == issued
        )

    def test_the_tenant_is_on_the_line(self, client, caplog):
        _, [record] = self._get(client, caplog)
        assert record.tenant_id == CLAIMS["tenant_id"]

    def test_the_tenant_does_not_leak_to_the_next_request(self, client, caplog):
        self._get(client, caplog)
        with caplog.at_level("INFO", logger="invoice_processing.api.service"):
            response = TestClient(app).get("/api/me")  # no token: 401
        record = [r for r in caplog.records if r.getMessage() == "request"][-1]
        assert response.status_code == 401
        assert record.status == 401 and record.tenant_id == ""

    def test_the_crash_line_carries_the_id(self, monkeypatch, caplog):
        from invoice_processing.api import service  # noqa: PLC0415

        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        monkeypatch.setattr(auth, "_verifier", FakeVerifier())
        monkeypatch.setattr(
            service,
            "get_cases",
            lambda: (_ for _ in ()).throw(RuntimeError("down")),
        )
        with caplog.at_level("INFO", logger="invoice_processing.api.service"):
            response = TestClient(
                app,
                headers={"Authorization": "Bearer tok"},
                raise_server_exceptions=False,
            ).get("/api/invoices")
        crash = [
            r
            for r in caplog.records
            if r.getMessage().startswith("Unhandled error")
        ]
        [line] = [r for r in caplog.records if r.getMessage() == "request"]
        assert response.status_code == 500
        assert crash and crash[0].request_id == response.headers["x-request-id"]
        assert line.status == 500
