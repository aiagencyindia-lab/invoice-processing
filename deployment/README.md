# Production (Railway + Google project `rasayan`)

Ledgerly runs on Railway; Gemini (Vertex AI) and sign-in (Identity
Platform) run on the Google project `rasayan`, which is shared with the
Rasayan web app. Design: docs/superpowers/specs/2026-10-14-railway-production-design.md.

## Layout

Railway project `ledgerly`:

| Service | Source | Settings |
|---|---|---|
| `ledgerly` (API + worker) | repo root, `Dockerfile` | start command `sh scripts/start-railway.sh`; pre-deploy `python -m invoice_processing.db.migrate`; volume mounted at `/data` |
| `app` (frontend) | `frontend/`, `frontend/Dockerfile` | public domain; proxies `/api` to `ledgerly` over the private network |
| `Postgres` | Railway template | its own volume |

`scripts/start-railway.sh` runs the worker and the API together and exits
when either stops, so Railway restarts the container; one container is the
ceiling (scale up, not out). Documents, the local job queue and the worker
heartbeat live on the `/data` volume (`STATE_DIR`); the image keeps the
tracked files (rules book, master data).

## Variables

"You" means the operator sets it by hand: it is a secret, and nobody else
creates, reads or pastes it. Mark those variables **sealed** in Railway, so
no one can read them back from the dashboard after they are set.

