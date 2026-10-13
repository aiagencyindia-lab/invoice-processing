# Logging and error reporting — design

SOC 2 readiness, sub-project 4 of 5. Brainstormed with the user 2026-10-13;
the three decisions and the seven design sections were approved in turn.

## Why now

Sub-projects 1–3 (audit integrity, hardening, SDLC evidence) are merged. An
auditor's next questions are "what happened at 14:02 on the 9th" and "how do
you learn that something broke". Today neither can be answered from the
logs: the API never configures logging, the worker writes bare lines, no
line carries a request id, and no error reaches anyone. The audit trail is
good at *who did what*; this sub-project is about *what the system did and
what went wrong*, joined to the trail by one id.

## Where we start

Measured 2026-10-13 on `main` (`75f3da5`):

- **The API configures no logging.** The root logger is at WARNING with no
  handler, so every `logger.info` in the application is dropped and
  warnings leave as bare message text through Python's fallback handler.
  uvicorn's own access log is on, in its own format, with no request id.
- **The worker** runs `logging.basicConfig(format="%(levelname)s
  %(message)s")`: no timestamp, no logger name. The ~300 pipeline `print`
  lines (" [2/9] Extractor Agent…", evidence strings) go to its stdout as
  plain text.
- **The request id** (`auth.current_request_id`, set by
  `service._identify_request`, echoed as `X-Request-Id`) reaches audit rows
  written during a request. It is in no log line, it never crosses the
  queue (`enqueue_document` sends tenant, document, sha and storage key), so
  the worker's audit rows carry the queue message id instead, and
  `Worker._emit`'s rows carry nothing. The frontend reads the header
  nowhere. An inbound `X-Request-Id` is trusted unchecked against a
  64-character audit column.
- **Errors.** The middleware catches unhandled exceptions and logs
  `"Unhandled error on %s %s"` without the id. The worker logs a one-line
  warning per failed attempt (no traceback) and, on the last attempt, stores
  `traceback.format_exc()` inside the dead-letter audit payload. Nothing
  reports anywhere.
- **PII.** Sign-up and SSO log email addresses (`signup.py:164`,
  `sso.py:111/147/223/245`). Nothing logs tokens, bodies or bank numbers.
  `api.log` is committed to git.
- **Dependencies.** No `sentry-sdk`, `structlog` or OTel exporter.
  `sentry-sdk` 2.72.0 depends only on `urllib3` and `certifi`.

## Decisions

- **Standard library.** `logging` with a JSON formatter and a context
  filter, fed by ContextVars. No structlog (a second idiom beside 35
  existing loggers, for nothing it adds here); no OpenTelemetry (needs an
  exporter and a backend that do not exist; traces are not the ask).
- **Sentry for errors.** The user creates the project during this work and
  puts the DSN in the gitignored `.env`; everything is gated on
  `SENTRY_DSN`, so tests and local runs never report.
