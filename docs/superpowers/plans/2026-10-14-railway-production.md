# Production on Railway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ledgerly runs in production on Railway (one API+worker service with a `/data` volume, a frontend service, Railway Postgres), with Gemini and sign-in on Google project `rasayan`.

**Architecture:** A call-time `paths.state_dir()` moves the durable runtime state (documents, local job queue, worker heartbeat) to `STATE_DIR` when set; preflight accepts a writable `STATE_DIR` in place of `GCS_BUCKET`; `scripts/start-railway.sh` runs both processes and exits when either stops. Docs become the Railway runbook. The rollout itself (Task 6) happens after the PR merges, step by step with the user's go-ahead.

**Tech Stack:** Python 3.12, FastAPI, POSIX sh, Docker (uv), Railway, Google Vertex AI + Identity Platform.

**Spec:** `docs/superpowers/specs/2026-10-14-railway-production-design.md`

## Global Constraints

- `STATE_DIR` unset ⇒ behaviour identical to today (everything under `DATA_DIR`).
- Tracked files (rules book, master data, rule base) stay under `DATA_DIR`; only documents (`blobs/`), the local queue (`queue/`) and `worker_heartbeat.json` move.
- Preflight in a deployed environment: storage is satisfied by `GCS_BUCKET` **or** an existing, writable `STATE_DIR`.
- The start script is POSIX `sh`; it exits non-zero when either process stops, and 0 on SIGTERM/SIGINT after stopping both.
- No production secret is created, read or pasted by the agent; the user sets `GOOGLE_CREDENTIALS_JSON`, role passwords, `DATABASE_URL`, `MIGRATE_DATABASE_URL`, `SENTRY_DSN`.
- Service account roles: exactly `roles/aiplatform.user` and `roles/firebaseauth.admin`.
- RPO up to 24 hours (daily Railway volume backups); RTO 4 hours; drill every 6 months.
- ruff line length 80; `# noqa` with reasons; never `git commit -a`; trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Rulings made while planning

- **`state_dir()` is a function, not a constant:** callers read `paths.DATA_DIR` at call time today and tests patch it; a constant fixed at import would ignore both.
- **No local `docker build`:** about 776 MB is free on the Mac. Task 4 checks the Dockerfile's uv pin against the lock with that uv directly; Railway's first build (Task 6) is the build test.
- **The start script takes `WORKER_CMD` / `API_CMD` overrides** so its test can use stubs.

## Review Focus