| Service | Variable | Value | Who |
|---|---|---|---|
| ledgerly | `ENV` | `production` | operator |
| ledgerly | `PORT` | `8000` (so `${{ledgerly.PORT}}` below resolves; the start script listens on it) | operator |
| ledgerly | `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` | `60`: the grace a redeploy gives before SIGKILL (Railway's default is 0, which kills an in-flight extraction) | operator |
| ledgerly | `STATE_DIR` | `/data` | operator |
| ledgerly | `RUN_MIGRATIONS` | `0` (the pre-deploy step migrates, as the owner) | operator |
| ledgerly | `GOOGLE_CLOUD_PROJECT`, `PROJECT_ID` | `rasayan` | operator |
| ledgerly | `GOOGLE_GENAI_USE_VERTEXAI` | `TRUE` | operator |
| ledgerly | `APPLY_IDENTITY_CLAIMS` | `1` | operator |
| ledgerly | `ALLOW_SELF_SIGNUP` | `1`, or unset to close sign-up | operator |
| ledgerly | `LOG_LEVEL` | `INFO`; `WARNING` to quieten | operator |
| ledgerly | `GOOGLE_CREDENTIALS_JSON` | the `ledgerly-prod@rasayan` key (JSON) | **you** |
| ledgerly | `DATABASE_URL` | app role: `postgresql+pg8000://ledgerly_app:<pw>@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/railway` | **you** |
| ledgerly | `MIGRATE_DATABASE_URL` | owner role, same host | **you** |
| ledgerly | `SENTRY_DSN` | the Sentry project's DSN | **you** |
| app | `API_ORIGIN` | `http://${{ledgerly.RAILWAY_PRIVATE_DOMAIN}}:${{ledgerly.PORT}}` (read at build time; needs `PORT` set on `ledgerly`) | operator |
| app | `NEXT_PUBLIC_FIREBASE_API_KEY`, `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN` (`rasayan.firebaseapp.com`), `NEXT_PUBLIC_FIREBASE_PROJECT_ID` (`rasayan`) | Identity Platform's public web values (build time) | operator |

`LOG_FORMAT` unset gives JSON under `ENV=production`. Railway sets
`RAILWAY_GIT_COMMIT_SHA`, which becomes the Sentry release.

## Google side (`rasayan`)

- Service account `ledgerly-prod@rasayan.iam.gserviceaccount.com` with
  `roles/aiplatform.user` and `roles/firebaseauth.admin`, nothing else.
  **You** create its JSON key and paste it into `GOOGLE_CREDENTIALS_JSON`.
- Identity Platform → Settings → Authorized domains: add the `app`
  service's Railway domain (keep the existing ones).

## Database bootstrap

The API and worker connect as a least-privilege app role that owns nothing
and cannot run DDL. Migrations run in the pre-deploy step as the owner role.
`invoice_processing/db/bootstrap.sql` has two sections: §1 before the first
migrate (roles, schema rights, default privileges), §2 after (grants on the
migrated tables, append-only audit trail, VERIFY). Run each section on its
own; `sed` cuts the file at the `-- §2` marker line.

`$ADMIN_URL` is the `postgres` user's connection to the application
database (Railway: the database `railway`, through the service's public TCP
proxy, from a trusted machine): the grants and default privileges are
per-database, so run elsewhere they protect nothing.

1. Roles, as `postgres` (§1):
   `sed -n '1,/^-- §2/p' invoice_processing/db/bootstrap.sql | psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -v owner=ledgerly_owner -v app=ledgerly_app -v owner_pw=... -v app_pw=... -f -`
2. Migrate as the owner:
   `MIGRATE_DATABASE_URL=postgresql+pg8000://ledgerly_owner:...@host/db python -m invoice_processing.db.migrate`
3. Grants, as `postgres` (§2, safe to re-run; re-run it after every migrate):
   `sed -n '/^-- §2/,$p' invoice_processing/db/bootstrap.sql | psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -v owner=ledgerly_owner -v app=ledgerly_app -f -`
4. Read the VERIFY output that ends step 3: the app role cannot UPDATE
   `audit_events` but can INSERT, and neither role is superuser or bypasses RLS.

Re-run step 3 after every migrate, not just the first: a migration that
creates or recreates a table gets the app's grants through §1's default
privileges, so a recreated `audit_events` (or `alembic_version`) would be
writable by the app again until §2 revokes it.

Order on Railway: §1 → set the secret variables → first deploy (pre-deploy
migrates as the owner) → §2 → read VERIFY.

## Deploy settings

The Railway project is described in `.railway/railway.ts` (Railway
Infrastructure as Code): both services, Postgres, the volumes, the API's start
command, pre-deploy migrate, `/healthz` health check and 60 s draining, and the
frontend's `frontend` root directory. Variables are unmanaged: they stay in
Railway and never appear in the file.

Railway does not apply it on push. To change a setting, edit the file in a PR,
then from a checkout linked to the project (`railway link`), with Railway's
SDK installed (`npm install railway`, outside the repo is fine):

```bash
railway config plan     # must show only the change you meant
railway config apply
```

An apply covers the whole project: anything live that the file does not list
(a database or volume added in the dashboard since) is planned for
**deletion**. Run `railway config pull --variables unmanaged --force` before
editing so the file starts from what is live, read the plan, and never pass
`--yes --confirm-destructive`.

`railway config plan` against the live project showed no changes when the file
was added (2026-10-26). Railway no longer accepts `railway.json` /
`railway.toml` config files. The image always builds from the root
`Dockerfile`.

## Health

`GET /healthz` needs no sign-in: 200 `{"status": "ok"}` when the database
answers, 503 `{"status": "unavailable"}` otherwise. Set it as the
Healthcheck Path of the API service so Railway only switches traffic to a
deploy whose database answers. The start script gates a deploy too:
preflight refusing the configuration, or either process exiting, stops the
container, and Railway marks the deploy failed. Check the API service's
deploy logs for "Refusing to start" after any variable change. Adding
`connect_timeout=5` to `DATABASE_URL` keeps a hung database from holding a
probe.

## Smoke test

1. Open the `app` domain → Continue with Google → sign up an organisation.
2. Upload one PDF; the worker banner is green and the invoice is processed.
3. `python -m invoice_processing.db.restore_check --restored <owner url>` →
   `PASS`.
4. The `ledgerly` logs show preflight passing and the worker starting.

The Dockerfile pins uv 0.5.11 and `uv.lock` is written by uv 0.9.28; uv
0.5.11 passes `uv lock --check` on it (2026-10-14). If a build fails on the
lock, pin the uv image to the lock's writer.

---

# Deployment Guide

Deploy an ADK agent to Agent Runtime and optionally register it on Gemini Enterprise.

**Reference:** [Google Agents CLI](https://github.com/google/agents-cli)

---

## Prerequisites

- Python 3.10+
- [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) (`gcloud` CLI)
- A GCP project with Vertex AI API enabled
- Python packages: `google-adk` 2.x, `google-genai` 2.x, `python-dotenv`
- (Optional) `google-agents-cli` — required only for Gemini Enterprise registration

```bash
pip install "google-adk>=2,<3" "google-genai>=2,<3" python-dotenv
```

---

## Step 1: Authenticate and set quota project

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project <YOUR_PROJECT_ID>
```

---

## Step 2: Prepare the agent package

Agent Runtime uploads only the agent directory (the folder containing `agent.py` and `__init__.py`).

**Key requirements:**

1. **`requirements.txt`** must exist **inside the agent package directory** (not the project root). Agent Runtime uses it to install dependencies in the container.
2. **`.env`** file in the agent directory is auto-detected and deployed. Use it for runtime env vars (`PROJECT_ID`, `LOCATION`, etc.). Alternatively, use `--env_file` to point to a different file.
3. **All imports must be relative** (e.g., `from .tools.tools import ...`). Agent Runtime renames the package directory during deployment, which breaks absolute imports.
4. **Minimize the `data/` directory** — any files inside the agent package are uploaded. Remove runtime outputs (logs, cache, temp files) before deploying.
5. **Environment variable access must be lazy** — Agent Runtime sets env vars *after* module import. Any `os.getenv()` calls at module level will return `None`. Use a lazy initialization pattern (call `os.getenv()` inside a function on first use, not at import time).

### `.env` file template

Create a `.env` file in the agent package directory:

```bash
GOOGLE_GENAI_USE_VERTEXAI=TRUE
PROJECT_ID=<YOUR_PROJECT_ID>
GOOGLE_CLOUD_PROJECT=<YOUR_PROJECT_ID>
LOCATION=<YOUR_REGION>
```

> A `.env.example` is provided in the agent directory. Copy it to `.env` and fill in your values.

---

## Step 3: Deploy to Agent Runtime

```bash
adk deploy agent_engine \
  --project=<YOUR_PROJECT_ID> \
  --region=<YOUR_REGION> \
  --display_name="<AGENT_DISPLAY_NAME>" \
  --description="<AGENT_DESCRIPTION>" \
  <PATH_TO_AGENT_PACKAGE>
```

On success, the CLI outputs:

```
✅ Created agent engine: projects/<PROJECT_NUMBER>/locations/<REGION>/reasoningEngines/<RESOURCE_ID>
```

**Save the `<RESOURCE_ID>`** — you need it for updates and Gemini Enterprise registration.

---

## Step 4: Update an existing deployment

To push changes without creating a new instance:

```bash
adk deploy agent_engine \
  --project=<YOUR_PROJECT_ID> \
  --region=<YOUR_REGION> \
  --agent_engine_id=<RESOURCE_ID> \
  <PATH_TO_AGENT_PACKAGE>
```

---

## Step 5: Register on Gemini Enterprise (optional)

Requires `google-agents-cli`:

```bash
pip install google-agents-cli
```

### Option A: Interactive (recommended)

```bash
agents-cli publish gemini-enterprise
```

The CLI will:
1. Auto-detect the Agent Runtime ID from `deployment_metadata.json`
2. List available Gemini Enterprise apps in your project
3. Fetch the agent's display name and description
4. Register the agent as a tool in Gemini Enterprise
5. Print a console link to view the registered agent

### Option B: Non-interactive

```bash
ID="projects/<PROJECT_NUMBER>/locations/global/collections/default_collection/engines/<GE_APP_ID>" \
AGENT_ENGINE_ID="projects/<PROJECT_NUMBER>/locations/<REGION>/reasoningEngines/<RESOURCE_ID>" \
GEMINI_DISPLAY_NAME="<AGENT_DISPLAY_NAME>" \
GEMINI_DESCRIPTION="<AGENT_DESCRIPTION>" \
agents-cli publish gemini-enterprise
```

Find your Gemini Enterprise App ID in the Google Cloud Console under Gemini Enterprise settings.

---

## Step 6: Verify

1. **Console check:** Open the Gemini Enterprise console and confirm your agent appears as an available tool
2. **Chat test:** Select the agent and send a test message
3. **Logs:** If something goes wrong, check Agent Runtime logs:

```bash
gcloud logging read \
  "resource.type=aiplatform.googleapis.com/ReasoningEngine" \
  --project=<YOUR_PROJECT_ID> \
  --limit=20 \
  --format="table(timestamp,textPayload)"
```

---

## Updating configuration post-deployment

When rules, data files, or agent instructions change:

1. Update the relevant files in the agent package
2. Re-deploy using the update command (Step 4)

No need to re-register on Gemini Enterprise — the registration points to the Agent Runtime instance, which is updated in place.

---

## Troubleshooting

### "failed to start and cannot serve traffic"

| Cause | Fix |
|-------|-----|
| Missing `requirements.txt` in agent dir | Add `requirements.txt` inside the agent package directory |
| Missing env vars | Ensure `.env` exists in the agent dir with `PROJECT_ID` and `GOOGLE_CLOUD_PROJECT` |
| Large package size | Remove runtime outputs from `data/` before deploying |
| Absolute imports | Convert all imports to relative (`.` / `..`) — Agent Runtime renames the package |
| Module-level `os.getenv()` | Defer env var reads to call time using lazy initialization |

### ADC quota warning

```bash
gcloud auth application-default set-quota-project <YOUR_PROJECT_ID>
```

---

## Further Reading

- [Google Agents CLI — Getting Started](https://github.com/google/agents-cli)
- [Google Agents CLI — Deployment](https://github.com/google/agents-cli)
- [ADK Deploy CLI Reference](https://google.github.io/adk-docs/deploy/agent-engine/)
