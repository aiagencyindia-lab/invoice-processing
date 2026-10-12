# SDLC evidence — design

SOC 2 readiness, sub-project 3 of 5. Brainstormed with the user 2026-10-12;
both design sections were approved in turn.

## Why now

Sub-projects 1 (audit integrity) and 2 (hardening) are merged. This one is
the evidence an auditor asks for about how change is controlled: known
vulnerable dependencies are caught before merge, upgrades arrive on a
schedule, the code is checked for security anti-patterns, and the change
process is written down. Production is still down, so nothing here touches
Railway.

## Where we start

Measured 2026-10-12 on `main` (`abde75f`):

- `npm audit` in `frontend/`: 13 findings, 7 in production dependencies,
  one **critical**: `next` 16.3.2 is inside the vulnerable range
  16.0.0–16.3.7 (unauthenticated RCE in the Image Optimization API, cache
  poisoning, SSRF and others). `firebase` 12.18 pulls `@grpc/grpc-js` with
  high-severity advisories. All have fixes available.
- `pip-audit` is not installed; the Python side is unmeasured.
- No Dependabot configuration, no CODEOWNERS, no SECURITY.md.
- Ruff's `S` (flake8-bandit) rules are not selected. Enabling them reports
  4292 hits: 4241 are `assert` in tests; 15 are in `invoice_processing/`.
- `main` has no branch protection. The repo is private on a personal
  account, where protection needs a paid plan.

## Decisions (user rulings)

1. **Fail CI on high/critical advisories; fix today's findings first.**
   Dependabot opens weekly upgrade PRs. A finding with no fix yet can be
   allow-listed with a reason and an expiry.
2. **No GitHub plan upgrade.** CODEOWNERS and SECURITY.md document the
   change process; enforcement waits for a paid plan or an org move. The
   merge history (every change by PR with green CI) is the evidence until
   then.
3. **Starlette waits for the google-adk 2.x migration.** Its advisories go
   on the allowlist with a 60-day expiry so CI forces the migration onto the
   schedule; the migration is its own sub-project, gated by the benchmark.

Out of scope: CodeQL (needs Advanced Security on private repos), secret
scanning beyond the existing `.env` check in CI, pre-commit hooks, branch
protection.

## Design

### 1. Dependency audit gate — `scripts/audit_gate.py`

One script, run by a new CI job `audit`:

- **Python**: `uv export --frozen --no-dev --format requirements-txt` piped
  to `pip-audit` (run with `uvx`, so it is not a project dependency), JSON
  output. Production dependencies only.
- **Frontend**: `npm audit --omit=dev --json` in `frontend/`.
- **Gate**: any finding of severity high or critical fails, unless its
  advisory id is in `security/audit-allowlist.json`:
  `[{"id": "GHSA-…", "reason": "…", "expires": "YYYY-MM-DD"}]`. An entry
  past its expiry fails the build, so a waiver cannot become permanent. A
  malformed allowlist fails.
- pip-audit's JSON carries no severity, so every Python finding is treated
  as high (fail closed). An allowlist entry matches an advisory by its id or
  any of its aliases (GHSA, CVE).
- Output: one line per finding (ecosystem, package, version, advisory id,
  severity, fixed versions), then a verdict. Exit 0 or 1.

Pure functions (`findings_from_pip_audit`, `findings_from_npm_audit`,
`load_allowlist`, `gate`) carry the logic so they are tested on canned JSON;
`main()` only runs the two commands and prints.

### 2. Fix today's findings

Measured with pip-audit 2.10.1 on 2026-10-13: 17 production Python packages
carry known advisories. Ruling 3 below governs the two that cannot simply be
upgraded.

- `next` and `eslint-config-next` 16.3.2 → 16.3.8 (above the vulnerable
  range 16.0.0–16.3.7).
- `@grpc/grpc-js`: every Firebase release, 12.x and 13.0, pins `~1.9`; the
  fix is in 1.13.6. It is the Node-only transport for Firestore, which the
  app never imports (it uses Firebase Auth). An npm `overrides` entry pins
  it to `^1.14.6`; the production build and e2e prove the app still works.
