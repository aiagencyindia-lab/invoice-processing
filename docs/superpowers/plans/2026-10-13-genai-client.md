# google-genai Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every `vertexai.generative_models` use with the `google-genai` SDK through one small client module, keeping each caller's generation settings, and prove it with a same-day A/B smoke and a full 300-invoice live run.

**Architecture:** `invoice_processing/gemini.py` is the only module that imports the SDK. It exposes `Model(name, temperature, seed, response_mime_type).generate_content(contents)`, so `llm_guard.guarded_generate`, `_model_for`'s cache, the benchmark preflight and every test fake keep their shape. Five modules swap their `GenerativeModel` for `gemini.Model`; `google-cloud-aiplatform` and `vertexai` leave the runtime dependencies.

**Tech Stack:** Python 3.12, uv (`uv.lock`), google-genai 2.29.0 (already locked), pytest, ruff; `benchmark/harness/run.py` for the live gate on GCP project `rasayan`.

**Spec:** `docs/superpowers/specs/2026-10-13-genai-client-design.md`

## Global Constraints

- Always `./.venv/bin/python`; tests with `-p no:cacheprovider`. `uv sync --frozen` after any checkout or lock change.
- Never `git commit -a`; stage explicit paths (`.impeccable/hook.cache.json` is modified and never committed).
- Behaviour-preserving settings per module: pipeline `temperature=0, seed=_GENERATION_SEED`; ALF `temperature=0`; critic `temperature=0, response_mime_type="application/json"`; rule drafting and eval no config. No seed added where there is none.
- The only behaviour change: every request carries `http_options.timeout = VERTEX_TIMEOUT_SECONDS * 1000` (milliseconds; default 180 s). No SDK retries (`retry_options` unset); `llm_guard` is untouched.
- After the swap, no tracked Python under `invoice_processing/`, `eval/`, `benchmark/harness/`, `scripts/` imports `vertexai` or `google.cloud.aiplatform`; `google.genai` is imported only by `invoice_processing/gemini.py` and `invoice_processing/agent.py`.
- `pyproject.toml` after this work: `google-genai>=2.0,<3` present; `google-cloud-aiplatform` and `vertexai` absent from `[project].dependencies`.
- Local LLM runs: `CLOUD_SQL_INSTANCE="" GCS_BUCKET="" PUBSUB_TOPIC="" PROJECT_ID=rasayan GOOGLE_CLOUD_PROJECT=rasayan`. Never touch Railway, Cloud SQL, GCS.
- Commit trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` (the harness names the model; use what it says at execution time).
- Ledger: `.superpowers/sdd/2026-10-13-genai-client/progress.md`; delete at the end.

## Review Focus

1. A blocked or empty model answer (safety stop, zero candidates): the pipeline must fail that call with a clear error, not a `TypeError` from `clean_json_response(None)`. Pinned in Task 1 (`test_an_empty_response_is_an_error`).
2. A response whose `usage_metadata.candidates_token_count` is `None`: metrics and cost must not raise. Pinned in Task 1 (`test_token_counts_treat_missing_as_zero`) and used in Task 2.
3. A genai `ServerError` ("503 UNAVAILABLE. {...}") inside the guard: retried, then `LlmUnavailable`, exactly as the old SDK's message was. Pinned in Task 2 (`test_a_genai_server_error_is_retried_then_unavailable`).
4. A test or script that builds a real `RuleDiscoveryEngine` on a machine with no gcloud config: the client must construct offline from `GOOGLE_CLOUD_PROJECT`. Pinned in Task 1 (`test_the_client_builds_offline_from_env`) and exercised by the existing `test_critic_call_guard::TestDiscoveryCacheKey` in Task 3.
5. An httpx timeout at the SDK layer (the new per-request timeout firing before the guard's): `str(exc)` of `httpx.ReadTimeout` contains no "timeout" word in some versions, so the guard could re-raise it as a document error. Pinned in Task 2 (`test_an_httpx_timeout_is_infrastructure`), which asserts classification by exception type name as well as message.

---

### Task 1: `invoice_processing/gemini.py` and its tests

**Files:**
- Create: `invoice_processing/gemini.py`
- Create: `tests/test_gemini.py`

**Interfaces:**
- Produces: `gemini.project_id() -> str | None`, `gemini.location() -> str`, `gemini.client() -> google.genai.Client` (lru_cache), `gemini.timeout_ms() -> int`, `class gemini.Model(name: str, temperature: float = 0, seed: int | None = None, response_mime_type: str | None = None)` with `.name` and `.generate_content(contents) -> GenerateContentResponse`, `gemini.pdf_part(data: bytes) -> types.Part`, `gemini.token_counts(response) -> tuple[int, int]`, `class gemini.GeminiEmptyResponse(RuntimeError)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gemini.py`:

```python
"""The one module that talks to the Gemini SDK.

Everything else sees `Model.generate_content(contents)`, the same surface the
old vertexai object had, so the guard, the cache and the fakes do not change.
"""

import re
from pathlib import Path

import pytest
from google.genai import types

from invoice_processing import gemini

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def fresh_client():
    gemini.client.cache_clear()
    yield
    gemini.client.cache_clear()


class Recorder:
    """Stands in for client().models; remembers one call."""

    def __init__(self, response=None):
        self.calls = []
        self.response = response or types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(parts=[types.Part(text="ok")])
                )
            ],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=7, candidates_token_count=3
            ),
        )

    def generate_content(self, *, model, contents, config):
        self.calls.append((model, contents, config))
        return self.response


