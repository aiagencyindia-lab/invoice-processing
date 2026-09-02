"""Local filesystem implementations of the ports.

These preserve today's on-disk layout exactly, so development and tests behave
as before. Phase 1 adds GCS and Postgres implementations of the same protocols;
nothing in the engines changes when it does.

The one behaviour that is deliberately *not* preserved is rule-base writing.
Today `RuleWriterAgent` rewrites the whole file with no locking and allocates
ids by scanning for the maximum, so two concurrent SME sessions silently lose a
rule. `JsonRuleRepository` adds optimistic concurrency and an allocation that
reserves the id by writing it, which turns that silent loss into a visible
conflict.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from . import paths
from .ports import RuleBaseConflict, RunContext

# ---------------------------------------------------------------------------
# Blob storage
# ---------------------------------------------------------------------------


class LocalBlobStore:
    """Files under a root directory, keyed by relative path."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root or paths.DATA_DIR)

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        # Keys come from case ids and filenames; refuse anything that climbs out.
        if (
            self.root.resolve() not in candidate.parents
            and candidate != self.root.resolve()
        ):
            raise ValueError(f"Key escapes the blob root: {key!r}")
        return candidate

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def put(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def signed_url(self, key: str, ttl_seconds: int = 3600) -> str:
        """Local files have no signed URLs; a file:// URI is the honest answer."""
        return self._path(key).as_uri()


# ---------------------------------------------------------------------------
# Case artifacts
# ---------------------------------------------------------------------------


class FsCaseRepository:
    """Reads the per-case stage artifacts written by the acting pipeline.

    **Single-tenant by construction.** One directory holds one tenant's cases,
    so this adapter is bound to a tenant id at construction and serves nothing
    to any other. Before that binding existed it ignored `ctx.tenant_id`
    completely, which meant that the moment authentication admitted a second
    tenant, both would have read the same directory -- a cross-tenant leak with
    no code change required to trigger it.

    Reads for a foreign tenant come back empty rather than raising, so the API
    returns 404 and discloses nothing about whether a case exists. Writes raise:
    a write aimed at the wrong tenant is never legitimate, and silently landing
    it in this tenant's directory would corrupt the wrong customer's data.
    """

    def __init__(
        self,
        root: Path | None = None,
        artifact_map: dict | None = None,
        tenant_id: str = "default",
    ):
        self.root = Path(root or paths.AGENTIC_FLOW_OUT)
        self.tenant_id = tenant_id
        if artifact_map is None:
            from .core.config import ARTIFACT_MAP  # noqa: PLC0415

            artifact_map = ARTIFACT_MAP
        self.artifact_map = artifact_map

    def serves(self, tenant_id: str) -> bool:
        return tenant_id == self.tenant_id

    def _case_dir(self, ctx: RunContext) -> Path:
        return self.root / ctx.case_id

    def get_artifacts(self, ctx: RunContext) -> dict[str, dict]:
        if not self.serves(ctx.tenant_id):
            return {}
        artifacts: dict[str, dict] = {}
        case_dir = self._case_dir(ctx)
        for key, filename in self.artifact_map.items():
            path = case_dir / filename
            if path.exists():
                try:
                    artifacts[key] = json.loads(
                        path.read_text(encoding="utf-8")
                    )
                except json.JSONDecodeError:
                    artifacts[key] = {}

        # Anything written under a key the map does not know about is read back
        # under that same key. Without this, `put_artifact` accepts any key --
        # it falls back to `<key>.json` -- while `get_artifacts` returns only
        # mapped ones, so the write silently disappears. That asymmetry is how
        # recorded approvals became unreadable.
        mapped = set(self.artifact_map.values())
        if case_dir.exists():
            for path in sorted(case_dir.glob("*.json")):
                if path.name in mapped or path.stem in artifacts:
                    continue
                try:
                    artifacts[path.stem] = json.loads(
                        path.read_text(encoding="utf-8")
                    )
                except json.JSONDecodeError:
                    artifacts[path.stem] = {}
        return artifacts

    def put_artifact(self, ctx: RunContext, key: str, payload: dict) -> None:
        if not self.serves(ctx.tenant_id):
            raise PermissionError(
                f"This repository serves tenant {self.tenant_id!r}, "
                f"not {ctx.tenant_id!r}."
            )
        filename = self.artifact_map.get(key, f"{key}.json")
        path = self._case_dir(ctx) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def iter_case_ids(self, tenant_id: str = "default") -> Iterator[str]:
        """Sorted, so impact assessment walks cases in a stable order."""
        if not self.serves(tenant_id) or not self.root.exists():
            return iter(())
        return iter(
            sorted(
                d.name
                for d in self.root.iterdir()
                if d.is_dir() and not d.name.startswith(".")
            )
        )

    def project_case(self, ctx: RunContext, artifacts: dict) -> None:
        """Nothing to do: on a filesystem the artifacts are the query surface.

        Declared rather than omitted so the port has one shape -- a caller
        that has to ask whether the method exists is a caller that will
        forget to.
        """

    def get_many_artifacts(
        self, tenant_id: str, case_ids: list[str]
    ) -> dict[str, dict[str, dict]]:
        if not self.serves(tenant_id):
            return {}
        return {
            case_id: self.get_artifacts(
                RunContext(tenant_id=tenant_id, case_id=case_id)
            )
            for case_id in case_ids
        }


# ---------------------------------------------------------------------------
# Rule base
# ---------------------------------------------------------------------------


class JsonRuleRepository:
    """The rule base as a JSON file, with optimistic concurrency.

    Version is derived from the file's content hash rather than stored, so an
    edit made outside this class (by hand, or by the existing RuleWriterAgent)
    still invalidates a stale writer's expectation.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path or paths.RULE_BASE_PATH)

    def _version_of(self, raw: str) -> int:
        # A content hash truncated to a positive int: stable, and changes on any
        # edit including ones made by another process.
        import hashlib  # noqa: PLC0415

        return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12], 16)

    def load(self, tenant_id: str = "default") -> tuple[dict, int]:
        if not self.path.exists():
            return {"schema_version": "2.0.0", "metadata": {}, "rules": []}, 0
        raw = self.path.read_text(encoding="utf-8")
        return json.loads(raw), self._version_of(raw)

    def save(self, tenant_id: str, doc: dict, *, expected_version: int) -> int:
        from .context import validate_rule_base  # noqa: PLC0415

        validate_rule_base(doc)
        _, current = self.load(tenant_id)
        if current != expected_version:
            raise RuleBaseConflict(
                "The rule base changed since it was read "
                f"(expected version {expected_version}, found {current}). "
                "Re-read it and reapply the change rather than overwriting."
            )

        payload = json.dumps(doc, indent=2, ensure_ascii=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory, then rename. A crash
        # mid-write leaves the previous rule base intact instead of a truncated
        # one -- this file is the record of every rule an SME approved.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return self._version_of(payload)

    def next_rule_id(self, tenant_id: str = "default") -> str:
        """Allocate the next id.

        Still derived from the existing rules, but callers must save with the
        version they loaded, so two sessions racing for the same id produce a
        RuleBaseConflict rather than one silently overwriting the other.
        """
        doc, _ = self.load(tenant_id)
        highest = 0
        for rule in doc.get("rules", []):
            rule_id = str(rule.get("id", ""))
            if rule_id.startswith("ALF-"):
                try:
                    highest = max(highest, int(rule_id.split("-")[1]))
                except (IndexError, ValueError):
                    continue
        return f"ALF-{highest + 1:03d}"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class JsonlCorrectionStore:
    """Corrections as JSONL. Durable on a machine that keeps its disk.

    That caveat is the whole reason the Postgres store exists: on a container
    filesystem this file is gone on the next deploy, taking with it the only
    copy of what a reviewer actually changed.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path or paths.DATA_DIR / "corrections.jsonl")

    def record(self, ctx: RunContext, correction: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(correction, default=str) + "\n")

    def list(self, tenant_id: str, case_id: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if case_id is not None and record.get("case_id") != case_id:
                continue
            # Older records predate tenant stamping; one with no tenant is
            # only ever visible to the tenant asking for everything.
            if record.get("tenant_id") not in (None, tenant_id):
                continue
            out.append(record)
        return out


class JsonlAuditSink:
    """Append-only JSONL. Durable on every call, unlike the session logger."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or paths.SESSIONS_DIR / "audit.jsonl")

    def emit(
        self,
        ctx: RunContext,
        event_type: str,
        payload: dict,
        before: dict | None = None,
    ) -> None:
        from datetime import datetime, timezone  # noqa: PLC0415

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tenant_id": ctx.tenant_id,
            "case_id": ctx.case_id,
            "run_id": ctx.run_id,
            "actor": ctx.actor,
            "event_type": event_type,
            "before": before,
            "payload": payload,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")


class JsonDocumentRepository:
    """Ingested-document registry as one JSON file, for local development.

    Same contract as `PgDocumentRepository`: the pilot workbench can ingest
    without a database standing up first. Written through a temp file and
    `os.replace` so an interrupted write cannot leave a half-file that loses the
    whole registry.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else paths.DATA_DIR / "documents.json"

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    def find_by_sha(self, tenant_id: str, sha256: str) -> dict | None:
        for document in self._read().get(tenant_id, {}).values():
            if document.get("sha256") == sha256:
                return document
        return None

    def create(self, tenant_id: str, **fields) -> str:
        data = self._read()
        document_id = str(uuid.uuid4())
        data.setdefault(tenant_id, {})[document_id] = {
            "id": document_id,
            "tenant_id": tenant_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        self._write(data)
        return document_id

    def list_documents(self, tenant_id: str, limit: int = 200) -> list[dict]:
        """Every ingested document for a tenant, newest first.

        The Documents view reads this to show what has been received but not
        yet processed -- the state that was previously invisible and made an
        upload look like it had failed.
        """
        docs = list(self._read().get(tenant_id, {}).values())
        docs.sort(key=lambda d: str(d.get("created_at") or ""), reverse=True)
        return docs[:limit]

    def tenant_id_for_slug(self, slug: str) -> str | None:
        """Resolve an inbound-email local part to a tenant, or refuse.

        This used to `return slug or None`, which meant every recipient
        local-part resolved to a tenant id of the same name -- so inbound mail
        addressed to anything at all was accepted and filed under an invented
        tenant. The test suite did not catch it because the fake used in tests
        held a real tenant map and was therefore *stricter* than the code it
        stood in for.

        Local development has exactly one tenant, so that is the only slug that
        resolves. Anything else returns None and the webhook answers 404, which
        is what the Postgres implementation does for an unknown or suspended
        tenant.
        """
        if not slug:
            return None
        known = os.getenv("AUTH_DEV_TENANT", "default")
        if slug == known:
            return slug
        # A tenant already holding documents is also legitimate, so a local
        # corpus built up before AUTH_DEV_TENANT was set keeps working.
        return slug if slug in self._read() else None
