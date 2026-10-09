"""Ports: the seams between the invoice engines and whatever stores their data.

Today every engine reaches for the local filesystem directly, via package-
relative constants. That is why the system is single-tenant, why outputs land
inside the deployed artifact, and why moving the agent broke `.env` resolution
in six places at once.

These protocols name the four things the engines actually need -- blob storage,
case artifacts, the rule base, and an audit trail -- plus a client for model
calls. Phase 1 implements them against GCS, Cloud SQL and Vertex; the local
implementations in `adapters.py` keep tests and development on the filesystem
with no behaviour change.

Two design points worth stating explicitly:

* `RunContext` is what removes the module-level singletons. Every engine call
  takes one, so tenant, case, actor and settings travel with the work rather
  than living in process globals that concurrent requests would share.
* `RuleRepository.save` takes `expected_version`. The rule base is currently a
  whole-file rewrite with no locking, and rule IDs are allocated by scanning for
  the maximum and adding one -- so two SMEs editing at once silently lose a
  rule. Optimistic concurrency makes that a visible conflict instead.

Nothing imports this module yet at runtime; it defines the target shape so the
engines can be moved onto it incrementally without a big-bang migration.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunContext:
    """Identity and configuration for one unit of work.

    Threaded through every engine call. Replaces `_case_loader`,
    `_session_logger`, `_orchestrator` (tools.py), `_investigation_state`
    (agent.py) and the mutable `_config_store` global in the acting pipeline.
    """

    tenant_id: str = "default"
    case_id: str = ""
    run_id: str = ""
    actor: str | None = None  # user email, or "system"
    settings: dict[str, Any] = field(default_factory=dict)

    def child(self, **overrides: Any) -> RunContext:
        """Derive a context for a sub-step, e.g. a different case."""
        return RunContext(
            tenant_id=overrides.get("tenant_id", self.tenant_id),
            case_id=overrides.get("case_id", self.case_id),
            run_id=overrides.get("run_id", self.run_id),
            actor=overrides.get("actor", self.actor),
            settings=overrides.get("settings", self.settings),
        )


class RuleBaseConflict(RuntimeError):
    """The rule base changed since it was read. Re-read and retry."""


class StatusChanged(RuntimeError):
    """A scanned document's review was decided by someone else meanwhile."""

    def __init__(self, status: str):
        super().__init__(f"already {status}")
        self.status = status


class UploadOwned(ValueError):
    """The ERP upload owns this PO; a scan may not change it."""

    def __init__(self, po_number: str):
        super().__init__(
            f"PO {po_number} came from your ERP upload; a scan cannot change it."
        )
        self.po_number = po_number


class ConcurrentWrite(ValueError):
    """Another writer inserted the same PO or receipt at the same moment."""

    def __init__(self, what: str):
        super().__init__(
            f"Another import stored this {what} at the same moment; "
            "reload and try again."
        )


class LockHeld(RuntimeError):
    """Someone else is editing this record. Carries the lock, so the refusal
    can say who and until when -- "locked" without a name is unactionable."""

    def __init__(self, lock: dict):
        super().__init__(
            f"Locked by {lock.get('holder')} until {lock.get('expires_at')}"
        )
        self.lock = lock


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


@runtime_checkable
class BlobStore(Protocol):
    """Opaque bytes. Documents in, rendered pages and exports out.

    Deliberately free of path semantics so the core never learns whether it is
    talking to a directory or a bucket.
    """

    def get(self, key: str) -> bytes: ...

    def put(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str: ...

    def exists(self, key: str) -> bool: ...

    def signed_url(self, key: str, ttl_seconds: int = 3600) -> str: ...


@runtime_checkable
class CaseRepository(Protocol):
    """Reads and writes the per-case stage artifacts.

    The only way the engines see case data. `iter_case_ids` must return a
    deterministic order: impact assessment walks it, and a report that changes
    between runs is not a safety guarantee.
    """

    def get_artifacts(self, ctx: RunContext) -> dict[str, dict]:
        """Return {'extraction': {...}, 'phase1': {...}, ...} for ctx.case_id."""

    def put_artifact(
        self, ctx: RunContext, key: str, payload: dict
    ) -> None: ...

    def update_artifact(
        self, ctx: RunContext, key: str, mutate: Callable[[dict | None], dict]
    ) -> dict:
        """Store `mutate(current value)` under one lock and return it.

        For any read-modify-write: two writers that each read, change, and
        put the whole artifact lose whichever wrote first.
        """

    def iter_case_ids(self, tenant_id: str) -> Iterator[str]:
        """Deterministic, stable ordering."""

    def get_many_artifacts(
        self, tenant_id: str, case_ids: list[str]
    ) -> dict[str, dict[str, dict]]:
        """Batch read for impact assessment -- one query, not N."""

    def project_case(self, ctx: RunContext, artifacts: dict) -> None:
        """Mirror a finished case into whatever queryable form the store has.

        The artifacts are the record; this is the view of them that answers
        questions like "which invoices await review" without loading every
        case. A store whose artifacts are already queryable implements it as
        a no-op.
        """


@runtime_checkable
class RuleRepository(Protocol):
    """Stores the ALF rule base.

    **Status: aspirational. No live consumer.** Both implementations exist and
    are tested (`JsonRuleRepository`, `PgRuleRepository`), but nothing routes
    through them: `core/rule_writer.py` and `alf_engine.load_rule_base` each do
    their own file I/O against a path. So the optimistic-concurrency and
    id-allocation guarantees described below are available and unused -- two
    concurrent SME sessions can still lose a rule.

    Stated here rather than left to be discovered, because a port that looks
    wired and is not is worse than an absent one. Closing it means giving
    `RuleWriter` and `ALFEngine` an injected repository instead of a path, which
    is a refactor of the learning path, not a configuration change.
    """

    def load(self, tenant_id: str) -> tuple[dict, int]:
        """Return (document, version). The document includes disabled rules and
        any keys this version does not understand -- dropping them on save is
        silent data loss."""

    def save(self, tenant_id: str, doc: dict, *, expected_version: int) -> int:
        """Write if the stored version still matches. Returns the new version.

        Raises RuleBaseConflict when someone else wrote first.
        """

    def next_rule_id(self, tenant_id: str) -> str:
        """Allocate an id from the store, never by scanning for max + 1."""


@runtime_checkable
class AuditSink(Protocol):
    """Append-only trail of who did what.

    Durable on call: the current session logger buffers in memory until an
    explicit save(), so a crash loses the whole SME session.
    """

    def emit(
        self,
        ctx: RunContext,
        event_type: str,
        payload: dict,
        before: dict | None = None,
    ) -> None:
        """`before` is the state the action replaced.

        Without it the trail records an after-state with nothing to compare it
        to, and "what changed" -- the question an auditor actually asks -- can
        only be answered by reading the values out of some other store.
        """


@runtime_checkable
class CorrectionStore(Protocol):
    """Reviewer edits: the accuracy record, and the source of future rules.

    Separate from the audit sink because the two answer different questions.
    The audit trail says an edit happened and who made it; a correction is the
    old value, the new value and the field -- the training signal. Recording
    only the first leaves nothing to learn from.
    """

    def record(self, ctx: RunContext, correction: dict) -> None: ...

    def list(
        self, tenant_id: str, case_id: str | None = None
    ) -> list[dict]: ...


@runtime_checkable
class LlmClient(Protocol):
    """Model calls, with the cost and latency controls in one place."""

    def generate(
        self,
        prompt: Any,
        *,
        model: str,
        schema: type | None = None,
        temperature: float = 0.0,
        timeout_seconds: float | None = None,
    ) -> Any: ...
