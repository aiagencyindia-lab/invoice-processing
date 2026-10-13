# google-adk 2.x migration — design

Brainstormed with the user 2026-10-13; the scope, the gate and the five
design sections were approved in turn.

## Why now

`security/audit-allowlist.json` waives five Starlette 0.52.1 advisories
(PYSEC-2026-161, -248, -249, -2280, -2281) until **2026-12-12**. After that
the CI `audit` job fails. The fixes are in Starlette 1.x, which google-adk 1.x
pins out. Moving to google-adk 2.x lifts the pin.

## Where we start

Measured 2026-10-13 on `main` (`ea11ab1`):

- **ADK surface.** One `LlmAgent` in `invoice_processing/agent.py:528`, used
  by `adk web`. The API and worker import it through `__init__.py` but never
  call it. ADK 2.x's documented breaking changes (custom session services,
  `_run_async_impl`/`generate_content` overrides, appending events to a
  session directly) touch none of our code.
- **Gemini client.** Every model call goes through `vertexai.generative_models`
  (`general_invoice_agent.py`, `alf_engine.py`, `core/rule_discoverer.py`,
  `eval/eval.py`) or `vertexai.preview.generative_models`
  (`investigate_agent_reconst.py`). google-cloud-aiplatform **2.4.0 still
  ships both modules**: deprecated, logs a warning when a `GenerativeModel`
  is built, otherwise unchanged. `google-genai` is already in the lock as a
  transitive dependency (1.73.1).
- **Resolution.** `uv lock --dry-run` upgrading fastapi, starlette,
  google-adk, mcp, sse-starlette and google-cloud-aiplatform resolves cleanly
  to google-adk 2.11.0, aiplatform 2.4.0, starlette 1.7.0, fastapi 0.141.1,
  google-genai 2.29.0, google-auth 2.62.0. It removes `mcp`, `sse-starlette`,
  `pydantic-settings`, `pyjwt`, `sqlparse` and about twenty Google client
  packages nothing imports.
- **Direct Starlette imports.** `api/po_routes.py` uses
  `starlette.concurrency.run_in_threadpool` and
  `starlette.datastructures.UploadFile`; both exist in 1.x.
- **Dependabot.** It opened #32 (google-adk 2.11) and #31 (aiplatform 2.4)
  on 2026-10-12. On #32, `audit` and `frontend` passed, `e2e` failed one
  test (`a clerk corrects a scanned receipt and it clears the held invoice`,
  a `toMatch` on a non-string), and `test`/`postgres` never finished. #30,
  the grouped minor/patch bump, **downgraded** google-adk to 1.14.1, which
  carries PYSEC-2026-344 (high); its `audit` and `test` jobs failed.

## Decisions

### Scope: the dependency bump only

The deadline is the Starlette waiver, and the bump alone meets it. Moving the
five Gemini call sites to `google-genai` is a rewrite of the client layer
(the `llm_guard` retry logic matches exception text; eight tests patch
`GenerativeModel` or `generate_content` by name) and is its own sub-project
with its own benchmark run. Doing both at once would put two causes behind
any accuracy change.

### Gate: a live smoke plus a full replay

The client code path does not change, so the question is whether the newer
SDK still makes the same calls to Vertex, not whether the model answers
differently.

- Live on project `rasayan`: the harness preflight plus the 14 invoices in
  `benchmark/harness/smoke.txt`. Their decisions are compared with the same
  records in the last full run, `benchmark/runs/after-9f8c923`.
- `replay` over all 300 records of that run, scored: decision accuracy must
  stay at **98.0%** (what `replay-three-way-final` measures today).
- The full 300-invoice live run (87 minutes) only if the smoke disagrees
  with its recording.

**Amended during execution (2026-10-13).** Both comparisons were made
against a same-day control instead of the recording: `main` (`ea11ab1`) on
the old lock, run in a scratch worktree. The recording could not isolate
the libraries: the model's GSTIN reads have drifted since 2026-10-07, and
the 98.0% above was the replay figure for a different recording (`ecb9bbd`);
replays over `9f8c923` have scored 98.3% since `bca701a`. Result: the replay is identical
on all 300 records on both locks (98.3%), and the live smoke gives the same
14 decisions and failing steps on both locks. The full live run was not
needed. Details in `benchmark/CHANGELOG.md`, 2026-10-13.

## Design

### 1. Dependencies

- `pyproject.toml`: `google-adk>=2.0,<3`, `google-cloud-aiplatform>=2.0,<3`,
  `fastapi>=0.141`. The upper bounds are on the two majors only, so the next
  major arrives as a visible Dependabot PR rather than a silent resolve.
  `vertexai>=1.38.0` (a shim that depends on aiplatform) and every other
  floor stay as they are.
- `uv lock` with exactly the dry-run upgrade set, then `uv sync --frozen`.
  The lock must show starlette 1.7.x and no `mcp` or `sse-starlette`.
- `invoice_processing/requirements.txt` gets the same two floors; it is the
  install list Agent Engine reads (`deployment/README.md`).
- A test in `tests/test_repo_policy.py` pins the policy: both majors floored
  at 2, and no starlette below 1.0 in `uv.lock`. It fails on `main`.

### 2. Code

- No runtime change is expected. The full suite runs after `uv sync`; only
  what breaks is fixed, at the root.
- The SDK's deprecation warning stays visible. It is the reminder for the
  google-genai sub-project; silencing it hides the debt.
- The e2e failure seen on #32 is investigated on the new lock. If it
  reproduces, it is a real Starlette 1.x behaviour change (most likely in
  multipart uploads) and gets a failing test first. If it does not, it is
  recorded as flake with the run ids.

### 3. Audit gate

- `security/audit-allowlist.json` becomes `[]`. `load_allowlist` accepts an
  empty list and `gate` then fails on any high finding, which is the point.
- `python3 scripts/audit_gate.py` must print `Dependency audit: passed` with
  no `waived` lines.
- `SECURITY.md` loses the sentence about the Starlette waiver.

### 4. Dependabot

- #30, #31 and #32 are closed as superseded by this branch, with a one-line
  comment each (#30's also says why it was wrong). The nine frontend and
  GitHub Actions PRs (#21–#29) are left for a separate triage.

### 5. Documentation

- `deployment/README.md` names the new floors where it states versions.
- The Dockerfile pins uv 0.5.11 while the lock is written by uv 0.9.28. That
  predates this work and production is down; it is noted, not fixed.

## Verification

- Python suite (2678 passed, 4 skipped, plus the new test), ruff check and
  format, all nine gates in `benchmark/harness/gates.sh`.
- The CI Postgres list on a fresh `postgres:16` container, then the Alembic
  up/down round trip on a fresh database.
- `python3 scripts/audit_gate.py` passes with no waivers.
- The live smoke and the full replay described under *Gate*.
- A browser check on temporary port-8200/3200 launch configs: the login
  page, an invoice detail, and a supporting-document upload (the multipart
  path Starlette 1.x changed most).
- Whole-branch review on the `fable` model, one fix pass with tests first,
  then CI green on all five jobs.

## Out of scope

- Moving to `google-genai` (next sub-project).
- The nine other Dependabot PRs.
- The Dockerfile's uv version, Railway, and anything that needs production.
