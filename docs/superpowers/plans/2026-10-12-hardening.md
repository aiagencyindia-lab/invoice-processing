# Application Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bank account numbers leave the API only for approvers and admins, every response carries security headers (CSP report-only on the app), the API docs are off when deployed, and `.env` no longer reaches Railway's builder.

**Architecture:** One pure `masking.py` helper replaces the vendor routes' private `mask()` and is applied at the three serialisation points that still leaked numbers (invoice detail, sheet rows, exports — the export job carries a `mask_bank` flag because its runner has no principal). Headers are added in the API's existing `_identify_request` middleware and in `next.config.ts headers()`; the docs flag is computed at import from `preflight.is_deployed()`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, pytest; Next.js 16, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-12-hardening-design.md`

## Global Constraints

- Always `./.venv/bin/python`; run from `/Users/vineet/Downloads/ADK/invoice-processing`; tests with `-q -p no:cacheprovider`.
- Full bank numbers only when `principal.can("approver")`; everyone else sees `"…"` + last four (`MASK_PREFIX = "…"`, U+2026). Short values always get the prefix.
- `BANK_FIELDS = ("bank_account_number", "bank_iban")`.
- PATCH refusal text: `"That looks like a masked value; enter the full number."` (422).
- Export job spec key `mask_bank`; a missing key masks (`spec.get("mask_bank", True)`).
- Fixed headers, exact values: `X-Content-Type-Options: nosniff`; `X-Frame-Options: DENY`; `Referrer-Policy: strict-origin-when-cross-origin`; `Permissions-Policy: camera=(), microphone=(), geolocation=()`; HSTS `max-age=31536000; includeSubDomains` only when deployed.
- `Cache-Control: no-store` only on `/api/*` responses whose content-type starts with `application/json` and that have no `Content-Disposition`.
- The app's CSP header is `Content-Security-Policy-Report-Only`; the API's HTML CSP is enforced. Policies verbatim from the spec.
- `/docs`, `/redoc`, `/openapi.json` exist locally and are absent when `ENV` is in `preflight.DEPLOYED_ENVIRONMENTS`.
- Comments explain why; ruff check + format clean; commit subjects imperative and say why; trailer `Co-Authored-By: Claude <model that wrote it> <noreply@anthropic.com>`.
- `tests/test_frontend_reaches_the_flow.py` greps frontend source for exact strings: run the Python suite after any frontend change.

## Review Focus

1. A bank field whose value is not a string (a number from extraction) must still mask, not raise — test in Task 1.
2. An approver's export of a *legacy* job (no `mask_bank`) masks; a new approver job does not — test in Task 2.
3. The invoice PDF and the export download must not carry `no-store` (Safari "Save as" needs caching) — tests in Tasks 2 and 3.
4. A JSON error response (401/404) on `/api/*` still gets `no-store` and the fixed headers — test in Task 3.
5. `headers()` in Next must not break the `/api` rewrite: the e2e suite's existing API-backed tests keep passing after Task 4 — the full e2e run in Task 4 pins it.

---

### Task 1: Masking helper, vendor routes reuse it, invoice detail and field edits

**Files:**
- Create: `invoice_processing/masking.py`, `tests/test_masking.py`
- Modify: `invoice_processing/api/vendor_routes.py:57-63` (`mask`), `invoice_processing/api/service.py` (`get_invoice` ~885-940, `edit_fields` ~997-1012)
- Test: `tests/test_workbench_api.py`

**Interfaces:**
- Produces: `masking.BANK_FIELDS: tuple[str, ...]`, `masking.MASK_PREFIX: str`, `masking.mask_bank(value) -> str | None`, `masking.mask_row(row: dict) -> dict` (copy; input not mutated).

- [ ] **Step 1: Failing helper tests** — `tests/test_masking.py`:

```python
"""Bank numbers leave the API as the last four digits unless the caller may
verify a payment. One helper, so every surface masks the same way."""

from invoice_processing.masking import BANK_FIELDS, mask_bank, mask_row


def test_last_four_with_a_prefix():
    assert mask_bank("901031925572") == "…5572"


def test_spaces_do_not_count():
    assert mask_bank("12 34 56") == "…3456"


def test_short_values_are_never_returned_verbatim():
    """Four characters or fewer used to come back in full."""
    assert mask_bank("1234") == "…1234"
    assert mask_bank("12") == "…12"


def test_numbers_mask_too():
    assert mask_bank(123456) == "…3456"


def test_empty_is_none():
    assert mask_bank("") is None
    assert mask_bank(None) is None


def test_mask_row_touches_only_bank_fields_and_copies():
    row = {"vendor_name": "Acme", "bank_account_number": "901031925572", "bank_iban": "GB29NWBK60161331926819"}
    masked = mask_row(row)
    assert masked == {"vendor_name": "Acme", "bank_account_number": "…5572", "bank_iban": "…6819"}
    assert row["bank_account_number"] == "901031925572"
    assert set(BANK_FIELDS) == {"bank_account_number", "bank_iban"}
```

Run: `./.venv/bin/python -m pytest tests/test_masking.py -q -p no:cacheprovider` — Expected: FAIL, `ModuleNotFoundError: invoice_processing.masking`.

- [ ] **Step 2: Implement `invoice_processing/masking.py`**:

```python
"""Bank numbers as the API shows them to someone who may not pay anyone.

The last four digits are enough to recognise an account and not enough to
pay into it. One helper, used by every surface that serialises a bank
number, so the vendor queue, the invoice detail, the sheet and the exports
cannot drift apart.
"""

from __future__ import annotations

BANK_FIELDS = ("bank_account_number", "bank_iban")
MASK_PREFIX = "…"


def mask_bank(value) -> str | None:
    if value is None or value == "":
        return None
    digits = str(value).replace(" ", "")
    # Always prefixed: a short value returned verbatim both leaks it and
    # makes "is this masked?" unanswerable downstream.
    return f"{MASK_PREFIX}{digits[-4:]}"


def mask_row(row: dict) -> dict:
    masked = dict(row)
    for field in BANK_FIELDS:
        if field in masked:
            masked[field] = mask_bank(masked[field])
    return masked
```

Run Step 1 — Expected: PASS.

- [ ] **Step 3: Vendor routes reuse it.** Replace the `mask` function body at `vendor_routes.py:57-63` with an import at the top of the module: `from ..masking import mask_bank as mask` (keep the name; five call sites unchanged). Delete the local definition.

Run: `./.venv/bin/python -m pytest tests/test_vendor_routes.py -q -p no:cacheprovider` — Expected: PASS (the `…5572` assertions hold).

- [ ] **Step 4: Failing invoice-detail tests** — add to `tests/test_workbench_api.py`. The existing `client` fixture is admin-only; add a role-aware one beside it:

```python
ROLE_CLAIMS = {
    f"tok-{role}": {**ADMIN_CLAIMS, "sub": f"u-{role}", "email": f"{role}@acme.com", "role": role}
    for role in ("viewer", "clerk", "approver", "admin")
}


@pytest.fixture
def roles(client, monkeypatch):
    """The same isolated stack as `client`, with a token per role."""
    monkeypatch.setattr(auth, "_verifier", FakeVerifier({"tok": ADMIN_CLAIMS, **ROLE_CLAIMS}))

    def make(role):
        return TestClient(app, headers={"Authorization": f"Bearer tok-{role}"})

    return make


class TestBankNumbersAreMaskedByRole:
    NUMBER = "901031925572"

    def _seed(self, roles):
        r = roles("admin").patch(
            "/api/invoices/case_002/fields",
            json={"edits": [{"field": "bank_account_number", "value": self.NUMBER}]},
        )
        assert r.status_code == 200, r.text

    @pytest.mark.parametrize("role", ["viewer", "clerk"])
    def test_a_non_approver_sees_the_last_four(self, roles, role):
        self._seed(roles)
        assert _field(roles(role), "case_002", "bank_account_number") == "…5572"

    @pytest.mark.parametrize("role", ["approver", "admin"])
    def test_an_approver_sees_the_number(self, roles, role):
        self._seed(roles)
        assert _field(roles(role), "case_002", "bank_account_number") == self.NUMBER

    def test_a_masked_value_cannot_be_saved_back(self, roles):
        """The editor sends only changed fields, but a person can still type
        the mask; refusing it keeps a placeholder out of the record."""
        self._seed(roles)
        r = roles("clerk").patch(
            "/api/invoices/case_002/fields",
            json={"edits": [{"field": "bank_account_number", "value": "…5572"}]},
        )
        assert r.status_code == 422
        assert "masked" in r.json()["detail"]
        assert _field(roles("admin"), "case_002", "bank_account_number") == self.NUMBER

    def test_a_real_number_still_saves(self, roles):
        r = roles("clerk").patch(
            "/api/invoices/case_002/fields",
            json={"edits": [{"field": "bank_account_number", "value": "123456789"}]},
        )
        assert r.status_code == 200, r.text
```

Run: `./.venv/bin/python -m pytest tests/test_workbench_api.py -q -p no:cacheprovider -k MaskedByRole` — Expected: the two non-approver tests and the masked-save test FAIL; the others pass. (If `case_002` is a decided invoice and the PATCH returns 409 because `bank_account_number` is in `ROUTING_FIELDS`, pick another golden case id from `client.get("/api/invoices")` that is undecided, and say so in the report.)

- [ ] **Step 5: Implement.** In `service.py` add `from ..masking import BANK_FIELDS, MASK_PREFIX, mask_bank` to the `from ..` import block. In `get_invoice`, after `fields.sort(...)`:

```python
    if not principal.can("approver"):
        # Reading an invoice is not paying one: the full number is for the
        # people who verify a vendor's bank change, and the PDF shows it anyway.
        for field in fields:
            if field["name"] in BANK_FIELDS:
                field["value"] = mask_bank(field["value"])
```

In `edit_fields`, inside the first `for edit in request.edits:` loop, after the `line_items` check:

```python
        if edit.field in BANK_FIELDS and str(edit.value or "").startswith(MASK_PREFIX):
            raise HTTPException(
                status_code=422,
                detail="That looks like a masked value; enter the full number.",
            )
```

Run Step 4 — Expected: PASS. Then `./.venv/bin/python -m pytest tests/test_workbench_api.py tests/test_vendor_routes.py tests/test_masking.py -q -p no:cacheprovider` — Expected: PASS.

- [ ] **Step 6: Lint and commit.** `ruff format invoice_processing/masking.py invoice_processing/api/vendor_routes.py invoice_processing/api/service.py tests/test_masking.py tests/test_workbench_api.py`; `ruff check . && ruff format --check .` clean.

```bash
git add invoice_processing/masking.py invoice_processing/api/vendor_routes.py invoice_processing/api/service.py tests/test_masking.py tests/test_workbench_api.py
git commit -m "Show full bank numbers only to those who verify payments

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```

---

### Task 2: Sheet rows and exports mask by role

**Files:**
- Modify: `invoice_processing/api/sheet.py` (`list_sheet` ~151-166, `create_export` ~500-515, `download_export` test only), `invoice_processing/exports.py` (`run` ~218-244)
- Test: `tests/test_sheet_api.py`, `tests/test_exports.py`

**Interfaces:**
- Consumes: `masking.mask_row(row) -> dict` from Task 1.
- Produces: export job `spec["mask_bank"]: bool`.

- [ ] **Step 1: Failing sheet tests** — add to `tests/test_sheet_api.py` (the `client(role)` fixture and `invoice(session, **overrides)` helper exist; `rows(client, **params)` returns the JSON):

```python
class TestBankNumbersMaskByRole:
    def test_a_clerk_sees_the_last_four(self, db, client):
        with tenant_session(TENANT, db) as session:
            invoice(session, case_id="bank", bank_account_number="901031925572")
        row = rows(lambda: client("clerk"))["rows"][0]
        assert row["bank_account_number"] == "…5572"

    def test_an_approver_sees_the_number(self, db, client):
        with tenant_session(TENANT, db) as session:
            invoice(session, case_id="bank", bank_account_number="901031925572")
        row = rows(lambda: client("approver"))["rows"][0]
        assert row["bank_account_number"] == "901031925572"
```

Run: `./.venv/bin/python -m pytest tests/test_sheet_api.py -q -p no:cacheprovider -k MaskByRole` — Expected: the clerk test FAILS.

- [ ] **Step 2: Implement in `list_sheet`.** Import `from ..masking import mask_row`. After `rows, total = _repository().page(...)`:

```python
    if not principal.can("approver"):
        # The repository has no caller; the role is known only here.
        rows = [mask_row(row) for row in rows]
```

Run Step 1 — Expected: PASS.

- [ ] **Step 3: Failing export tests** — add to `tests/test_exports.py` (fixtures `db`, `client(role)`, `blobs.stored`, `seed(db, count, **overrides)`, `export(client, **body)` exist; `exports` and `PgExportJobs` importable):

```python
def _csv_cells(blobs, job):
    raw = blobs.stored[job["storage_key"]].decode("utf-8-sig")
    return list(csv.reader(io.StringIO(raw)))[1:]


class TestBankNumbersMaskByRole:
    NUMBER = "901031925572"

    def test_a_clerk_export_carries_the_last_four(self, db, client, blobs):
        seed(db, 1, bank_account_number=self.NUMBER)
        job = export(lambda: client("clerk"), format="csv", columns=["bank_account_number"])
        assert _csv_cells(blobs, job) == [["…5572"]]

    def test_an_approver_export_carries_the_number(self, db, client, blobs):
        seed(db, 1, bank_account_number=self.NUMBER)
        job = export(lambda: client("approver"), format="csv", columns=["bank_account_number"])
        assert _csv_cells(blobs, job) == [[self.NUMBER]]

    def test_a_job_without_the_flag_masks(self, db, client, blobs):
        """Jobs queued before masking existed say nothing about the caller;
        the safe reading is the restrictive one."""
        from invoice_processing import exports  # noqa: PLC0415
        from invoice_processing.db.repositories import PgExportJobs  # noqa: PLC0415

        seed(db, 1, bank_account_number=self.NUMBER)
        job = PgExportJobs().create(
            TENANT, requested_by="approver@acme.test", fmt="csv",
            spec={"columns": ["bank_account_number"], "filters": {}, "case_ids": []},
        )
        exports.run(job["id"], TENANT)
        done = PgExportJobs().get(TENANT, job["id"])
        assert _csv_cells(blobs, done) == [["…5572"]]

    def test_a_download_is_not_marked_no_store(self, db, client):
        seed(db, 1)
        job = export(client, format="csv")
        response = client().get(f"/api/sheet/exports/{job['id']}/download")
        assert "no-store" not in response.headers.get("cache-control", "")
```

Check `export()`'s `client` argument: it calls `client()` with no args, so passing `lambda: client("clerk")` works. Check `PgExportJobs.create` returns a dict with `"id"` (sheet.py uses `job["id"]`); if `spec["filters"]` must contain keys `_filters` produces, copy what `create_export` builds.

Run: `./.venv/bin/python -m pytest tests/test_exports.py -q -p no:cacheprovider -k MaskByRole` — Expected: the clerk and legacy tests FAIL; the approver and download tests pass (download passes now and must keep passing after Task 3).

- [ ] **Step 4: Implement.** In `sheet.py create_export`, add to `spec`: `"mask_bank": not principal.can("approver"),` with the comment `# The runner has no caller; carry the decision with the job.` In `exports.run`, after `rows = collect_rows(...)`:

```python
        if spec.get("mask_bank", True):
            # A job from before this flag existed names no caller: mask.
            rows = [mask_row(row) for row in rows]
```

with `from .masking import mask_row` at the top of `exports.py`. Run Step 3 — Expected: PASS. Then `./.venv/bin/python -m pytest tests/test_sheet_api.py tests/test_exports.py tests/test_tenant_isolation_api.py -q -p no:cacheprovider` — Expected: PASS.

- [ ] **Step 5: Lint and commit.** ruff format the touched files; `ruff check . && ruff format --check .` clean.

```bash
git add invoice_processing/api/sheet.py invoice_processing/exports.py tests/test_sheet_api.py tests/test_exports.py
git commit -m "Mask bank numbers in the sheet and in exports unless the caller may verify payments

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```

---

### Task 3: API security headers and docs off when deployed

**Files:**
- Modify: `invoice_processing/api/service.py:60-94` (app construction, `_identify_request`), `tests/test_tenant_isolation_api.py:1281-1289` (EXEMPT reasons)
- Create: `tests/test_security_headers.py`

**Interfaces:**
- Consumes: `preflight.is_deployed() -> bool`, `preflight.DEPLOYED_ENVIRONMENTS`.
- Produces: the header set every API response carries (Task 4's app headers mirror it).

- [ ] **Step 1: Failing tests** — `tests/test_security_headers.py`:

```python
"""Every response says what a browser may do with it."""

import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from invoice_processing.api import auth
from invoice_processing.api.service import app

CLAIMS = {
    "sub": "u-1", "email": "clerk@acme.test", "email_verified": True,
    "tenant_id": "tenant-acme", "role": "clerk",
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
        assert "no-store" not in client.get("/").headers.get("cache-control", "")


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
            "print(c.get('/docs').status_code, c.get('/openapi.json').status_code, c.get('/redoc').status_code)\n"
        )
        env = {**os.environ, "ENV": "production", "DATABASE_URL": "sqlite://", "CLOUD_SQL_INSTANCE": "", "GCS_BUCKET": "", "PUBSUB_TOPIC": ""}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False, env=env)
        assert out.returncode == 0, out.stderr[-2000:]
        assert out.stdout.strip() == "404 404 404"
```

Run: `./.venv/bin/python -m pytest tests/test_security_headers.py -q -p no:cacheprovider` — Expected: everything FAILS except `TestDocs::test_served_locally`.

- [ ] **Step 2: Implement.** In `service.py`, add `from . import preflight` next to `from . import auth`. Replace the `app = FastAPI(...)` line:

```python
# The schema pages describe every route to anyone who asks. Fine on a
# laptop; on a deployed host they are a map handed to a stranger. ENV is
# fixed for the life of a process, so this is decided once at import.
_SERVE_DOCS = not preflight.is_deployed()
app = FastAPI(
    title="Invoice Review Workbench",
    version="0.1.0",
    docs_url="/docs" if _SERVE_DOCS else None,
    redoc_url="/redoc" if _SERVE_DOCS else None,
    openapi_url="/openapi.json" if _SERVE_DOCS else None,
)

# What a browser may do with a response. The app in `frontend/` sets the
# same set on its own pages; the API repeats them so a direct call is no
# weaker than a proxied one.
_FIXED_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
# The API's own HTML is the landing page and the schema browser: static,
# styled inline, scripts from jsdelivr for Swagger and ReDoc.
_HTML_CSP = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "img-src 'self' data: https://fastapi.tiangolo.com; frame-ancestors 'none'; "
    "base-uri 'self'; form-action 'self'; object-src 'none'"
)
```

In `_identify_request`, after `response.headers["x-request-id"] = request_id`:

```python
    response.headers.update(_FIXED_HEADERS)
    if preflight.is_deployed():
        # Only where TLS is real: a browser that saw HSTS from a local http
        # host would refuse that host for a year.
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html"):
        response.headers["Content-Security-Policy"] = _HTML_CSP
    elif (
        request.url.path.startswith("/api/")
        and content_type.startswith("application/json")
        and "content-disposition" not in response.headers
    ):
        # Tenant data must not sit in a shared cache; downloads keep the
        # default so "Save as" works.
        response.headers["Cache-Control"] = "no-store"
```

Run Step 1 — Expected: PASS. Then `./.venv/bin/python -m pytest tests/test_exports.py -q -p no:cacheprovider -k no_store` (the download test from Task 2) — Expected: PASS.

- [ ] **Step 3: EXEMPT reasons.** In `tests/test_tenant_isolation_api.py` change the four docs entries' reason to `"schema only; absent when deployed"`. Run the file — Expected: PASS.

- [ ] **Step 4: Full suite, lint, commit.** `./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` — Expected: all pass (4 skipped). ruff clean.

```bash
git add invoice_processing/api/service.py tests/test_security_headers.py tests/test_tenant_isolation_api.py
git commit -m "Send security headers on every API response and hide the schema when deployed

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```

---

### Task 4: App headers with a report-only CSP, and `.railwayignore`

**Files:**
- Modify: `frontend/next.config.ts`, `.railwayignore`
- Test: `frontend/e2e/golden-path.spec.ts`

**Interfaces:**
- Consumes: the header set from Task 3 (same values).

- [ ] **Step 1: Failing e2e test** — append to `frontend/e2e/golden-path.spec.ts`:

```ts
test("every page carries security headers and a report-only CSP", async ({ page }) => {
  const headers = (await page.request.get("/login")).headers();
  expect(headers["x-frame-options"]).toBe("DENY");
  expect(headers["x-content-type-options"]).toBe("nosniff");
  expect(headers["referrer-policy"]).toBe("strict-origin-when-cross-origin");
  expect(headers["content-security-policy-report-only"]).toContain("identitytoolkit.googleapis.com");
  expect(headers["content-security-policy-report-only"]).toContain("frame-ancestors 'none'");
  // Report-only now: Google sign-in cannot be exercised here, so nothing is blocked yet.
  expect(headers["content-security-policy"]).toBeUndefined();
});
```

Run: `cd frontend && npm run test:e2e -- -g "security headers"` — Expected: FAIL (`x-frame-options` undefined).

- [ ] **Step 2: `next.config.ts`.** Add above `nextConfig`:

```ts
// Where Google sign-in goes: the Firebase auth domain, Google's token
// endpoints, and the popup/iframe hosts. Report-only until sign-in has been
// exercised against a live Identity Platform project; then enforce.
const AUTH = process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN || "*.firebaseapp.com";
const CSP = [
  "default-src 'self'",
  // Next's inline runtime carries no nonce, and the app styles inline.
  "script-src 'self' 'unsafe-inline' https://apis.google.com https://www.gstatic.com",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https://*.googleusercontent.com",
  "font-src 'self' data:",
  `connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com https://www.googleapis.com https://${AUTH} https://*.firebaseapp.com`,
  `frame-src https://${AUTH} https://*.firebaseapp.com https://accounts.google.com https://apis.google.com`,
  "object-src 'none'",
  "base-uri 'self'",
  "frame-ancestors 'none'",
  "form-action 'self' https://accounts.google.com",
].join("; ");

const SECURITY_HEADERS = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
  { key: "Content-Security-Policy-Report-Only", value: CSP },
  // Browsers ignore HSTS over plain http, so the e2e build is unaffected.
  ...(process.env.NODE_ENV === "production"
    ? [{ key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" }]
    : []),
];
```

and inside `nextConfig`, before `rewrites()`:

```ts
  // Applied to proxied /api responses too; the API sets the same values, and
  // browsers ignore CSP on anything that is not a document.
  async headers() {
    return [{ source: "/:path*", headers: SECURITY_HEADERS }];
  },
```

- [ ] **Step 3: `.railwayignore`.** Append:

```
# Secrets never reach the builder; the image reads them from Railway variables.
.env
frontend/.env.local
```

- [ ] **Step 4: Verify.** `cd frontend && npm run lint && npx tsc --noEmit && npm run test:e2e` — Expected: lint and tsc clean; e2e 38 passed (37 + 1), which also proves the `/api` rewrite still works under `headers()`. Then `./.venv/bin/python -m pytest tests/test_frontend_reaches_the_flow.py tests/test_gcs_and_migrations.py -q -p no:cacheprovider` — Expected: PASS (the latter has a test about `.env` never being committed; `.railwayignore` is not `.gitignore`, but run it).

- [ ] **Step 5: Commit.**

```bash
git add frontend/next.config.ts frontend/e2e/golden-path.spec.ts .railwayignore
git commit -m "Send security headers from the app, with a report-only CSP for Google sign-in

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```

---

### Finishing checks (controller)

- Full suite, ruff, `bash benchmark/harness/gates.sh` (9 gates).
- Pre-feature proof in a scratch worktree of `main` (`d257cc8`): copy in `tests/test_masking.py`, `tests/test_security_headers.py`, `tests/test_workbench_api.py`, `tests/test_sheet_api.py`, `tests/test_exports.py` and run them: the new tests fail. Remove the worktree.
- Browser on temporary `check-api-8200` / `check-frontend-3200` launch configs: `curl -sI http://127.0.0.1:3200/login | grep -i -E "x-frame|content-security|referrer"`, `curl -sI http://127.0.0.1:8200/api/system/status`, `/docs` is 200 locally; the login page in the browser pane shows zero CSP-Report-Only violations in the console; on the manual stack as `clerk@bench.test:clerk` an invoice with bank details shows `…NNNN` in the detail and the sheet, and as the default admin the full number. Remove the configs.
