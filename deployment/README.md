# Release (Railway + Postgres)

The API and worker connect as a least-privilege app role that owns nothing and
cannot run DDL. Migrations run in a release step as the owner role.
`invoice_processing/db/bootstrap.sql` has two sections: §1 before the first
migrate (roles, schema rights, default privileges), §2 after (grants on the
migrated tables, append-only audit trail, VERIFY). Run each section on its own;
`sed` cuts the file at the `-- §2` marker line.

`$ADMIN_URL` is the `postgres` user's connection to the application database
(not the `postgres` database): the grants and default privileges are
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

Railway service variables:

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | the app role's connection (API and worker) |
| `MIGRATE_DATABASE_URL` | the owner role's connection (release step only) |
| `RUN_MIGRATIONS` | `0` on BOTH the API and the worker service: each migrates at startup otherwise (`invoice_processing/worker/__main__.py` too), and as the app role that fails |

Pre-deploy command: `python -m invoice_processing.db.migrate`

The Dockerfile pins uv 0.5.11; `uv.lock` is written by uv 0.9.28. Check that
`uv sync --frozen` builds the image before the next deploy.

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