def test_the_client_builds_offline_from_env(monkeypatch):
    """conftest sets GOOGLE_CLOUD_PROJECT; nothing else is configured."""
    monkeypatch.delenv("PROJECT_ID", raising=False)
    monkeypatch.delenv("LOCATION", raising=False)
    client = gemini.client()
    assert client.vertexai is True
    assert client._api_client.project == "test-project-not-real"
    assert client._api_client.location == "us-central1"


def test_project_id_prefers_project_id_then_the_aliases(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "first")
    assert gemini.project_id() == "first"
    monkeypatch.delenv("PROJECT_ID")
    assert gemini.project_id() == "test-project-not-real"


def test_the_settings_reach_the_request(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(gemini.client(), "models", rec)
    monkeypatch.setenv("VERTEX_TIMEOUT_SECONDS", "42")
    model = gemini.Model(
        "gemini-2.5-flash", temperature=0, seed=42, response_mime_type="application/json"
    )
    response = model.generate_content("hello")
    assert response.text == "ok"
    name, contents, config = rec.calls[0]
    assert name == "gemini-2.5-flash"
    assert contents == "hello"
    assert (config.temperature, config.seed) == (0, 42)
    assert config.response_mime_type == "application/json"
    assert config.http_options.timeout == 42_000  # milliseconds


def test_unset_settings_are_not_sent(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(gemini.client(), "models", rec)
    gemini.Model("gemini-2.5-pro").generate_content("p")
    config = rec.calls[0][2]
    assert config.seed is None and config.response_mime_type is None
    assert config.temperature == 0


def test_a_pdf_travels_as_inline_data(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(gemini.client(), "models", rec)
    gemini.Model("m").generate_content([gemini.pdf_part(b"%PDF-1.4"), "read it"])
    contents = rec.calls[0][1]
    assert contents[0].inline_data.mime_type == "application/pdf"
    assert contents[0].inline_data.data == b"%PDF-1.4"
    assert contents[1] == "read it"


def test_an_empty_response_is_an_error(monkeypatch):
    rec = Recorder(types.GenerateContentResponse(candidates=[]))
    monkeypatch.setattr(gemini.client(), "models", rec)
    with pytest.raises(gemini.GeminiEmptyResponse):
        gemini.Model("m").generate_content("p")


def test_token_counts_treat_missing_as_zero():
    response = types.GenerateContentResponse(
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=11, candidates_token_count=None
        )
    )
    assert gemini.token_counts(response) == (11, 0)
    assert gemini.token_counts(types.GenerateContentResponse()) == (0, 0)


def test_only_this_module_imports_the_sdk():
    """One SDK-aware module. agent.py builds the ADK LlmAgent with genai
    types and is the other permitted importer."""
    old = re.compile(r"^\s*(from|import)\s+(vertexai|google\.cloud\.aiplatform)\b", re.M)
    new = re.compile(r"^\s*(from|import)\s+google\.genai\b", re.M)
    offenders, genai_users = [], []
    for folder in ("invoice_processing", "eval", "benchmark/harness", "scripts"):
        for path in (REPO / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if old.search(text):
                offenders.append(str(path.relative_to(REPO)))
            if new.search(text):
                genai_users.append(str(path.relative_to(REPO)))
    assert offenders == [], offenders
    assert sorted(genai_users) == [
        "invoice_processing/agent.py",
        "invoice_processing/gemini.py",
    ]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_gemini.py -q -p no:cacheprovider 2>&1 | tail -3`
Expected: collection error `ModuleNotFoundError: No module named 'invoice_processing.gemini'` (or `ImportError: cannot import name 'gemini'`).

- [ ] **Step 3: Prove the suite fails on `main` with this file (scratch worktree)**

```bash
git worktree add /private/tmp/claude-501/genai-main-t1 main
cp tests/test_gemini.py /private/tmp/claude-501/genai-main-t1/tests/
(cd /private/tmp/claude-501/genai-main-t1 && /Users/vineet/Downloads/ADK/invoice-processing/.venv/bin/python -m pytest tests/test_gemini.py -q -p no:cacheprovider 2>&1 | tail -1)
git worktree remove --force /private/tmp/claude-501/genai-main-t1 && git worktree prune
```
Expected: the same import error. Ledger it.

- [ ] **Step 4: Write the module**

Create `invoice_processing/gemini.py`:

```python
"""The Gemini client, and the only module that imports its SDK.

Every caller holds a `Model` and calls `generate_content(contents)`, the same
one-method surface the old `vertexai.generative_models.GenerativeModel` had,
so `llm_guard.guarded_generate`, the pipeline's model cache, the benchmark
preflight and every test fake are unchanged by the SDK underneath.

The SDK is `google-genai`, pointed at Vertex AI. It makes no network call
when built with a project and a location; one client serves the whole
process, including the guard's thread pool (the SDK keeps one httpx client
and refreshes credentials under a lock). It retries nothing on its own: the
guard owns retry, deadline and the "is this an outage" classification.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from google import genai
from google.genai import types

DEFAULT_TIMEOUT_SECONDS = 180.0


class GeminiEmptyResponse(RuntimeError):
    """The model answered with no text: blocked, filtered, or no candidate.

    The SDK returns `None` from `.text` in that case rather than raising, and
    the first thing every caller does with the text is parse it. Failing
    here names the reason; failing there is a TypeError on `None`.
    """


def project_id() -> str | None:
    """The project the pipeline, ALF and the critic have always read."""
    return (
        os.getenv("PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GOOGLE_CLOUD_PROJECT_ID")
        or os.getenv("GCP_PROJECT")
    )


def location() -> str:
    return os.getenv("LOCATION") or os.getenv("GOOGLE_CLOUD_REGION") or "us-central1"


def timeout_ms() -> int:
    """VERTEX_TIMEOUT_SECONDS, in the milliseconds the SDK wants."""
    seconds = float(os.getenv("VERTEX_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
    return int(seconds * 1000)


# ponytail: one process-wide client; per-tenant credentials would key the cache.
@lru_cache(maxsize=1)
def client() -> genai.Client:
    return genai.Client(vertexai=True, project=project_id(), location=location())


def pdf_part(data: bytes) -> types.Part:
    return types.Part.from_bytes(data=data, mime_type="application/pdf")


def token_counts(response) -> tuple[int, int]:
    """(prompt, candidates) token counts; the SDK leaves either as None."""
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return 0, 0
    return usage.prompt_token_count or 0, usage.candidates_token_count or 0


@dataclass(frozen=True)
class Model:
    """A model name plus the generation settings a caller wants.

    Frozen so the pipeline's `lru_cache` keyed on (name, temperature) hands
    out a shared, immutable object.
    """

    name: str
    temperature: float = 0
    seed: int | None = None
    response_mime_type: str | None = None

    def generate_content(self, contents):
        config = types.GenerateContentConfig(
            temperature=self.temperature,
            seed=self.seed,
            response_mime_type=self.response_mime_type,
            http_options=types.HttpOptions(timeout=timeout_ms()),
        )
        response = client().models.generate_content(
            model=self.name, contents=contents, config=config
        )
        if response.text is None:
            candidate = (response.candidates or [None])[0]
            reason = getattr(candidate, "finish_reason", None) or getattr(
                response.prompt_feedback, "block_reason", None
            )
            raise GeminiEmptyResponse(
                f"{self.name} returned no text (reason: {reason or 'no candidates'})"
            )
        return response
```

- [ ] **Step 5: Run the new tests**

Run: `./.venv/bin/python -m pytest tests/test_gemini.py -q -p no:cacheprovider 2>&1 | tail -3`
Expected: 8 passed, 1 failed — `test_only_this_module_imports_the_sdk` fails listing the five modules that still import vertexai/aiplatform. That one goes green in Task 3; the other eight must pass now.

- [ ] **Step 6: Lint and commit**

```bash
./.venv/bin/python -m ruff check invoice_processing/gemini.py tests/test_gemini.py && ./.venv/bin/python -m ruff format invoice_processing/gemini.py tests/test_gemini.py
git add invoice_processing/gemini.py tests/test_gemini.py
git commit -m "Put the Gemini SDK behind one module with the old one-method surface

google-genai replaces the deprecated vertexai.generative_models. Model
keeps generate_content(contents), so the guard, the cache and every fake
stay as they are; the module adds the per-request timeout the SDK lacks
by default, raises when an answer has no text, and reads token counts
that the SDK may leave as None.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: The acting pipeline on the new client

**Files:**
- Modify: `invoice_processing/shared_libraries/acting/general_invoice_agent.py` (imports ~48-63; `_generation_config` ~158-163; `_model_for` ~412-428; `_ensure_gcp_initialized` ~451-482; `extract_pdf_with_gemini` ~840; `call_gemini` ~987-997; `call_gemini_with_pdf` ~1024-1040)
- Modify: `tests/test_item_classification_determinism.py:165-196`
- Modify: `tests/test_llm_unavailable.py` (append two tests)

**Interfaces:**
- Consumes: `gemini.Model`, `gemini.pdf_part`, `gemini.token_counts` from Task 1.
- Produces: `gia._model_for(name, temperature) -> gemini.Model` (still `lru_cache`); `gia.gemini` attribute available for patching.

- [ ] **Step 1: Write the failing tests**

In `tests/test_item_classification_determinism.py` replace the two patches of `gia.GenerativeModel`:

```python
    def test_the_same_model_and_temperature_is_reused(self, monkeypatch):
        built = []

        class _Model:
            def __init__(self, name, temperature=0, seed=None, response_mime_type=None):
                built.append((name, temperature, seed))

        monkeypatch.setattr(gia.gemini, "Model", _Model)
        gia._model_for.cache_clear()

        first = gia._model_for("gemini-2.5-flash", 0)
        second = gia._model_for("gemini-2.5-flash", 0)

        assert first is second
        assert built == [("gemini-2.5-flash", 0, gia._GENERATION_SEED)]

    def test_a_different_temperature_is_a_different_model(self, monkeypatch):
        """The object is stateless; the configuration is not."""
        monkeypatch.setattr(
            gia.gemini, "Model", lambda name, **kw: object()
        )
        gia._model_for.cache_clear()

        assert gia._model_for("gemini-2.5-flash", 0) is not gia._model_for(
            "gemini-2.5-flash", 0.7
        )
```

Append to `tests/test_llm_unavailable.py` (inside `class TestTheGuard`):

```python
    def test_a_genai_server_error_is_retried_then_unavailable(self):
        """google-genai formats errors as '<code> <status>. <details>'."""
        from google.genai import errors

        exc = errors.ServerError(
            503,
            {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}},
        )
        model = Model(exc)
        with pytest.raises(LlmUnavailable):
            guarded_generate(model, "p", CallBudget(deadline_seconds=30))
        assert model.calls == 3

    def test_an_httpx_timeout_is_infrastructure(self):
        """The SDK's own per-request timeout fires as httpx.ReadTimeout, whose
        message is the URL or nothing at all. It is still an outage."""
        import httpx

        model = Model(httpx.ReadTimeout(""))
        with pytest.raises(LlmUnavailable):
            guarded_generate(model, "p", CallBudget(deadline_seconds=30))
        assert model.calls == 3
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_item_classification_determinism.py::TestTheModelFactoryIsShared tests/test_llm_unavailable.py::TestTheGuard -q -p no:cacheprovider 2>&1 | tail -4`
Expected: the two determinism tests fail with `AttributeError: ... has no attribute 'gemini'`; `test_a_genai_server_error_is_retried_then_unavailable` passes already (its `str()` contains "503 UNAVAILABLE"); `test_an_httpx_timeout_is_infrastructure` fails (re-raised `ReadTimeout`). If the ServerError constructor signature differs in the installed version (`ServerError(code, response_json, response=None)`), read `google/genai/errors.py` and adjust the two positional args; the assertion stands.

- [ ] **Step 3: Make the guard see an httpx timeout as infrastructure**

In `invoice_processing/llm_guard.py`, the `_matches(message, …)` calls classify by `str(exc)`. Add the exception's class name to what is matched, in the one place that builds `message`:

```python
        except Exception as exc:  # classified below, not swallowed
            last_error = exc
            # The SDK's own timeout arrives as httpx.ReadTimeout with an
            # empty message; the class name says what the text does not.
            message = f"{type(exc).__name__}: {exc}"
```

`_INFRASTRUCTURE_ERROR_PATTERNS` already contains `"timeout"` and `"connection"` (case-insensitive), so `ReadTimeout`, `ConnectTimeout` and `ConnectError` now match. Run the guard's own tests: `./.venv/bin/python -m pytest tests/test_critic_call_guard.py tests/test_llm_unavailable.py tests/test_vertex_deadline.py -q -p no:cacheprovider`. Expected: all pass except the two determinism tests not in this set.

- [ ] **Step 4: Swap the pipeline's SDK use**

In `general_invoice_agent.py`:

Imports (both the package try-block and the script fallback): remove `from google.cloud import aiplatform` and the `vertexai.generative_models` import; add `gemini` to the sibling imports:

```python
try:
    from ... import (
        bank_check,
        confidence,
        duplicates,
        extraction_cleanup,
        gemini,
        po_book,
        ...
```
and in the `except ImportError` fallback `from invoice_processing import (..., gemini, ...)`. Change the third-party try block's error hint to `pip install google-genai pydantic pdfplumber python-dotenv`.

Delete `_generation_config` (and its docstring); keep `_GENERATION_SEED` and its comment.

`_model_for`:
```python
@lru_cache(maxsize=32)
def _model_for(model_name: str, temperature: float) -> gemini.Model:
    """One model object per (name, temperature). ...(keep docstring)..."""
    return gemini.Model(model_name, temperature=temperature, seed=_GENERATION_SEED)
```

`_ensure_gcp_initialized`: delete the `else: aiplatform.init(...)` branch; the `if not PROJECT_ID: print(...)` warning stays. Update the docstring's "Vertex AI" wording to "the model client".

PDF parts, both places:
```python
    pdf_part = gemini.pdf_part(pdf_data)
```

Token accounting in `call_gemini` and `call_gemini_with_pdf`:
```python
    prompt_tokens, completion_tokens = gemini.token_counts(response)
    metrics["llm_calls"] += 1
    metrics["total_tokens"]["prompt"] += prompt_tokens
    metrics["total_tokens"]["completion"] += completion_tokens

    pricing = MODEL_PRICING.get(
        model_name.split("/")[-1], MODEL_PRICING["gemini-2.5-flash"]
    )
    cost = (prompt_tokens / 1_000_000) * pricing["input"]
    cost += (completion_tokens / 1_000_000) * pricing["output"]
    metrics["total_cost_usd"] += cost
```

Update the `VERTEX_TIMEOUT_SECONDS` comment: the SDK now also gets this as its per-request timeout (`gemini.timeout_ms`), so a stalled socket is cut at the SDK as well as at the guard.

- [ ] **Step 5: Run the tests**

```bash
./.venv/bin/python -m pytest tests/test_item_classification_determinism.py tests/test_llm_unavailable.py tests/test_vertex_deadline.py tests/test_critic_call_guard.py tests/test_pipeline_progress.py tests/test_document_taxonomy.py tests/test_extraction_cleanup.py tests/test_supporting_docs.py tests/test_dual_import_modes.py -q -p no:cacheprovider 2>&1 | tail -3
grep -n 'aiplatform\|vertexai\|GenerativeModel\|GenerationConfig\|Part.from_data' invoice_processing/shared_libraries/acting/general_invoice_agent.py
```
Expected: all passed; grep prints nothing.

- [ ] **Step 6: Lint and commit**

```bash
./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format --check .
git add invoice_processing/shared_libraries/acting/general_invoice_agent.py invoice_processing/llm_guard.py tests/test_item_classification_determinism.py tests/test_llm_unavailable.py
git commit -m "Run the acting pipeline's Gemini calls through the genai client

_model_for builds a gemini.Model with the same temperature and seed; PDFs
travel as inline parts; token counts tolerate the SDK's None. The guard
now classifies by exception class as well as message, because the SDK's
own timeout arrives as httpx.ReadTimeout with nothing in its text.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: ALF, the critic, rule drafting and eval

**Files:**
- Modify: `invoice_processing/shared_libraries/alf_engine.py:964-999` (`LLMActionExecutor._get_model`), and its sibling-import blocks (~55-70 and the script fallback) to import `gemini`
- Modify: `invoice_processing/shared_libraries/investigation/investigate_agent_reconst.py:48-58` (imports), `:177-220` (`_ensure_gcp_initialized`, `_build_model`), and the re-export try/except at `:374-393` to also import `gemini`
- Modify: `invoice_processing/core/rule_discoverer.py:58-79`
- Modify: `eval/eval.py:44-53`, `:73-95`, `:~1609`
- Test: `tests/test_gemini.py::test_only_this_module_imports_the_sdk` (the RED from Task 1), plus `tests/test_critic_call_guard.py`, `tests/test_rule_drafting.py`, `tests/test_alf_in_the_worker.py`, `tests/test_dual_import_modes.py`

**Interfaces:**
- Consumes: `gemini.Model` from Task 1.

- [ ] **Step 1: Confirm the RED**

Run: `./.venv/bin/python -m pytest tests/test_gemini.py::test_only_this_module_imports_the_sdk -q -p no:cacheprovider 2>&1 | grep -E 'offenders|assert' | head -3`
Expected: FAIL listing `alf_engine.py`, `investigate_agent_reconst.py`, `core/rule_discoverer.py`, `eval/eval.py`.

- [ ] **Step 2: `alf_engine.py`**

Add `gemini` to the package import block (`from ..  import gemini` next to `from ..context import promote_convenience_fields`) and to the script fallback (`from invoice_processing import gemini`). Replace `_get_model`:

```python
    _model = None  # Lazy-initialized model client

    @classmethod
    def _get_model(cls):
        """Lazy-initialize the Gemini model (temperature 0, as before)."""
        if cls._model is None:
            if not _get_llm_project_id():
                raise ValueError(
                    "PROJECT_ID not set. Export it or add to .env file."
                )
            cls._model = gemini.Model(_get_llm_model(), temperature=0)
            logger.info(
                f"[LLM] Initialized {_get_llm_model()} "
                f"(project={_get_llm_project_id()}, location={_get_llm_location()})"
            )
        return cls._model
```
The `except ImportError` arm goes with the lazy imports it guarded. Token reads at ~1063 and ~1187 stay (`usage.prompt_token_count` may be `None`; they are only logged and stored, so leave them).

- [ ] **Step 3: `investigate_agent_reconst.py`**

Third-party try block: remove `from google.cloud import aiplatform` and the `vertexai.preview.generative_models` import; hint becomes `pip install google-genai pydantic python-dotenv`. In the re-export try/except (~374-393) add `from ... import gemini` / `from invoice_processing import gemini` alongside the `llm_guard` imports. `_ensure_gcp_initialized`: delete the `aiplatform.init(...)` call; keep everything else including the `RuntimeError`. `_build_model`:

```python
def _build_model(model_name: str, **generation_kwargs) -> gemini.Model:
    """Construct a model, resolving the project first.
    ...(keep the docstring's history)...
    """
    _ensure_gcp_initialized()
    return gemini.Model(model_name, **generation_kwargs)
```
Callers pass `temperature=0, response_mime_type="application/json"`, which are `Model` fields.

- [ ] **Step 4: `core/rule_discoverer.py`**

```python
class _LLMClient:
    """Lazy-initialized Gemini Pro client."""

    _model = None

    @classmethod
    def get_model(cls):
        if cls._model is None:
            from .. import gemini  # noqa: PLC0415

            project_id = get_llm_project_id()
            if not project_id:
                raise ValueError(
                    "PROJECT_ID not set. Export it or add to .env file."
                )
            cls._model = gemini.Model(get_llm_model())
            logger.info(f"Initialized {get_llm_model()} (project={project_id})")
        return cls._model
```
`generate()` is unchanged.

- [ ] **Step 5: `eval/eval.py`**

Move the optional import below the `sys.path.insert` lines (AGENT_DIR is defined at ~57; the SDK import block at 44-53 runs before it) and make it:

```python
sys.path.insert(0, str(AGENT_DIR))
# Optional LLM import -- only needed when --skip-llm is NOT set
_LLM_AVAILABLE = False
try:
    from dotenv import load_dotenv

    from invoice_processing import gemini

    _LLM_AVAILABLE = True
except ImportError:
    pass
```
`_init_llm` returns `gemini.Model(model_name)` after the env/project resolution (drop `aiplatform.init`); its return annotation becomes `Optional["gemini.Model"]`. The install hint at ~1609: `pip install google-genai python-dotenv`.

- [ ] **Step 6: Run the tests and the grep**

```bash
./.venv/bin/python -m pytest tests/test_gemini.py tests/test_critic_call_guard.py tests/test_rule_drafting.py tests/test_alf_in_the_worker.py tests/test_dual_import_modes.py tests/test_critic_deterministic_checks.py tests/test_confidence.py -q -p no:cacheprovider 2>&1 | tail -3
./.venv/bin/python scripts/verify_phase0.py 2>&1 | tail -5
./.venv/bin/python eval/eval.py --ground-truth invoice_processing/exemplary_data --agent-output tests/fixtures/eval/agent_output --skip-llm --require-baseline 2>&1 | tail -2
grep -rn 'aiplatform\|vertexai' invoice_processing eval benchmark/harness scripts --include='*.py' | grep -v '^invoice_processing/gemini.py'
```
Expected: all passed (including `test_only_this_module_imports_the_sdk`); verify_phase0 all gates pass; eval gate passes; grep prints nothing.

- [ ] **Step 7: Lint and commit**

```bash
./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format --check .
git add invoice_processing/shared_libraries/alf_engine.py invoice_processing/shared_libraries/investigation/investigate_agent_reconst.py invoice_processing/core/rule_discoverer.py eval/eval.py
git commit -m "Move ALF, the critic, rule drafting and eval onto the genai client

Each keeps the settings it had: ALF temperature 0, the critic temperature 0
with JSON output, rule drafting and eval the defaults. aiplatform.init goes
with the SDK that needed it; the project checks stay.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Dependencies, policy test, documents

**Files:**
- Modify: `pyproject.toml:16-22`
- Modify: `uv.lock` (regenerated)
- Modify: `invoice_processing/requirements.txt`
- Modify: `tests/test_repo_policy.py` (the second-major test)
- Modify: `deployment/README.md:53-58`, `.env.example:1-5`, `tests/conftest.py:40-54` docstring

- [ ] **Step 1: Write the failing test**

In `tests/test_repo_policy.py::test_the_agent_stack_is_on_its_second_major`, replace the aiplatform line with:

```python
    assert _floor(by_name["google-genai"]).split(".")[0] == "2"
    # The deprecated vertexai.generative_models shim is gone; the client
    # module is the only SDK user, and it uses google-genai.
    assert "vertexai" not in by_name and "google-cloud-aiplatform" not in by_name
```

Run: `./.venv/bin/python -m pytest tests/test_repo_policy.py -q -p no:cacheprovider 2>&1 | tail -1`
Expected: 1 failed (`KeyError: 'google-genai'`).

- [ ] **Step 2: Change the dependencies**

In `pyproject.toml` `dependencies`: after `"google-adk>=2.0,<3",` add `"google-genai>=2.0,<3",`; delete the `"google-cloud-aiplatform>=2.0,<3",` and `"vertexai>=1.38.0",` lines. `invoice_processing/requirements.txt` becomes:

```
google-adk>=2.0,<3
google-genai>=2.0,<3
pdfplumber>=0.10.0
pydantic>=2.8.0
python-dotenv>=1.0.0
pyyaml>=6.0
```

```bash
uv lock 2>&1 | tail -3
uv sync --frozen 2>&1 | tail -1
./.venv/bin/python -c "import vertexai" ; ./.venv/bin/python -c "import google.genai; print(google.genai.__version__)"
```
Expected: lock prints `Remove vertexai v1.43.0` and nothing else changes; `import vertexai` fails with `ModuleNotFoundError`; genai prints `2.29.0`. (google-cloud-aiplatform stays installed via the dev group's `agent-starter-pack`; that is expected.)

- [ ] **Step 3: Documents**

`deployment/README.md` prerequisites and install line:
```
- Python packages: `google-adk` 2.x, `google-genai` 2.x, `python-dotenv`
```
```bash
pip install "google-adk>=2,<3" "google-genai>=2,<3" python-dotenv
```
`.env.example` top block: after `LOCATION=us-central1` add
```
# The app's own Gemini client (invoice_processing/gemini.py) reads
# PROJECT_ID/GOOGLE_CLOUD_PROJECT and LOCATION above; GOOGLE_GENAI_USE_VERTEXAI
# is for the ADK dev UI.
```
`tests/conftest.py` fixture docstring: replace the two sentences naming `google.cloud.aiplatform` with "`invoice_processing.gemini.client()` builds a Vertex client from `GOOGLE_CLOUD_PROJECT`; with none set, the SDK falls back to `google.auth.default()`, which fails on a machine without gcloud."

- [ ] **Step 4: Run, lint, commit**

```bash
./.venv/bin/python -m pytest tests/test_repo_policy.py tests/test_gemini.py tests/test_audit_gate.py -q -p no:cacheprovider 2>&1 | tail -1
./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format --check .
python3 scripts/audit_gate.py 2>&1 | tail -2
git add pyproject.toml uv.lock invoice_processing/requirements.txt tests/test_repo_policy.py deployment/README.md .env.example tests/conftest.py
git commit -m "Depend on google-genai and drop aiplatform and the vertexai shim

Nothing imports them now. The policy test pins the genai floor and that
neither old package returns.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```
Expected: all passed; `Dependency audit: passed`.

---

### Task 5: Offline gates

**Files:** none modified unless something fails.

- [ ] **Step 1: Full suite**

Run: `./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -3`
Expected: `2685 + new tests passed, 4 skipped` (2685 on main; this branch adds 9 in test_gemini, 2 in test_llm_unavailable). No `UserWarning: This feature is deprecated` line anymore. Any failure: root cause, failing test, fix, ledger.

- [ ] **Step 2: Nine gates**

Run: `bash benchmark/harness/gates.sh benchmark/runs/gates-genai && cat benchmark/runs/gates-genai/summary.txt`
Expected: 9 PASS.

- [ ] **Step 3: Postgres parity (never touch `bsi-platform-db-1`)**

```zsh
docker run -d --rm --name pg-x -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=t -p 55432:5432 postgres:16 && sleep 5
files=(${(f)"$(awk '/DB-backed tests against Postgres/{f=1} f&&/tests\//{for(i=1;i<=NF;i++) if($i ~ /tests\//){gsub(/[\\"]/,"",$i); print $i}} f&&/-q$/{exit}' .github/workflows/ci.yml)"})
TEST_DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/t ./.venv/bin/python -m pytest "${files[@]}" -q -p no:cacheprovider 2>&1 | tail -1
docker exec pg-x psql -q -U postgres -c 'CREATE DATABASE fresh;'
U=postgresql+pg8000://postgres:postgres@localhost:55432/fresh; DATABASE_URL=$U CLOUD_SQL_INSTANCE="" ./.venv/bin/python -m alembic upgrade head 2>&1 | tail -1; DATABASE_URL=$U CLOUD_SQL_INSTANCE="" ./.venv/bin/python -m alembic downgrade base 2>&1 | tail -1
docker stop pg-x
```
Expected: ~825 passed; head `c9d5e2f3a4b6`; downgrade exit 0.

---

### Task 6: Same-day A/B on the 14 smoke invoices

**Files:** none modified; runs in `benchmark/runs/smoke-main-vertexai/` (inside the scratch worktree) and `benchmark/runs/smoke-genai/`.

- [ ] **Step 1: The branch, new client**

```bash
cd /Users/vineet/Downloads/ADK/invoice-processing
CLOUD_SQL_INSTANCE="" GCS_BUCKET="" PUBSUB_TOPIC="" PROJECT_ID=rasayan GOOGLE_CLOUD_PROJECT=rasayan \
  ./.venv/bin/python -m benchmark.harness.run --run smoke-genai --profile benchmark/harness/profiles/after.json --subset-file benchmark/harness/smoke.txt --fresh 2>&1 | tail -6
```
Expected: no `PREFLIGHT FAILED`; `A/B/C exited 0`; `scored 14 documents`; exit 0 (PR #34 fixed the clean-run exit). If preflight fails, the last stderr line says why (ADC, billing, API); fix the environment, not the code.

- [ ] **Step 2: `main`, old client, same hour (disk is ~4 GB free: build, run, remove)**

```bash
WT=/private/tmp/claude-501/genai-main
git worktree add -q $WT main
cp .env $WT/.env
ln -s /Users/vineet/Downloads/ADK/invoice-processing/benchmark/dataset/documents $WT/benchmark/dataset/documents
(cd $WT && UV_PROJECT_ENVIRONMENT=$WT/.venv uv sync --frozen --python 3.12.12 2>&1 | tail -1 && .venv/bin/python -c "import vertexai.generative_models, google.adk; print(google.adk.__version__)")
(cd $WT && CLOUD_SQL_INSTANCE="" GCS_BUCKET="" PUBSUB_TOPIC="" PROJECT_ID=rasayan GOOGLE_CLOUD_PROJECT=rasayan \
  .venv/bin/python -m benchmark.harness.run --run smoke-main-vertexai --profile benchmark/harness/profiles/after.json --subset-file benchmark/harness/smoke.txt --fresh 2>&1 | tail -4)
```
Expected: `2.11.0`; 14 scored; exit 0.

- [ ] **Step 3: Compare**

```bash
./.venv/bin/python - <<'EOF'
import json, glob, statistics as st
def load(root, run):
    out = {}
    for f in glob.glob(f"{root}/benchmark/runs/{run}/raw/*.jsonl"):
        for line in open(f):
            r = json.loads(line); out[r["invoice_id"]] = r
    return out
old = load("/private/tmp/claude-501/genai-main", "smoke-main-vertexai")
new = load(".", "smoke-genai")
ids = [l.strip() for l in open("benchmark/harness/smoke.txt") if l.strip()]
d = lambda r: (r["final_decision"], r.get("first_failing_step"))
diff = [(i, d(old[i]), d(new[i])) for i in ids if d(old[i]) != d(new[i])]
print(f"{len(ids)} compared; decisions/steps differ: {len(diff)}"); print(*diff, sep="\n")
ext = [i for i in ids if old[i]["extracted"] != new[i]["extracted"]]
print("extractions differ:", len(ext))
for i in ext:
    o, n = old[i]["extracted"] or {}, new[i]["extracted"] or {}
    print(" ", i, sorted(k for k in set(o) | set(n) if o.get(k) != n.get(k))[:8])
for k in ("llm_calls", "cost_usd", "processing_time"):
    print(f"{k:16} old mean {st.mean(old[i].get(k) or 0 for i in ids):.4f}  new mean {st.mean(new[i].get(k) or 0 for i in ids):.4f}")
print("outcomes:", {old[i]["outcome"] for i in ids}, {new[i]["outcome"] for i in ids})
EOF
git worktree remove --force /private/tmp/claude-501/genai-main && git worktree prune && git worktree list
```
Expected: `decisions/steps differ: 0`; outcomes `{'done'}` both. Extraction differences are listed field by field and go into the changelog with an explanation (model non-determinism on seedless modules, or a field the new endpoint reads differently). Cost and processing-time means are recorded. If any decision differs, stop: read both records' findings, decide whether it is the client or the model, ledger it, and bring it to the user before Task 7 if it is the client.

---

### Task 7: The full 300-invoice live run

**Files:**
- Modify: `benchmark/CHANGELOG.md` (append a dated entry)

- [ ] **Step 1: Run it (~87 minutes; run in the background and poll the log)**

```bash
CLOUD_SQL_INSTANCE="" GCS_BUCKET="" PUBSUB_TOPIC="" PROJECT_ID=rasayan GOOGLE_CLOUD_PROJECT=rasayan \
  ./.venv/bin/python -m benchmark.harness.run --run after-genai --profile benchmark/harness/profiles/after.json --fresh > /private/tmp/claude-501/after-genai.txt 2>&1; echo "exit $?"
```
Expected: exit 0; `benchmark/runs/after-genai/report.md` says `300 of 300 submissions recorded`, `errors_or_no_decision | 0`.

- [ ] **Step 2: Score against the gate and the previous live run**

```bash
grep -E 'decision accuracy|touchless|reason_(strict|family|wrong)|incorrectly|posted untouched' benchmark/runs/after-genai/report.md
grep -E 'decision accuracy|touchless|reason_(strict|family|wrong)|incorrectly|posted untouched' benchmark/runs/after-9f8c923/report.md
grep -A3 'cost USD\|processing s' benchmark/runs/after-genai/report.md | head -12
cat benchmark/runs/after-genai/wall_seconds.txt benchmark/runs/after-9f8c923/wall_seconds.txt
./.venv/bin/python - <<'EOF'
import json, glob
gt = {r["invoice_id"]: r["expected"] for r in json.load(open("benchmark/dataset/ground_truth.json"))["records"]}
def load(run):
    out = {}
    for f in glob.glob(f"benchmark/runs/{run}/raw/*.jsonl"):
        for line in open(f):
            r = json.loads(line); out[(r["invoice_id"], r["submission_seq"])] = (r["final_decision"], r.get("first_failing_step"))
    return out
a, b = load("after-9f8c923"), load("after-genai")
moved = [k for k in a if a[k] != b.get(k)]
print(len(moved), "decisions/steps differ from after-9f8c923")
for k in sorted(moved):
    e = gt[k[0]]; exp = (e["final_decision"], e["first_failing_check"])
    print(f"{k[0]:13} exp {str(exp):18} old {str(a[k]):18} new {str(b.get(k)):18} {'ok' if b.get(k, ('',))[0]==exp[0] else 'WRONG'}")
EOF
```
Gate: `decision accuracy` ≥ **98.0%**. Also compare per-check pass rates (the `## Per check` table) and `extraction.csv` field accuracy between the two runs; list every invoice whose decision differs from `after-9f8c923` with its first failing step and whether ground truth agrees. Record total cost (USD) and median processing seconds for both runs.

If the gate fails: do not tune. Find the first differing invoice, read both records' `extracted` and `findings`, and decide whether the client changed what the model returned (endpoint/thinking defaults) or the model drifted. Bring the evidence to the user; the fix may be a `thinking_config` or an endpoint `api_version` in `gemini.Model`, which would be a design change.

- [ ] **Step 3: Write the changelog entry and commit**

Append to `benchmark/CHANGELOG.md` a `## 2026-10-13 — google-genai client: <result in five words>` entry in the file's voice with: what changed (SDK, one module, settings preserved, per-request timeout), the A/B table (14 invoices: decisions/steps identical or not, extraction diffs, cost and time means), the full-run table against `after-9f8c923` (decision accuracy, wrong accepts/rejects, reason exact/family/wrong, touchless, cost USD, median s, wall minutes), and the sentence that replaces the 2026-10-13 adk entry's "every Gemini call still goes through `vertexai.generative_models`".

```bash
git add benchmark/CHANGELOG.md
git commit -m "Record the google-genai client's A/B smoke and full live run

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Browser check

**Files:**
- Modify (temporarily, restored): `.claude/launch.json`

- [ ] **Step 1:** `cp .claude/launch.json /private/tmp/claude-501/launch.json.bak`; add `check-api-8200` (the `manual-a-api-worker` command without the worker, `--port 8200`) and `check-frontend-3200` (`cd frontend && API_ORIGIN=http://127.0.0.1:8200 npx next dev -p 3200`).
- [ ] **Step 2:** `preview_start {name:"check-api-8200"}`, then `check-frontend-3200`. Open `/invoices?case=<an id from GET /api/invoices with X-Dev-Principal: clerk@bench.test:clerk>`; the detail renders; `read_console_messages` shows no API 500s after startup; `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8200/api/system/status` → 200. Optionally upload one PDF through `/` to watch the worker-less API accept it (the model call happens in the worker, which is not running here).
- [ ] **Step 3:** `preview_stop` both; `cp /private/tmp/claude-501/launch.json.bak .claude/launch.json`.

---

### Task 9: Review, PR

- [ ] **Step 1:** Whole-branch review on the `fable` model (superpowers:requesting-code-review) with the review package, spec, plan, this Review Focus, and the ledger's rulings. One fix pass; each fix tested first.
- [ ] **Step 2:** Re-run `pytest tests/ -q -p no:cacheprovider`, ruff, `python3 scripts/audit_gate.py` after any fix.
- [ ] **Step 3:** On the user's choice: `git push -u origin phase3/genai-client`; `gh pr create --title "Move the Gemini calls to the google-genai client" --body-file …` (body: why, what, A/B and full-run tables, rulings; ends with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`); `mcp__ccd_pr__get_status`. Merge only on an explicit "merge PR N".
- [ ] **Step 4:** `rm -rf .superpowers/sdd/2026-10-13-genai-client`; update memory `latest-handoff.md`.
