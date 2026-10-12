# Application hardening — design

SOC 2 readiness, sub-project 2 of 5. Brainstormed with the user 2026-10-12;
the two rulings below were each approved in turn, and the plan built from
them was approved before this file was written.

## Why now

Sub-project 1 (audit integrity, PR #18) is merged. This is the cheap,
visible hardening a security questionnaire checks first. Production is still
down, so everything here is made ready and proven locally; nothing touches
Railway.

## Where we start

- The API serves `/docs`, `/redoc` and `/openapi.json` unauthenticated in
  every environment (`api/service.py:65` sets no `docs_url`).
- No response carries a security header. The only middleware
  (`_identify_request`, `service.py:68`) sets `x-request-id`. The Next.js app
  (`frontend/next.config.ts`) proxies `/api` on the same origin and has no
  `headers()`.
- `.railwayignore` does not exclude `.env`, so the root `.env` is uploaded to
  Railway's builder. `.dockerignore` does exclude it.
- Bank masking is half done. `api/vendor_routes.py` masks account numbers to
  the last four for clerks and shows them in full to approvers, who need
  them to verify a change. But `GET /api/invoices/{case_id}` returns the
  printed bank fields to every role including viewers; the sheet's
  `bank_account_number` column is readable by viewers; and CSV/JSON/XLSX
  exports carry full numbers out of the system. The existing `mask()` also
  returns a value of four characters or fewer verbatim.

## Decisions (user rulings)

1. **Full bank numbers only for approver and admin** (`principal.can(
   "approver")`), the rule vendor routes already use. Viewers and clerks get
   the last four everywhere: invoice detail, sheet rows, exports. Clerks
   still see the full number on the invoice PDF, and proposing a bank change
   from an invoice works from stored fields server-side, so nothing they do
   needs the digits in JSON.
2. **Security headers everywhere; CSP in Report-Only mode now.** HSTS,
   nosniff, `X-Frame-Options: DENY`, Referrer-Policy and Permissions-Policy
   are enforced. The CSP ships as `Content-Security-Policy-Report-Only`,
   allowing the Firebase/Google sign-in origins, so violations show in the
   browser console without blocking anything. Google sign-in cannot be
   live-tested locally, so the policy is flipped to enforcing after
   live-verify.
3. `/docs`, `/redoc` and `/openapi.json` are off when `preflight.is_deployed()`
   (ENV in production/prod/staging/stage/uat); on locally.
4. `.railwayignore` gains `.env` and `frontend/.env.local`.

Not in scope: rate limiting (no unauthenticated surface besides `/` and the
secret-checked webhook), a nonce-based CSP (needed when moving from
Report-Only to enforcing), a CSP report collector (none exists; the console
is the report), a health endpoint.

## Design

### 1. Bank masking — `invoice_processing/masking.py` (new, pure)

```python
BANK_FIELDS = ("bank_account_number", "bank_iban")
MASK_PREFIX = "…"
def mask_bank(value) -> str | None   # None/"" → None; else "…" + last 4 of the value, spaces removed
def mask_row(row: dict) -> dict      # a copy with BANK_FIELDS masked where present
```

Short values always get the prefix (`"…123"`), never the raw value.
`vendor_routes.mask` becomes `from ..masking import mask_bank as mask`, so
its call sites and the existing `…5572` assertions are unchanged.

Call sites, all keyed on `principal.can("approver")`:

- `GET /api/invoices/{case_id}` (`service.py:885`): after the `fields` loop,
  mask the value of any field named in `BANK_FIELDS`. `_summarize` carries
  no bank fields; `POST /api/invoices/{case_id}/bank-change` reads the stored
  artifacts, not the response.
- `PATCH /api/invoices/{case_id}/fields` (`service.py:997`): both edit
  clients send only changed fields, so a masked value is written back only
  if someone types it. Guard anyway, in the existing pre-validation loop: a
  `BANK_FIELDS` edit whose value starts with `MASK_PREFIX` → 422 "That looks
  like a masked value; enter the full number."
- Sheet rows: `SheetRepository.page` has no principal; `api/sheet.py
  list_sheet` masks after `page()` returns. `/api/sheet/summary` is
  aggregates only.
- Exports: `create_export` stores `"mask_bank": not principal.can("approver")`
  in the job spec; `exports.run` applies `mask_row` after `collect_rows` when
  `spec.get("mask_bank", True)`. A job created before this change has no key
  and masks. Retry reuses `run`.

### 2. Security headers

**API**: extend `_identify_request` (`service.py:68`), after `call_next`:

- Always: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: strict-origin-when-cross-origin`,
  `Permissions-Policy: camera=(), microphone=(), geolocation=()`.
- `Strict-Transport-Security: max-age=31536000; includeSubDomains` only when
  `preflight.is_deployed()`, read per request so tests can set ENV.
- `Cache-Control: no-store` when the path starts with `/api/`, the response
  content-type starts with `application/json`, and there is no
  `Content-Disposition`. The PDF, the export download and the audit CSV
  stream keep default caching so "Save as" works.
- When the content-type is `text/html` (the `/` page, `/docs`, `/redoc`), an
  enforced `Content-Security-Policy: default-src 'self'; script-src 'self'
  https://cdn.jsdelivr.net 'unsafe-inline'; style-src 'self'
  https://cdn.jsdelivr.net 'unsafe-inline'; img-src 'self' data:
  https://fastapi.tiangolo.com; frame-ancestors 'none'; base-uri 'self';
  form-action 'self'; object-src 'none'`. These pages are static.

**Frontend**: `frontend/next.config.ts` gains `headers()` with `source:
"/:path*"`: the same four fixed headers; HSTS when `process.env.NODE_ENV ===
"production"` (browsers ignore it over http, so the e2e production build is
unaffected); and `Content-Security-Policy-Report-Only` with
`AUTH = process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN || "*.firebaseapp.com"`:

```
default-src 'self';
script-src 'self' 'unsafe-inline' https://apis.google.com https://www.gstatic.com;
style-src 'self' 'unsafe-inline';
img-src 'self' data: blob: https://*.googleusercontent.com;
font-src 'self' data:;
connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com https://www.googleapis.com https://<AUTH> https://*.firebaseapp.com;
frame-src https://<AUTH> https://*.firebaseapp.com https://accounts.google.com https://apis.google.com;
object-src 'none'; base-uri 'self'; frame-ancestors 'none';
form-action 'self' https://accounts.google.com
```

`'unsafe-inline'` for scripts because Next emits its inline runtime without
a nonce, and for styles because the app uses inline `style={{}}` throughout.
Next applies `headers()` to proxied `/api/*` responses too: the fixed headers
duplicate the API's identical values, and browsers ignore CSP on
non-document responses, so this is accepted rather than filtered.
`X-Frame-Options: DENY` does not affect `signInWithPopup` (a separate window)
or `signInWithRedirect` (a full navigation); the SDK's hidden auth-domain
iframe is governed by our `frame-src`.

### 3. Docs off when deployed

`service.py`: `_SERVE_DOCS = not preflight.is_deployed()` at import (ENV is
fixed for a process; `preflight` imports only stdlib and `gcp_credentials`)
and `FastAPI(..., docs_url="/docs" if _SERVE_DOCS else None, redoc_url=…,
openapi_url=…)`. `tests/test_tenant_isolation_api.py` keeps the four docs
routes in `EXEMPT` (absent entries are tolerated) with the reason "schema
only; absent when deployed".

### 4. `.railwayignore`

`.env` and `frontend/.env.local`, under a comment saying why.

## Testing

Each new test fails on `main` (`d257cc8`) before its change.

- New `tests/test_masking.py`: `mask_bank("1234") == "…1234"`,
  `mask_bank("12 34 56") == "…3456"`, `mask_bank("") is None`, `mask_row`
  leaves non-bank keys alone and does not mutate its input.
- `tests/test_workbench_api.py`: set `bank_account_number` to
  `901031925572` through the fields editor as admin; clerk and viewer GET →
  `"…5572"`; approver and admin → full. PATCH as clerk with `"…5572"` → 422;
  a real number → 200.
- `tests/test_sheet_api.py`: clerk rows masked, approver rows full.
- `tests/test_exports.py`: a clerk's CSV contains `…5572` and not the full
  number; an approver's contains it; a job created with a spec lacking
  `mask_bank`, then run, is masked; the download response carries no
  `no-store`.
- New `tests/test_security_headers.py`: the four fixed headers on a JSON
  route; HSTS present under `ENV=production`, absent under `local`;
  `Cache-Control: no-store` on JSON, absent on `/`; the HTML CSP on `/`;
  `/docs` and `/openapi.json` 200 locally and 404 under `ENV=production`
  (one subprocess that imports the app fresh).
- `frontend/e2e/golden-path.spec.ts`: `page.request.get("/login")` headers
  include `x-frame-options: DENY`, `x-content-type-options: nosniff`, and a
  `content-security-policy-report-only` containing
  `identitytoolkit.googleapis.com`.
- `tests/test_frontend_reaches_the_flow.py` greps frontend source for exact
  strings; run the Python suite after the frontend edit.

Gates: full pytest, ruff, `benchmark/harness/gates.sh`, frontend lint/tsc/e2e,
a browser check on temporary port-8200/3200 configs (headers via `curl -I`,
`/docs` 200 locally, login page with zero CSP-Report-Only console violations,
masked vs full bank number by role on the manual stack).

## Residual risk

- The CSP's effect on real Google sign-in (popup and redirect fallback) can
  only be confirmed when Identity Platform is live. It ships Report-Only for
  that reason; enforcing it is a one-word change after live-verify.
- An export requested by an approver carries full bank numbers in a file
  that leaves the system. That is the ruling: approvers already see them.
