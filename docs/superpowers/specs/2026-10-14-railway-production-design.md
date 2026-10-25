# Production on Railway — design

Brainstormed with the user 2026-10-14; the hosting decision, the storage
shape and three design sections were approved in turn.

## Why now

Production has been down since the old GCP project's billing closed. The
user asked to restore it.

## Where we start

Measured 2026-10-14 on `main` (`9f6f6f4`):

- The old project `project-cb9934aa-2b3a-4bd4-9ac` is not visible to the
  user's gcloud account (aiagencyindia@gmail.com); its Cloud SQL data is
  unreachable. The connected Railway workspace ("shramiksmart-star's
  Projects") holds two unrelated projects and no Ledgerly services.
- `rasayan` (shared with the user's Rasayan web app) has Vertex AI,
  Identity Platform with Google and Microsoft providers, and `localhost` as
  an authorized domain. One open billing account exists.
- Storage is GCS (`GCS_BUCKET`) or a local directory; the job queue is
  Pub/Sub (`PUBSUB_TOPIC` + `GOOGLE_CLOUD_PROJECT`) or `LocalQueue`. Local
  paths hang off `paths.DATA_DIR`, which also holds tracked files (rules
  book, master data).
- `preflight.enforce` refuses a deployed environment without `GCS_BUCKET`.
- `deployment/README.md` already describes the Railway release step and the
  two-role bootstrap; `SECURITY.md` promises Cloud SQL backups with PITR.

## Decisions

1. **Hosting:** Google services on `rasayan` (Gemini via Vertex AI, sign-in
   via Identity Platform); **database and files on Railway**.
2. **Shape:** one Railway service runs the API and the worker together, with
   one volume for documents and the job queue (the existing local
   adapters); Railway Postgres for the database. Rejected for now: a Railway
   S3 bucket with a new adapter, plus Pub/Sub or a new Postgres queue (the
   scale-out path, recorded as the upgrade).
3. **Fresh start:** the old data is not reachable; production starts empty.
4. **Secrets are the user's:** I never create, read or paste a production
   password or key.

## Design

### 1. Topology

Railway project `ledgerly` in the user's current workspace:

- **`ledgerly`** (root `Dockerfile`): start command
  `sh scripts/start-railway.sh`, which starts the worker and the API and
  exits when either stops. Volume mounted at `/data`; `STATE_DIR=/data`.
  Pre-deploy command `python -m invoice_processing.db.migrate` (owner role
  via `MIGRATE_DATABASE_URL`); `RUN_MIGRATIONS=0`.
- **`app`** (`frontend/Dockerfile`): `API_ORIGIN` = the `ledgerly` service's
  private address; build-time `NEXT_PUBLIC_FIREBASE_*` for `rasayan`.
  Public Railway domain; the browser sees one origin (the frontend proxies
  `/api`).
- **`Postgres`**: Railway's managed Postgres; bootstrap §1 → migrate as
  owner → §2, as in `deployment/README.md`.

On `rasayan`:

- Service account `ledgerly-prod@rasayan.iam.gserviceaccount.com` with
  `roles/aiplatform.user` and `roles/firebaseauth.admin` only. Its key is
  created by the user and pasted into Railway as `GOOGLE_CREDENTIALS_JSON`.
- The `app` service's Railway domain added to Identity Platform's
  authorized domains (keeping the existing ones).

Ceiling (recorded with a `ponytail` note in the start script): one
container, scaled up not out; documents on one volume. Upgrade path: S3
adapter for documents, Postgres or Pub/Sub queue, separate worker service.

### 2. Code

- **`paths.STATE_DIR`**: `Path(os.getenv("STATE_DIR"))` when set, else
  `DATA_DIR`. Used by `service.get_blobs` (local store root
  `STATE_DIR/blobs/<tenant>`), `queue.get_queue` (`STATE_DIR/queue`), and
  the worker heartbeat file. Tracked files stay under `DATA_DIR` (the
  image).
- **`preflight`**: in a deployed environment, accept `GCS_BUCKET` **or** a
  `STATE_DIR` that exists and is writable; refuse when neither, and name
  the problem ("STATE_DIR=/data does not exist — is the volume mounted?" /
  "…is not writable"). The `GCS_BUCKET` sentence stays for the neither case.
- **`scripts/start-railway.sh`**: POSIX `sh` (the image has no bash
  guarantee): start `python -m invoice_processing.worker` and
  `uvicorn invoice_processing.api.service:app --host 0.0.0.0 --port
  ${PORT:-8000}` in the background, wait until either exits, kill the
  other, exit non-zero. Credentials go through the Python bridge already on
  both entry paths.
- No other code changes: GCS and Pub/Sub keep working when configured.

### 3. Backups, documents, rollout

- **Backups:** Railway volume backups on the Postgres and `ledgerly`
  volumes (daily schedule, plus a manual one before risky changes). No PITR
  on Railway: **RPO up to 24 hours**, RTO 4 hours, drill every 6 months.
  `SECURITY.md` and `deployment/BACKUP_RESTORE.md` rewritten for Railway:
  restore a volume backup → bootstrap §2 → `restore_check` → re-purge from
  the register.
- **`deployment/README.md`** becomes the Railway runbook: services, every
  variable (secret ones marked "you set this"), commands, bootstrap order,
  smoke test.
- **Rollout, with approvals:** code PR merged on the user's "merge" →
  Railway project/services/volume/Postgres and non-secret settings (my
  steps, user go-ahead first: it starts billing) → `rasayan` service
  account and roles, authorized domain (mine, go-ahead first) → user
  creates the key, chooses role passwords, runs bootstrap §1, sets the
  secret variables, runs §2 after the first deploy, sets `SENTRY_DSN` →
  smoke test together (Google sign-in, sign-up, one invoice through the
  worker, `restore_check`).

### 4. Tests

TDD; each new test proved failing first.

- `STATE_DIR` set: documents and the queue land under it; unset: today's
  paths under `DATA_DIR`.
- Worker heartbeat path follows `STATE_DIR`.
- Preflight, deployed: `STATE_DIR` existing and writable → no storage
  problem; missing → refusal naming it; read-only → refusal naming it;
  neither it nor `GCS_BUCKET` → the existing refusal.
- `scripts/start-railway.sh` with stub commands (environment overrides for
  the two commands): exits non-zero when the worker stub exits, and when
  the API stub exits.

## Out of scope

The S3 adapter, a Postgres or Pub/Sub queue, a separate worker service, a
custom domain, migrating old data, and the Microsoft live test (its own
branch).

## Changes after the review (2026-10-14)

- Railway Postgres does offer point-in-time recovery (WAL to a Railway
  bucket); it is enabled at setup. Database RPO minutes, documents up to
  24 hours.
- `PORT=8000` and `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=60` on `ledgerly`.
- Work authorizations, the vendor master and purchase-order files follow
  `STATE_DIR` too (they are written at run time).
- Offboarding stops the worker with `WORKER_CMD=sleep infinity` instead of
  scaling the shared service to zero.
