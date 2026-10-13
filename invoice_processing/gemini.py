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
# The SDK's deadline is a backstop for a call the guard has already given up
# on, so it lands after the guard's: see timeout_ms.
SDK_TIMEOUT_MARGIN_SECONDS = 15


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
    return (
        os.getenv("LOCATION")
        or os.getenv("GOOGLE_CLOUD_REGION")
        or "us-central1"
    )


def timeout_ms() -> int:
    """The request deadline, in the milliseconds the SDK wants.

    The SDK also sends it to Vertex, which aborts the call with 504
    DEADLINE_EXCEEDED when it passes. The guard waits VERTEX_TIMEOUT_SECONDS
    and treats its own timeout as the document's problem (the fallback
    stays); a 504 would read as an outage instead. So the SDK's deadline
    comes a margin later and only ever ends a call nobody is waiting for.
    """
    seconds = float(
        os.getenv("VERTEX_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    )
    return int((seconds + SDK_TIMEOUT_MARGIN_SECONDS) * 1000)


# ponytail: one process-wide client; per-tenant credentials would key the cache.
@lru_cache(maxsize=1)
def client() -> genai.Client:
    return genai.Client(
        vertexai=True, project=project_id(), location=location()
    )


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
