# Security

## Reporting a vulnerability

Email aiagencyindia@gmail.com with what you found and how to reproduce it.
Please do not open a public issue. You will get an answer within five
working days.

## How changes reach `main`

Every change lands by pull request. A pull request merges only when CI is
green on its head commit:

- **test**: lint (including ruff's security rules), format, the full test
  suite, the tenant-isolation suite, the evaluation gate, the phase-1
  acceptance sweep, and a scan that fails if a `.env` file or a service
  account key is committed;
- **postgres**: migrations up and down, and the database tests against
  Postgres with forced row-level security;
- **frontend**: lint and a production build;
- **e2e**: the browser suite against a production build;
- **audit**: `scripts/audit_gate.py` fails on any high or critical advisory
  in production dependencies. A waiver needs an entry in
  `security/audit-allowlist.json` with a reason and an expiry date, and an
  expired waiver fails the build.

Dependabot proposes dependency upgrades weekly (`.github/dependabot.yml`).
Security-fix pull requests outside that schedule need "Dependabot security
updates" turned on in the repository's Code security settings.

The merge history is the record: every change since the first pull request
is a merge commit naming its pull request, with its CI run attached.

## Logs and monitoring

The API and the worker write one JSON line per event to standard output
when deployed (`LOG_FORMAT=json`, the default under `ENV=production` or
`staging`). Every line carries `request_id`, `tenant_id` and `document_id`;
the API writes one line per request (method, route, status, duration, ip)
and the worker one per job (outcome, attempt, duration) and per pipeline
step. The request id is also returned to the caller as `X-Request-Id`, shown
to a user on a server error, carried on the queue message to the worker, and
stored on every audit event, so one id joins a user's report, the log line,
the worker's lines and the audit trail.

Logs are kept by the hosting platform for its retention window (Railway:
7 days on Hobby, 30 on Pro). The append-only audit trail in Postgres
(`audit_events`, which the application role can only insert into and read)
is the long-retention security record; an admin exports it as CSV from the
Activity page or `GET /api/audit/export`.

Errors are reported to Sentry when `SENTRY_DSN` is set: unhandled request
errors, jobs that exhaust their retries, and anything logged at ERROR. PII
collection is off, request bodies are never sent, and `Authorization`,
`Cookie` and `X-Dev-Principal` headers are removed before an event leaves
the process. Lines never carry bearer tokens, request bodies, bank account
numbers or email addresses; exception text is logged as raised.

## Known gap

These rules are followed and evidenced but not enforced by GitHub: branch
protection on a private repository owned by a personal account needs a paid
plan. Until the repository moves to a paid plan or an organisation, nothing
technically prevents a direct push to `main`. CODEOWNERS
(`.github/CODEOWNERS`) is in place for when protection is turned on.