1. `STATE_DIR` set to a path with a trailing slash or surrounding spaces → same directory, no `//` keys. Pinned in Task 1.
2. `STATE_DIR` set but empty string → treated as unset (today's paths). Pinned in Task 1.
3. Railway sends SIGTERM on redeploy → both processes stop and the script exits 0, not 1 (a non-zero exit would mark a clean redeploy as a crash). Pinned in Task 3.
4. A `STATE_DIR` that is a file, not a directory → preflight refuses it as missing. Pinned in Task 2.
5. Preflight outside a deployed environment with a bad `STATE_DIR` → a warning, not a refusal (local dev unaffected). Pinned in Task 2.

---

## File Structure

| File | Change |
|---|---|
| `invoice_processing/paths.py` | `state_dir()` |
| `invoice_processing/api/service.py` (~786) | local blob root under `state_dir()` |
| `invoice_processing/worker/queue.py` (~355) | local queue root under `state_dir()` |
| `invoice_processing/worker/runner.py` (~598) | heartbeat under `state_dir()` |
| `invoice_processing/api/preflight.py` | accept `STATE_DIR` |
| Create `scripts/start-railway.sh` | both processes |
| `Dockerfile` | uv pin, if Task 4 finds it too old |
| `deployment/README.md`, `deployment/BACKUP_RESTORE.md`, `SECURITY.md` | Railway runbook, backups |
| Create `tests/test_state_dir.py`, `tests/test_start_railway.py`; modify `tests/test_preflight.py` | tests |

---

### Task 1: `STATE_DIR`

**Files:** `invoice_processing/paths.py`, `api/service.py`, `worker/queue.py`, `worker/runner.py`; create `tests/test_state_dir.py`.

**Interfaces — Produces:** `paths.state_dir() -> Path`.

- [ ] **Step 1: Failing tests** — `tests/test_state_dir.py`:

```python
"""Durable runtime state goes where STATE_DIR says; tracked files do not."""

from __future__ import annotations

from invoice_processing import paths
from invoice_processing.api import service
from invoice_processing.worker import queue as queue_mod
from invoice_processing.worker import runner


def test_unset_keeps_everything_under_the_data_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("STATE_DIR", raising=False)
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    assert paths.state_dir() == tmp_path


def test_an_empty_value_counts_as_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", "  ")
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    assert paths.state_dir() == tmp_path


def test_set_moves_documents_queue_and_heartbeat(monkeypatch, tmp_path):
    state = tmp_path / "volume"
    monkeypatch.setenv("STATE_DIR", f" {state}/ ")
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "image")
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    monkeypatch.delenv("PUBSUB_TOPIC", raising=False)
    assert paths.state_dir() == state
    assert service.get_blobs("t1").root == state / "blobs" / "t1"
    assert queue_mod.get_queue().root == state / "queue"
    assert runner.heartbeat_path() == state / "worker_heartbeat.json"


def test_tracked_files_stay_in_the_image(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "volume"))
    assert paths.RULES_BOOK_PATH.parent == paths.DATA_DIR
```

Check `LocalQueue`'s attribute for its root (read `worker/queue.py` `class LocalQueue.__init__`); use the real name in the assertion (`root` is assumed).

- [ ] **Step 2: Run** `./.venv/bin/python -m pytest tests/test_state_dir.py -q -p no:cacheprovider` — Expected FAIL: `AttributeError: module 'invoice_processing.paths' has no attribute 'state_dir'`.

- [ ] **Step 3: Implement.** In `paths.py`, after the `DATA_DIR` block:

```python
def state_dir() -> Path:
    """Where durable runtime state lives: uploaded documents, the local job
    queue and the worker heartbeat.

    `STATE_DIR` -- a mounted volume in production (Railway: `/data`) -- when
    set; otherwise `DATA_DIR`, as before. Tracked files (the rules book, the
    rule base) stay under `DATA_DIR`, in the image, so a fresh volume does
    not hide them. Read at call time, like the callers' `DATA_DIR`.
    """
    raw = os.getenv("STATE_DIR", "").strip()
    return Path(raw) if raw else DATA_DIR
```

(`Path("/x/volume/")` normalises the trailing slash.)

`service.get_blobs`: `return LocalBlobStore(paths.state_dir() / "blobs" / tenant_id)`.
`queue.get_queue`: `return LocalQueue(root or paths.state_dir() / "queue")`.
`runner.heartbeat_path`: `return paths.state_dir() / "worker_heartbeat.json"`.

- [ ] **Step 4: Run** the Step 2 command, then `./.venv/bin/python -m pytest tests/test_paths_are_centralized.py tests/test_production_paths.py tests/test_worker.py tests/test_ingestion.py -q -p no:cacheprovider`. Expected PASS.
- [ ] **Step 5: Commit** "Keep documents, the queue and the heartbeat where STATE_DIR says".

---

### Task 2: Preflight accepts a volume

**Files:** `invoice_processing/api/preflight.py` (~line 58); `tests/test_preflight.py`.

**Interfaces — Consumes:** nothing new (reads `STATE_DIR` itself, like the other checks read their variables).

- [ ] **Step 1: Failing tests.** In `tests/test_preflight.py`, add `"STATE_DIR"` to the `clean_env` fixture's list, then append:

```python
class TestStateDir:
    """A Railway volume stands in for a bucket; a missing one must not."""

    def _rest(self, monkeypatch):
        monkeypatch.setenv("CLOUD_SQL_INSTANCE", "p:r:i")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")

    def test_a_writable_volume_satisfies_storage(self, monkeypatch, tmp_path):
        self._rest(monkeypatch)
        monkeypatch.setenv("STATE_DIR", str(tmp_path))
        assert check_configuration() == []

    def test_a_missing_volume_is_named(self, monkeypatch, tmp_path):
        self._rest(monkeypatch)
        monkeypatch.setenv("STATE_DIR", str(tmp_path / "nope"))
        [problem] = check_configuration()
        assert "does not exist" in problem and "volume" in problem

    def test_a_file_is_not_a_volume(self, monkeypatch, tmp_path):
        self._rest(monkeypatch)
        target = tmp_path / "f"
        target.write_text("x")
        monkeypatch.setenv("STATE_DIR", str(target))
        [problem] = check_configuration()
        assert "does not exist" in problem

    def test_a_read_only_volume_is_named(self, monkeypatch, tmp_path):
        self._rest(monkeypatch)
        monkeypatch.setenv("STATE_DIR", str(tmp_path))
        monkeypatch.setattr(preflight.os, "access", lambda path, mode: False)
        [problem] = check_configuration()
        assert "not writable" in problem

    def test_neither_still_names_both_ways_out(self, monkeypatch):
        self._rest(monkeypatch)
        [problem] = check_configuration()
        assert "GCS_BUCKET" in problem and "STATE_DIR" in problem

    def test_a_deployed_service_refuses_a_missing_volume(
        self, monkeypatch, tmp_path
    ):
        self._rest(monkeypatch)
        monkeypatch.setenv("ENV", "production")
        monkeypatch.setenv("STATE_DIR", str(tmp_path / "nope"))
        with pytest.raises(ConfigurationError, match="volume"):
            enforce()

    def test_local_development_only_warns(self, monkeypatch, tmp_path, caplog):
        self._rest(monkeypatch)
        monkeypatch.setenv("STATE_DIR", str(tmp_path / "nope"))
        with caplog.at_level("WARNING"):
            enforce()
        assert "does not exist" in caplog.text
```

Import `preflight` (the module) and `ConfigurationError` at the top if the file does not already (`from invoice_processing.api import preflight`; check the existing import block).

- [ ] **Step 2: Run** `./.venv/bin/python -m pytest tests/test_preflight.py -q -p no:cacheprovider` — Expected FAIL: the volume cases still report "GCS_BUCKET is not set".

- [ ] **Step 3: Implement.** Replace the `GCS_BUCKET` check in `check_configuration` with:

```python
    state = os.getenv("STATE_DIR", "").strip()
    if state:
        # A mounted volume stands in for a bucket (Railway); a missing or
        # read-only one would lose every document on the next restart.
        if not Path(state).is_dir():
            problems.append(
                f"STATE_DIR={state} does not exist: is the volume mounted? "
                "Documents and the job queue would be lost on restart."
            )
        elif not os.access(state, os.W_OK):
            problems.append(
                f"STATE_DIR={state} is not writable: documents and the job "
                "queue could not be kept."
            )
    elif not os.getenv("GCS_BUCKET"):
        problems.append(
            "GCS_BUCKET is not set: documents will be written to local disk and "
            "lost when the container restarts. Set GCS_BUCKET, or STATE_DIR to "
            "a mounted volume."
        )
```

Add `from pathlib import Path` to the imports. Update the module docstring's `GCS_BUCKET` bullet to mention `STATE_DIR`.

- [ ] **Step 4: Run** the Step 2 command. Expected PASS (the existing `test_missing_bucket_is_reported` still matches its assertion; check what it asserts and keep it true).
- [ ] **Step 5: Commit** "Let a mounted volume stand in for a bucket at start-up, and refuse a missing one".

---

### Task 3: One start command for both processes

**Files:** create `scripts/start-railway.sh`, `tests/test_start_railway.py`.

- [ ] **Step 1: Failing tests** — `tests/test_start_railway.py`:

```python
"""The Railway start script: both processes, and it stops when either does.

A container whose worker died but whose API still answers looks healthy to
the platform and processes nothing; exiting lets Railway restart it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "start-railway.sh"


def run(worker: str, api: str, **kw) -> subprocess.Popen:
    return subprocess.Popen(
        ["sh", str(SCRIPT)],
        env={**os.environ, "WORKER_CMD": worker, "API_CMD": api},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **kw,
    )


def test_a_dead_worker_stops_the_container():
    proc = run("exit 3", "sleep 60")
    assert proc.wait(timeout=15) != 0


def test_a_dead_api_stops_the_container():
    proc = run("sleep 60", "exit 4")
    assert proc.wait(timeout=15) != 0


def test_sigterm_stops_both_and_is_a_clean_exit():
    """Railway sends SIGTERM on every redeploy; that is not a crash."""
    proc = run("sleep 60", "sleep 60")
    time.sleep(1)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=15) == 0
```

- [ ] **Step 2: Run** `./.venv/bin/python -m pytest tests/test_start_railway.py -q -p no:cacheprovider` — Expected FAIL (script missing: `sh` exits 127, the SIGTERM test sees a non-zero code).

- [ ] **Step 3: Implement** `scripts/start-railway.sh`:

```sh
#!/bin/sh
# Railway start command: the API and the worker in one container, sharing
# the /data volume (documents and the local job queue).
#
# Exits as soon as either process stops, so the platform restarts the
# container instead of serving an API whose worker is gone. SIGTERM (every
# redeploy) stops both and exits 0.
#
# ponytail: one container, scaled up not out. Upgrade path: documents in an
# object store (S3 adapter), a shared queue (Postgres or Pub/Sub) and a
# separate worker service.
set -u

WORKER_CMD="${WORKER_CMD:-python -m invoice_processing.worker}"
API_CMD="${API_CMD:-uvicorn invoice_processing.api.service:app --host 0.0.0.0 --port ${PORT:-8000}}"

sh -c "$WORKER_CMD" &
worker=$!
sh -c "$API_CMD" &
api=$!

stop() {
  kill "$worker" "$api" 2>/dev/null
  wait
}
trap 'stop; exit 0' TERM INT

# POSIX sh has no `wait -n`: check both every second.
while kill -0 "$worker" 2>/dev/null && kill -0 "$api" 2>/dev/null; do
  sleep 1
done
echo "start-railway: a process stopped; stopping the container" >&2
stop
exit 1
```

- [ ] **Step 4: Run** the Step 2 command. Expected PASS. Also `sh -n scripts/start-railway.sh` (syntax) and, if available, `dash -n` / `checkbashisms`.
- [ ] **Step 5: Commit** "Run the API and the worker together on Railway, and stop when either does".

---

### Task 4: The image builds with the lock it ships

**Files:** `Dockerfile` (line 9) only if needed.

- [ ] **Step 1:** Check the pinned uv against the lock: `uvx --from 'uv==0.5.11' uv lock --check 2>&1 | tail -3` (the Dockerfile's `COPY --from=ghcr.io/astral-sh/uv:0.5.11`). Expected (risk from the handoff): a parse error on the lock's `revision = 3`, or "lockfile needs to be updated".
- [ ] **Step 2:** If it fails, change line 9 to the version that wrote the lock, `ghcr.io/astral-sh/uv:0.9.28`, and re-run the check with `uvx --from 'uv==0.9.28' uv lock --check` → Expected "Resolved … packages" / no change. If it passes on 0.5.11, leave the Dockerfile alone and ledger that.
- [ ] **Step 3:** `grep -n "uv" Dockerfile` and update the Dockerfile comment beside the pin to say why it matches the lock writer.
- [ ] **Step 4: Commit** (only if changed) "Pin the image's uv to the one that writes the lock".

---

### Task 5: The Railway runbook, backups and policy

**Files:** `deployment/README.md`, `deployment/BACKUP_RESTORE.md`, `SECURITY.md`.

- [ ] **Step 1: `deployment/README.md`** — replace the top section (`# Release (Railway + Postgres)` through the variables table) with a Railway runbook containing, in this order:

  1. **Layout** — project `ledgerly`: services `ledgerly` (root `Dockerfile`; start command `sh scripts/start-railway.sh`; volume at `/data`; pre-deploy `python -m invoice_processing.db.migrate`), `app` (`frontend/Dockerfile`), `Postgres`.
  2. **Variables**, as a table with a "who sets it" column:

| Service | Variable | Value | Who |
|---|---|---|---|
| ledgerly | `ENV` | `production` | operator |
| ledgerly | `STATE_DIR` | `/data` | operator |
| ledgerly | `RUN_MIGRATIONS` | `0` | operator |
| ledgerly | `GOOGLE_CLOUD_PROJECT`, `PROJECT_ID` | `rasayan` | operator |
| ledgerly | `GOOGLE_GENAI_USE_VERTEXAI` | `TRUE` | operator |
| ledgerly | `APPLY_IDENTITY_CLAIMS` | `1` | operator |
| ledgerly | `ALLOW_SELF_SIGNUP` | `1` (or unset to close sign-up) | operator |
| ledgerly | `GOOGLE_CREDENTIALS_JSON` | the `ledgerly-prod` key | **you set this** |
| ledgerly | `DATABASE_URL` | app role → Railway Postgres (`postgresql+pg8000://ledgerly_app:…@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/railway`) | **you set this** |
| ledgerly | `MIGRATE_DATABASE_URL` | owner role, same host | **you set this** |
| ledgerly | `SENTRY_DSN` | the Sentry DSN | **you set this** |
| app | `API_ORIGIN` | `http://${{ledgerly.RAILWAY_PRIVATE_DOMAIN}}:${{ledgerly.PORT}}` (build time) | operator |
| app | `NEXT_PUBLIC_FIREBASE_API_KEY`, `_AUTH_DOMAIN` (`rasayan.firebaseapp.com`), `_PROJECT_ID` (`rasayan`) | from Identity Platform (public values) | operator |

  3. **Bootstrap order** (unchanged commands, now against Railway Postgres's public TCP proxy from a trusted machine): §1 as `postgres` → first deploy (pre-deploy migrates as owner) → §2 as `postgres` → read VERIFY. Re-run §2 after every deploy that migrates.
  4. **Google side** — service account `ledgerly-prod@rasayan` with `roles/aiplatform.user` and `roles/firebaseauth.admin`; its JSON key into `GOOGLE_CREDENTIALS_JSON` (you); the `app` domain added to Identity Platform → Settings → Authorized domains.
  5. **Smoke test** — open the `app` domain, Continue with Google, sign up an organisation, upload one PDF, see it processed (worker banner green), `python -m invoice_processing.db.restore_check --restored <owner url>` → PASS.

  Keep the existing "Re-run step 3 after every migrate" paragraph and the logging rows (`LOG_LEVEL`, `LOG_FORMAT`, `RAILWAY_GIT_COMMIT_SHA`).

- [ ] **Step 2: `deployment/BACKUP_RESTORE.md`** — "What is backed up": Railway volume backups on the `Postgres` volume and the `ledgerly` `/data` volume, daily schedule plus a manual backup before any risky change; no point-in-time recovery. "Restoring": restore the volume backup in Railway (Service → Volume → Backups → Restore), redeploy, re-run bootstrap §2, `restore_check`, re-purge from the register; documents come back with the `/data` volume backup. Remove the Cloud SQL / GCS steps (keep the local drill and the drill log).

- [ ] **Step 3: `SECURITY.md`** — "Backup and restore" paragraph: Railway volume backups (daily), **RPO up to 24 hours**, RTO 4 hours, drill every 6 months; "What outlives a purge": database and document backups up to the retention of the Railway backup schedule (state the daily-backup retention Railway shows when configuring it — record the actual number in Task 6 Step 4). Hosting line: production on Railway, Gemini and sign-in on Google project `rasayan`.

- [ ] **Step 4: Run** the Python suite and ruff (docs only, but `tests/test_frontend_reaches_the_flow.py` and others grep repo files): `./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider`. Expected PASS.
- [ ] **Step 5: Commit** "Write the Railway runbook and restate backups honestly".

---

### Task 6: Verification, review, PR — then the rollout (after merge, with go-aheads)

- [ ] **Step 1:** Full suite, ruff, 9 gates. Whole-branch review on `fable`; one fix pass; all minors fixed. Push, PR, merge on the user's "merge".
- [ ] **Step 2 (go-ahead first: starts billing):** Railway tools — create project `ledgerly`; service `ledgerly` from GitHub repo `aiagencyindia-lab/invoice-processing` (branch `main`; the Railway GitHub app needs access to the private repo — if it lacks it, the user grants it), start command, pre-deploy command, volume at `/data`, non-secret variables; service `app` (root `frontend`, `frontend/Dockerfile`); Postgres from Railway's template; a public domain for `app`. Do not deploy yet (the secret variables are missing).
- [ ] **Step 3 (go-ahead first):** `gcloud iam service-accounts create ledgerly-prod --project rasayan`; grant the two roles; add the `app` domain to `rasayan`'s authorized domains (read the list, append, write it back, read back).
- [ ] **Step 4 (user):** create the key and set `GOOGLE_CREDENTIALS_JSON`; choose role passwords; run bootstrap §1 against Railway Postgres; set `DATABASE_URL`, `MIGRATE_DATABASE_URL`, `SENTRY_DSN`; configure daily volume backups on both volumes and note the retention. Then deploy; the user runs §2 and reads VERIFY.
- [ ] **Step 5 (together):** smoke test from the runbook; read the service logs for preflight and worker start; record the result and the backup retention in `SECURITY.md`/`BACKUP_RESTORE.md` (small docs PR).