- Python, in `uv.lock` only (`uv lock --upgrade-package …`): aiohttp,
  anyio, authlib, cryptography, httplib2, idna, mako, mcp, multidict,
  pillow, pyasn1, pydantic-settings, pyjwt, python-multipart, sqlparse,
  urllib3. All resolve within today's constraints.
- Allow-listed, expiring 2026-12-12 (60 days):
  - Starlette's five advisories (PYSEC-2026-161, -248, -249, -2280,
    -2281): the fix needs Starlette 1.x, which needs google-adk 2.x and
    google-cloud-aiplatform 2.x — a migration of the Vertex Gemini layer
    the pipeline depends on, scheduled as its own sub-project.
  - PyJWT PYSEC-2026-4146: no fixed version published.

### 3. Dependabot — `.github/dependabot.yml`

Weekly updates for the `uv` ecosystem (root), `npm` (`/frontend`) and
`github-actions` (root). Minor and patch bumps grouped into one PR per
ecosystem; at most 5 open PRs per ecosystem. Security updates arrive as
their own PRs regardless of the schedule.

### 4. Ruff security rules

`S` added to `[tool.ruff.lint] select` in `pyproject.toml`.

- `tests/**`, `benchmark/**`, `scripts/**` ignore `S101` (assert), `S105`,
  `S106`, `S107` (test passwords), `S603`, `S607` (subprocess to our own
  tools), `S608` (SQL built from fixture literals), `S108` (temp paths in
  fixtures).
- `invoice_processing/` is fixed or justified case by case:
  - `try`/`except`/`pass` and `try`/`except`/`continue` (`api/ingest.py`,
    `core/config.py`, `core/impact_assessor.py`,
    `shared_libraries/acting/general_invoice_agent.py`,
    `shared_libraries/alf_engine.py`): log what was skipped, or narrow the
    exception.
  - `db/po_lock.py` `sha1`: `usedforsecurity=False` (a lock key, not a
    secret).
  - `core/impact_assessor.py` `random`: `# noqa: S311` — sampling, not
    cryptography.
  - `confidence.py` `_NOT_AFTER_TOKEN` / `_NOT_BEFORE_TOKEN`:
    `# noqa: S105` — regex tokens.
  - `confidence.py` tesseract call: `# noqa: S603` — fixed argv, no shell.
  - `gcp_credentials.py` `/tmp` path: `# noqa: S108` — written with mode
    600 and overridable by `GOOGLE_CREDENTIALS_PATH`.
  - `db/session.py` `assert`: a `RuntimeError`, since asserts vanish under
    `python -O`.

Every `noqa` carries its reason on the same line.

### 5. CODEOWNERS and SECURITY.md

- `.github/CODEOWNERS`: `* @vineetyad`.
- `SECURITY.md` at the repo root: how to report a vulnerability (email
  aiagencyindia@gmail.com); the change process (every change by pull
  request with the four CI jobs and the audit job green; the merge history
  is the evidence); the controls CI enforces (tests, tenant isolation,
  Postgres RLS, the audit gate, the `S` rules); and the known gap (GitHub
  does not enforce any of this on a private personal repo without a paid
  plan).

## Testing

- `tests/test_audit_gate.py` on canned pip-audit and npm audit JSON: a high
  finding fails; a moderate passes; an allow-listed id passes; an expired
  entry fails; a malformed allowlist fails; a Python finding with no
  severity fails. Each fails before the script exists.
- After the upgrades: the full Python suite, `benchmark/harness/gates.sh`
  and e2e (38) pass; the real `audit_gate.py` run exits 0.
- `ruff check .` passes with `S` selected.
- The new `audit` CI job runs on the PR itself.

## Residual risk

- Without branch protection, nothing stops a direct push to `main`. The
  merge history and SECURITY.md are the evidence until the plan changes.
- A newly published advisory against an existing dependency fails an
  unrelated PR. That is the point of the gate; the allowlist with an expiry
  is the escape hatch.
- Upgrading Next.js within 16.3.x could change behaviour; the e2e suite is
  the check.
- Starlette runs with five known advisories until the google-adk 2.x
  migration; the allowlist expiry on 2026-12-12 fails CI if it slips.
- The `@grpc/grpc-js` override runs Firestore's transport on a newer minor
  than Firebase declares; harmless while Firestore is unused, and Dependabot
  will surface a Firebase release that lifts the pin.
