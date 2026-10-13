"""The one module that talks to the Gemini SDK.

Everything else sees `Model.generate_content(contents)`, the same surface the
old vertexai object had, so the guard, the cache and the fakes do not change.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import types

from invoice_processing import gemini

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def fresh_client():
    real = gemini.client  # tests may swap gemini.client for a stand-in
    real.cache_clear()
    yield
    real.cache_clear()


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


def _use(monkeypatch, recorder):
    """Client.models is a read-only property; stand in for the client."""
    monkeypatch.setattr(
        gemini, "client", lambda: SimpleNamespace(models=recorder)
    )


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
    _use(monkeypatch, rec)
    monkeypatch.setenv("VERTEX_TIMEOUT_SECONDS", "42")
    model = gemini.Model(
        "gemini-2.5-flash",
        temperature=0,
        seed=42,
        response_mime_type="application/json",
    )
    response = model.generate_content("hello")
    assert response.text == "ok"
    name, contents, config = rec.calls[0]
    assert name == "gemini-2.5-flash"
    assert contents == "hello"
    assert (config.temperature, config.seed) == (0, 42)
    assert config.response_mime_type == "application/json"
    # milliseconds, and later than the guard's own 42 s: see the next test
    assert (
        config.http_options.timeout
        == (42 + gemini.SDK_TIMEOUT_MARGIN_SECONDS) * 1000
    )


def test_the_guard_gives_up_before_vertex_does(monkeypatch):
    """The SDK also sends its timeout to Vertex, which then aborts the call
    itself with 504 DEADLINE_EXCEEDED. If that won the race, a slow document
    would read as an outage (retried, then ERROR) instead of the guard's
    timeout (which keeps the document's fallback). The SDK's deadline is the
    backstop for an abandoned call, so it must come after the guard's."""
    monkeypatch.setenv("VERTEX_TIMEOUT_SECONDS", "180")
    assert gemini.timeout_ms() > 180 * 1000


def test_unset_settings_are_left_to_the_sdk(monkeypatch):
    rec = Recorder()
    _use(monkeypatch, rec)
    gemini.Model("gemini-2.5-pro").generate_content("p")
    config = rec.calls[0][2]
    assert config.seed is None and config.response_mime_type is None
    assert config.temperature == 0


def test_a_pdf_travels_as_inline_data(monkeypatch):
    rec = Recorder()
    _use(monkeypatch, rec)
    gemini.Model("m").generate_content(
        [gemini.pdf_part(b"%PDF-1.4"), "read it"]
    )
    contents = rec.calls[0][1]
    assert contents[0].inline_data.mime_type == "application/pdf"
    assert contents[0].inline_data.data == b"%PDF-1.4"
    assert contents[1] == "read it"


def test_an_empty_response_is_an_error(monkeypatch):
    rec = Recorder(types.GenerateContentResponse(candidates=[]))
    _use(monkeypatch, rec)
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
    old = re.compile(
        r"^\s*(from|import)\s+(vertexai|google\.cloud\.aiplatform)\b", re.M
    )
    new = re.compile(r"^\s*(from|import)\s+google\.genai\b", re.M)
    offenders, genai_users = [], []
    for folder in (
        "invoice_processing",
        "eval",
        "benchmark/harness",
        "scripts",
    ):
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
