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
    """(prompt, output) token counts; the SDK leaves any as None. Output
    includes thinking, which Vertex bills at the output rate."""
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return 0, 0
    output = (usage.candidates_token_count or 0) + (
        usage.thoughts_token_count or 0
    )
    return usage.prompt_token_count or 0, output


# Measured on the 14-invoice smoke subset (2026-11-06): low cut cost 64% with
# the same decisions, but on Pro it misread two printed ex-tax totals that
# the default read correctly. Flash only classifies and codes lines.
DEFAULT_THINKING = {"flash": "low", "pro": "medium"}


def thinking_config(model_name: str) -> types.ThinkingConfig | None:
    """How hard a Gemini 3 model thinks before answering.

    Thinking is billed as output, and at the model's default it was most of
    the bill (Nov 2026: ~92% of spend was output for calls that return a
    small JSON). GEMINI_FLASH_THINKING_LEVEL / GEMINI_PRO_THINKING_LEVEL
    override the defaults (minimal/low/medium/high; empty leaves it to the
    model). 2.5 models take a budget, not a level, and are left alone.
    """
    name = model_name.rsplit("/", maxsplit=1)[-1]
    if not name.startswith("gemini-3"):
        return None
    family = "pro" if "-pro" in name else "flash"
    level = os.getenv(
        f"GEMINI_{family.upper()}_THINKING_LEVEL", DEFAULT_THINKING[family]
    ).strip()
    if not level:
        return None
    return types.ThinkingConfig(
        thinking_level=types.ThinkingLevel(level.upper())
    )


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
            thinking_config=thinking_config(self.name),
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