- **Retention.** Application logs live in Railway's window (7 days on
  Hobby, 30 on Pro) and are JSON so they can be searched. The append-only
  Postgres audit trail with its CSV export (PR #18) is the long-retention
  security record. The split is written down; no log forwarder now.
- **Pipeline prints stay.** They are the script-mode UX and tests read
  them. The worker logs one structured line per pipeline step instead.

## Design

### 1. `invoice_processing/observability.py`

- Two ContextVars, `current_tenant_id` and `current_document_id`; the
  request id stays `auth.current_request_id`.
- `ContextFilter` adds `request_id`, `tenant_id`, `document_id`, `service`
  and `env` to every record.
- `JsonFormatter` writes one object per line: `ts`, `level`, `logger`,
  `msg`, the five context fields, any `extra=` keys, and `exc` (the
  formatted traceback) when present. Standard `json.dumps` with
  `default=str`.
- `configure(service)`: one stdout handler on the root logger; level from
  `LOG_LEVEL` (default INFO); JSON when `LOG_FORMAT=json`, or when
  `LOG_FORMAT` is unset and `preflight.is_deployed()`; otherwise
  `"%(asctime)s %(levelname)s %(name)s %(message)s"` so local terminals stay
  readable. uvicorn's `uvicorn` and `uvicorn.error` loggers propagate to
  root with no handlers of their own; `uvicorn.access` is disabled because
  section 2 replaces it. Calling `configure` twice replaces its own handler
  rather than adding a second.
- `init_sentry(service)`, called by `configure`, does nothing without
  `SENTRY_DSN`. With one: `environment=ENV`, `release` from
  `RAILWAY_GIT_COMMIT_SHA` when set, `send_default_pii=False`,
  `max_request_body_size="never"`, `traces_sample_rate=0`,
  `LoggingIntegration(level=INFO, event_level=ERROR)` so ERROR records
  become events and INFO records breadcrumbs, and `before_send=scrub`.
  `scrub` drops `Authorization`, `Cookie` and `X-Dev-Principal` from the
  event's request headers and tags the event with the three ids and the
  service. An `LlmUnavailable` event gets its own fingerprint so an outage
  is one issue, not one per invoice.
- Called from the API lifespan (`service._lifespan`, before `enforce()`)
  and from `worker/__main__.py` in place of `basicConfig`.

### 2. The API request line

- `_identify_request` accepts an inbound `X-Request-Id` only when it
  matches `[A-Za-z0-9_-]{1,64}`; otherwise it generates one.
- It times the request and, after the response, logs one INFO record
  `"request"` with `method`, `route` (the matched route template when
  Starlette has one, else the path), `status`, `duration_ms` and `ip`. The
  tenant arrives through the filter: `auth.current_principal` sets
  `current_tenant_id` once the principal is known, and the middleware resets
  it in `finally`. The unhandled-exception record gains the request id
  through the filter.
- `frontend/src/lib/api.ts`: `ApiError.requestId` is read from the
  response header; `friendly` for a status of 500 or more becomes
  "Something went wrong (ref <id>)" when an id is present. The exact
  strings `tests/test_frontend_reaches_the_flow.py` greps stay as they are.

### 3. The worker

- `enqueue_document` and `enqueue_rematch` put the API's request id in the
  payload (`request_id`). The rematch caller in `po_routes.py` passes the
  current request id.
- `Worker.handle` sets the three ContextVars from the payload for the
  duration of the message (request id from the payload, else the message
  id), and every `RunContext` the worker builds, `_emit`'s included, uses
  that id as `run_id`. Every log line and audit row from a job then names
  the request that caused it.
- One INFO record `"job"` per message with `outcome`, `attempt`,
  `duration_ms`, `message_id` and `type`. A failed attempt logs through
  `logger.exception` (traceback in the log). A dead letter's audit payload
  keeps `reason` as the exception class and its first 300 characters; the
  traceback is no longer written into the audit row. `sentry_sdk.capture_exception`
  runs when a job is dead-lettered or exhausts its retries (a no-op without
  a DSN).
- `process_document`'s `publish_step` logs one INFO record `"pipeline
  step"` with the step name and `elapsed_ms` since the previous step, next
  to the artifact it already writes.

### 4. PII and hygiene

- Sign-up logs the tenant slug and the Firebase uid, not the email. The
  four SSO warnings log the uid or tenant where they logged the email.
- `api.log` leaves git and joins `.gitignore` beside `worker.log`.
- Limit, stated: exception text (`str(exc)`) still reaches logs, dead-letter
  reasons and Sentry; a driver error could carry a value. Sentry's PII
  default is off and request bodies are never sent.

### 5. Dependencies and documents

- `pyproject.toml`: `sentry-sdk>=2.72,<3`.
- `SECURITY.md` gains "Logs and monitoring": what a line carries, where
  logs live and for how long, that the audit trail and its export are the
  long-retention record, Sentry for errors, the request id as the join key
  across the three, and what is never logged.
- `.env.example` and `deployment/README.md`: `LOG_LEVEL`, `LOG_FORMAT`,
  `SENTRY_DSN`; Railway supplies `RAILWAY_GIT_COMMIT_SHA`.

### 6. Tests

- `tests/test_observability.py`: a JSON line parses and carries the ids
  from the ContextVars; text format when not deployed; `configure` is
  idempotent; `init_sentry` does not call `sentry_sdk.init` without a DSN
  and calls it with the options above with one; `scrub` strips the three
  headers and sets the tags; the `LlmUnavailable` fingerprint.
- API: an invalid `X-Request-Id` is replaced and a valid one echoed; the
  `request` record carries status, duration and the id; the crash path's
  record carries the id.
- Worker, on `LocalQueue`: the payload round-trips the request id; the
  `job` record has outcome and duration; the audit row's `request_id`
  equals the API's; the dead-letter payload contains no `Traceback`;
  `capture_exception` is called on exhaustion (recorder).
- PII: `caplog` for sign-up and the SSO paths contains no `@`.
- Frontend: `test_frontend_reaches_the_flow.py` greps `x-request-id` in
  `api.ts`; lint and type-check.

### 7. Verification

- Full suite, ruff, the nine gates, the CI Postgres list, the audit gate
  (one new dependency).
- A browser pass on the 8200/3200 configs with `LOG_FORMAT=json`: the
  API's `preview_logs` shows one JSON `request` line per call whose id
  matches the response header; a worker run shows `job` and `pipeline
  step` lines with the same ids.
- Sentry: with the user's DSN in `.env`, one API error and one worker
  dead letter produce two events in the project, tagged with service and
  request id, and no `Authorization` header. The user confirms in the
  Sentry UI.
- Whole-branch review on the `fable` model, one fix pass, CI green.

## Out of scope

- A log forwarder and retention beyond Railway's window.
- Sentry in the Next.js app, error boundaries, performance tracing.
- Converting the pipeline's `print` lines.
- Alerting rules inside Sentry (the user's to set).
