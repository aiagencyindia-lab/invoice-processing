# google-genai client — design

Brainstormed with the user 2026-10-13, after the google-adk 2.x migration
(PR #33) merged; the gate and the five design sections were approved in turn.

## Why now

Every Gemini call still goes through `vertexai.generative_models`.
google-cloud-aiplatform 2.4.0 ships it deprecated: "will be removed on
June 24, 2026", and it warns on every `GenerativeModel()`. The replacement,
the `google-genai` SDK, is already in the lock (2.29.0, pulled by google-adk).
google-adk 2.x no longer depends on aiplatform, so after the swap both
`google-cloud-aiplatform` and the `vertexai` shim leave the runtime
dependencies.

## Where we start

Measured 2026-10-13 on `main` (`d20e3bb`):

- **Five modules build a model.** `general_invoice_agent.py` (`_model_for`,
  an `lru_cache`, `temperature=0, seed=42`; `Part.from_data` for PDFs; reads
  `.text` and two `usage_metadata` counters), `alf_engine.py`
  (`LLMActionExecutor._get_model`, temperature 0, two unguarded calls),
  `investigate_agent_reconst.py` (`_build_model`, imports from
  `vertexai.preview`, `temperature=0, response_mime_type="application/json"`,
  calls through `guarded_generate`), `core/rule_discoverer.py` (`_LLMClient`,
  no config, unguarded), `eval/eval.py` (`_init_llm`, no config, unguarded).
  Nothing reads candidates, finish reasons, safety ratings or system
  instructions; `call_gemini`'s `response_schema` is validated client-side and
  never reaches the SDK.
- **The guard needs one method.** `llm_guard.guarded_generate(model, prompt,
  budget)` calls `model.generate_content(contents)` in a four-thread pool,
  enforces its own timeout, retries three times, and classifies errors by
  substrings of `str(exc)` ("503", "UNAVAILABLE", "timeout", "429", "403",
  "billing", …). No code catches an SDK exception class.
- **The genai SDK, read from the installed source.** `Client(vertexai=True,
  project=…, location=…)` makes no network call at construction when both are
  given; one httpx client per `Client`, token refresh under a lock, so one
  client can serve the guard's pool. `HttpOptions.timeout` is in
  **milliseconds**. Retries are off unless `retry_options` is set.
  `types.Part.from_bytes(data=, mime_type=)` carries a PDF.
  `GenerateContentConfig` takes `temperature`, `seed`, `response_mime_type`
  and per-request `http_options`. The response keeps `.text` and
  `usage_metadata.prompt_token_count` / `candidates_token_count`, but `.text`
  returns `None` on a blocked or empty response rather than raising, and
  `candidates_token_count` may be `None`. `APIError.__str__` is
  `"{code} {status}. {details}"`, so "503 UNAVAILABLE" still appears;
  timeouts surface as raw `httpx.TimeoutException`, which matches "timeout".
  No warnings on import or construction.
- **Tests tied to the SDK shape.** `test_item_classification_determinism`
  patches `gia.GenerativeModel(name, generation_config=None)`;
  `test_vertex_deadline` checks by AST that only `_generate` calls
  `.generate_content` in the pipeline module; `test_critic_call_guard` and
  `scripts/verify_phase0.py` construct a real `RuleDiscoveryEngine`, which
  builds a real model offline; `test_repo_policy` asserts the aiplatform
  floor; `test_dual_import_modes` runs the three big modules as scripts.
  Every fake returns a plain `str` from `generate_content`.

## Decisions

### Scope: a client module and five substitutions, behaviour preserved

Each module keeps the generation settings it has today (the pipeline's seed,
the critic's JSON mime type, nothing for ALF, rule drafting and eval).
Harmonising seeds across modules is a separate decision with its own
benchmark run. The one behaviour change is a per-request timeout for every
caller, which today bounds only the guarded ones.

### Gate: a full live run plus a same-day A/B

The replay proves nothing about this change; only live calls do. The genai
SDK talks to Vertex's `v1beta1` endpoint with its own defaults (thinking on
the 2.5 models among them), so answers can shift with the same prompt.

- **Full 300-invoice live run on the branch**, scored against ground truth:
  decision accuracy at least **98.0%** (the last live figure, `after-9f8c923`),
  no undecided invoices; per-check table, extraction accuracy, cost and
  latency compared with that run. About 87 minutes on project `rasayan`.
- **Same-day A/B on the 14 invoices in `smoke.txt`**: `main` (old client, in
  a scratch worktree) and the branch, run within the same hour. Decisions and
  first failing steps must match; extraction differences are listed and
  explained. This attributes any change to the client rather than to model
  drift, which the 2026-10-13 adk work showed moves GSTIN reads week to week.

## Design

### 1. `invoice_processing/gemini.py`, the only module that knows the SDK

- `project_id()`: `PROJECT_ID`, then `GOOGLE_CLOUD_PROJECT`,
  `GOOGLE_CLOUD_PROJECT_ID`, `GCP_PROJECT`. `location()`: `LOCATION`, then
  `GOOGLE_CLOUD_REGION`, then `us-central1`. The chains the modules use today.
- `client()`: one process-wide `genai.Client(vertexai=True, project=…,
  location=…)`, cached. Built offline; shared by the guard's thread pool.
- `Model(name, temperature=0, seed=None, response_mime_type=None)` with one
  method, `generate_content(contents)`. It builds the `GenerateContentConfig`
  from its fields plus `http_options.timeout` of `VERTEX_TIMEOUT_SECONDS`
  (default 180 s) in milliseconds, and returns the SDK response. A response
  whose `.text` is `None` raises `GeminiEmptyResponse` naming the finish
  reason, so a blocked answer fails where it happened instead of as a
  `TypeError` in `clean_json_response`.
- `pdf_part(data)`: `types.Part.from_bytes(data=data, mime_type="application/pdf")`.
- `token_counts(response)`: `(prompt or 0, candidates or 0)`.
- No SDK retries. `llm_guard` keeps retry, deadline and classification, and
  its patterns need no change.

### 2. The five call sites

- `general_invoice_agent.py`: `_model_for(name, temperature)` returns
  `gemini.Model(name, temperature=temperature, seed=_GENERATION_SEED)`;
  `_generation_config` and the `GenerationConfig` import go;
  `Part.from_data(...)` becomes `gemini.pdf_part(...)`;
  `_ensure_gcp_initialized` keeps its project resolution and warning and
  stops calling `aiplatform.init`; token and cost sums use
  `gemini.token_counts`. The import guard keeps its `ImportError → exit(1)`
  shape with the new names.
- `alf_engine.py`: `gemini.Model(_get_llm_model(), temperature=0)`; its
  project check stays; `aiplatform.init` goes.
- `investigate_agent_reconst.py`: `_build_model(name, **kw)` returns
  `gemini.Model(name, **kw)`; the `vertexai.preview` import goes;
  `_ensure_gcp_initialized` keeps the `RuntimeError` for a missing project.
- `core/rule_discoverer.py`: `gemini.Model(get_llm_model())`.
- `eval/eval.py`: `gemini.Model(model_name)`; `_LLM_AVAILABLE` from importing
  `gemini`; the install hint names `google-genai`.
- `agent.py` already uses `google.genai.types`; untouched.
  `benchmark/harness/run.py`'s preflight uses `_model_for` and `_generate`;
  untouched.
- After the swap no tracked Python outside `gemini.py` mentions `vertexai`,
  `aiplatform`, `GenerativeModel`, `GenerationConfig` or `Part.from_data`.

### 3. Dependencies and documents

- `pyproject.toml`: add `google-genai>=2.0,<3`; remove
  `google-cloud-aiplatform` and `vertexai`. `uv lock`; aiplatform may remain
  only through the dev-only `agent-starter-pack`.
- `invoice_processing/requirements.txt`: `google-adk>=2.0,<3`,
  `google-genai>=2.0,<3`.
- `tests/test_repo_policy.py`: the aiplatform assertion becomes a genai one,
  plus "no `vertexai` among the runtime dependencies".
- `deployment/README.md` install lines name google-adk and google-genai.
  `.env.example` says the app's own client reads `GOOGLE_CLOUD_PROJECT` and
  `LOCATION`.

### 4. Tests

- New `tests/test_gemini.py`: the client builds offline from the test env;
  `Model.generate_content` passes name, temperature, seed, mime type and the
  millisecond timeout through to a recorded `client().models.generate_content`;
  a `[pdf_part, str]` request carries an inline PDF; an empty response
  raises `GeminiEmptyResponse`; `token_counts` treats `None` as 0; and a
  source policy test: no module under `invoice_processing/`, `eval/` or
  `benchmark/harness/` imports `vertexai` or `google.cloud.aiplatform`.
- `test_item_classification_determinism` patches `gemini.Model` instead of
  `gia.GenerativeModel`, keeping both caching assertions.
- `test_llm_unavailable` gains a case whose fake raises in the genai error
  format (`"503 UNAVAILABLE. {...}"`), pinning the classification.
- The AST check in `test_vertex_deadline`, the real-construction tests and
  the script-mode imports pass unchanged.

### 5. Verification

- Full Python suite, ruff, all nine gates, the CI Postgres list, the Alembic
  round trip, `scripts/audit_gate.py`.
- The A/B smoke and the full live run under *Gate*, written up in
  `benchmark/CHANGELOG.md` with versions, both tables, cost and latency.
- A browser check that an invoice detail renders on the new client.
- Whole-branch review on the `fable` model, one fix pass, CI green.

## Out of scope

- Harmonising seeds or thinking settings across modules.
- Routing ALF, rule drafting and eval through `guarded_generate` (they gain
  the request timeout only).
- Structured output via `response_schema` at the SDK (the client-side
  validation stays).
