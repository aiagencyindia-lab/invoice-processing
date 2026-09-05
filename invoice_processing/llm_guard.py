"""A model call that cannot hang, and cannot retry forever.

The Vertex SDK's `generate_content` takes no timeout and the RPC beneath it has
none either, so a stalled request blocks its caller indefinitely. That was
observed twice at 15-25 minutes in the critic, and again in the acting pipeline
at 294 seconds on a single scanned invoice. In the worker the consequence is
worse than slow: the queue lease expires, the document is redelivered, stalls
again, and after three attempts an invoice nobody rejected is dead-lettered.

This module was the critic's fix, written in `investigate_agent_reconst.py` and
never shared. It lives here now because the acting pipeline needs exactly the
same thing, and a second implementation would drift from the first the way the
two ABN checksums did.

Three guarantees:

* **A hard wall-clock timeout per call.** The worker thread exists only so the
  caller can stop waiting; the abandoned request keeps running until the SDK
  gives up, because it cannot be killed. The pool is bounded so leaked calls
  cannot accumulate.
* **Retry with backoff, for infrastructure errors only.** A malformed request
  is not a flaky network: retrying it three times spends three times as much to
  fail the same way, so anything unrecognised is raised immediately.
* **A ceiling on calls and elapsed time per case**, for callers that make many.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field


class LlmBudgetExceeded(RuntimeError):
    """The per-case LLM call budget or deadline was exhausted."""


class LlmCallTimeout(RuntimeError):
    """A single LLM call exceeded its wall-clock timeout."""


DEFAULT_LLM_TIMEOUT_SECONDS = float(
    os.getenv("CRITIC_LLM_TIMEOUT_SECONDS", "120")
)
DEFAULT_MAX_LLM_CALLS = int(os.getenv("CRITIC_MAX_LLM_CALLS", "30"))
DEFAULT_CASE_DEADLINE_SECONDS = float(
    os.getenv("CRITIC_CASE_DEADLINE_SECONDS", "600")
)

_LLM_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="llm")

_GUARD_MAX_ATTEMPTS = 3
_GUARD_BASE_DELAY = 1.0
_INFRASTRUCTURE_ERROR_PATTERNS = (
    "503",
    "UNAVAILABLE",
    "timeout",
    "connection",
    "failed to connect",
    "DeadlineExceeded",
    "ServiceUnavailable",
    "DEADLINE_EXCEEDED",
    "429",
    "RESOURCE_EXHAUSTED",
)


@dataclass
class CallBudget:
    """Per-case ceiling on LLM calls and wall-clock time."""

    max_calls: int = DEFAULT_MAX_LLM_CALLS
    deadline_seconds: float = DEFAULT_CASE_DEADLINE_SECONDS
    timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS
    calls_made: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def remaining_seconds(self) -> float:
        return self.deadline_seconds - (time.monotonic() - self.started_at)

    def check(self) -> None:
        """Raise if this case has already used its allowance."""
        if self.calls_made >= self.max_calls:
            raise LlmBudgetExceeded(
                f"LLM call budget exhausted ({self.calls_made}/{self.max_calls} "
                f"calls). Stopped rather than continuing to spend."
            )
        if self.remaining_seconds() <= 0:
            raise LlmBudgetExceeded(
                f"Case deadline of {self.deadline_seconds:.0f}s exceeded after "
                f"{self.calls_made} calls. Stopped."
            )


def guarded_generate(model, prompt, budget: CallBudget | None = None):
    """Call generate_content with a hard timeout, retry and budget.

    Returns the model response. Raises LlmCallTimeout or LlmBudgetExceeded
    rather than blocking indefinitely.
    """
    budget = budget or CallBudget()
    budget.check()

    timeout = min(budget.timeout_seconds, max(budget.remaining_seconds(), 1.0))
    last_error: Exception | None = None

    for attempt in range(_GUARD_MAX_ATTEMPTS):
        budget.calls_made += 1
        future = _LLM_EXECUTOR.submit(model.generate_content, prompt)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout:
            future.cancel()
            last_error = LlmCallTimeout(
                f"LLM call exceeded {timeout:.0f}s and was abandoned"
            )
            print(f"      LLM call timed out after {timeout:.0f}s")
        except Exception as exc:  # classified below, not swallowed
            last_error = exc
            message = str(exc)
            if not any(
                pattern.lower() in message.lower()
                for pattern in _INFRASTRUCTURE_ERROR_PATTERNS
            ):
                raise  # A real error: surface it instead of retrying blindly.
            print(f"      LLM infrastructure error: {message[:80]}")

        if attempt < _GUARD_MAX_ATTEMPTS - 1:
            delay = _GUARD_BASE_DELAY * (2**attempt)
            if budget.remaining_seconds() <= delay:
                break
            print(f"      Retrying in {delay:.1f}s...")
            time.sleep(delay)

    raise last_error if last_error else LlmCallTimeout("LLM call failed")
