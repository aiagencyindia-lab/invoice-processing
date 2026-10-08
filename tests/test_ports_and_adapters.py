"""The storage seam, and the concurrency bug it closes.

Every engine currently reaches for the local filesystem through package-relative
constants. That is why the system is single-tenant, why output lands inside the
deployed artifact, and why moving the agent broke `.env` resolution in six
places at once.

`ports.py` names what the engines actually need; `adapters.py` implements it on
the filesystem so nothing changes today. Phase 1 swaps in GCS and Postgres
behind the same protocols.

The rule repository also fixes a real data-loss path: the existing writer
rewrites the whole file with no locking and allocates ids by scanning for the
maximum, so two SMEs approving rules at the same time silently lose one.
"""

import json

import pytest

from invoice_processing.adapters import (
    FsCaseRepository,
    JsonlAuditSink,
    JsonRuleRepository,
    LocalBlobStore,
)
from invoice_processing.ports import (
    AuditSink,
    BlobStore,
    CaseRepository,
    RuleBaseConflict,
    RuleRepository,
    RunContext,
)

RULE_DOC = {
    "schema_version": "2.0.0",
    "metadata": {"name": "test"},
    "rules": [
        {
            "id": "ALF-001",
            "name": "First",
            "enabled": True,
            "conditions": [],
            "actions": [],
        }
    ],
}


class TestProtocolConformance:
    """The adapters must actually satisfy the protocols they target."""

    def test_blob_store(self, tmp_path):
        assert isinstance(LocalBlobStore(tmp_path), BlobStore)

    def test_case_repository(self, tmp_path):
        assert isinstance(FsCaseRepository(tmp_path, {}), CaseRepository)

    def test_rule_repository(self, tmp_path):
        assert isinstance(
            JsonRuleRepository(tmp_path / "r.json"), RuleRepository
        )

    def test_audit_sink(self, tmp_path):
        assert isinstance(JsonlAuditSink(tmp_path / "a.jsonl"), AuditSink)


class TestRunContext:
    def test_carries_identity_instead_of_globals(self):
        ctx = RunContext(
            tenant_id="acme", case_id="case_005", actor="sme@x.com"
        )
        assert (ctx.tenant_id, ctx.case_id, ctx.actor) == (
            "acme",
            "case_005",
            "sme@x.com",
        )

    def test_is_immutable(self):
        """Shared state that can be mutated in flight is the bug we're removing."""
        ctx = RunContext(tenant_id="acme")
        with pytest.raises(AttributeError):
            ctx.tenant_id = "other"

    def test_child_derives_without_mutating(self):
        parent = RunContext(tenant_id="acme", case_id="case_001", actor="a@b.c")
        child = parent.child(case_id="case_002")
        assert child.case_id == "case_002"
        assert child.tenant_id == "acme" and child.actor == "a@b.c"
        assert parent.case_id == "case_001"


class TestBlobStore:
    def test_round_trip(self, tmp_path):
        store = LocalBlobStore(tmp_path)
        store.put("case_005/invoice.pdf", b"%PDF-1.4")
        assert store.exists("case_005/invoice.pdf")
        assert store.get("case_005/invoice.pdf") == b"%PDF-1.4"

    def test_missing_key(self, tmp_path):
        assert LocalBlobStore(tmp_path).exists("nope") is False

    def test_key_cannot_escape_the_root(self, tmp_path):
        """A tenant-supplied key must not reach another tenant's files."""
        with pytest.raises(ValueError, match="escapes"):
            LocalBlobStore(tmp_path).get("../../etc/passwd")


class TestCaseRepository:
    def test_reads_artifacts(self, tmp_path):
        case = tmp_path / "case_001"
        case.mkdir()
        (case / "02_extraction.json").write_text(json.dumps({"waf_count": 1}))

        repo = FsCaseRepository(tmp_path, {"extraction": "02_extraction.json"})
        artifacts = repo.get_artifacts(RunContext(case_id="case_001"))
        assert artifacts["extraction"] == {"waf_count": 1}

    def test_iteration_order_is_deterministic(self, tmp_path):
        """Impact assessment walks this; an unstable order is not a guarantee."""
        for name in ("case_003", "case_001", "case_002"):
            (tmp_path / name).mkdir()
        repo = FsCaseRepository(tmp_path, {})
        assert list(repo.iter_case_ids("default")) == [
            "case_001",
            "case_002",
            "case_003",
        ]

    def test_corrupt_artifact_does_not_crash_the_read(self, tmp_path):
        case = tmp_path / "case_001"
        case.mkdir()
        (case / "02_extraction.json").write_text("{ not json")
        repo = FsCaseRepository(tmp_path, {"extraction": "02_extraction.json"})
        assert (
            repo.get_artifacts(RunContext(case_id="case_001"))["extraction"]
            == {}
        )

    def test_a_case_id_cannot_escape_the_root(self, tmp_path):
        root = tmp_path / "cases"
        root.mkdir()
        outside = tmp_path / "x"
        outside.mkdir()
        (outside / "02_extraction.json").write_text('{"secret": 1}')
        repo = FsCaseRepository(root, {"extraction": "02_extraction.json"})
        assert repo.get_artifacts(RunContext(case_id="../x")) == {}
        with pytest.raises(ValueError):
            repo.put_artifact(RunContext(case_id="../x"), "extraction", {})
        assert json.loads((outside / "02_extraction.json").read_text()) == {
            "secret": 1
        }

    def test_batch_read(self, tmp_path):
        for name in ("case_001", "case_002"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "02_extraction.json").write_text(
                '{"waf_count": 0}'
            )
        repo = FsCaseRepository(tmp_path, {"extraction": "02_extraction.json"})
        batch = repo.get_many_artifacts("default", ["case_001", "case_002"])
        assert set(batch) == {"case_001", "case_002"}


class TestRuleRepositoryConcurrency:
    """The data-loss path: two SMEs approving rules at the same time."""

    @pytest.fixture
    def repo(self, tmp_path):
        path = tmp_path / "rule_base.json"
        path.write_text(json.dumps(RULE_DOC, indent=2))
        return JsonRuleRepository(path)

    def test_load_returns_a_version(self, repo):
        doc, version = repo.load()
        assert doc["rules"][0]["id"] == "ALF-001"
        assert version != 0

    def test_save_with_current_version_succeeds(self, repo):
        doc, version = repo.load()
        doc["rules"].append({"id": "ALF-002", "name": "Second"})
        new_version = repo.save("default", doc, expected_version=version)
        assert new_version != version
        assert len(repo.load()[0]["rules"]) == 2

    def test_concurrent_write_is_rejected(self, repo):
        """The regression: the second writer used to silently win."""
        doc_a, version_a = repo.load()
        doc_b, version_b = repo.load()

        doc_a["rules"].append({"id": "ALF-002", "name": "From SME A"})
        repo.save("default", doc_a, expected_version=version_a)

        doc_b["rules"].append({"id": "ALF-002", "name": "From SME B"})
        with pytest.raises(RuleBaseConflict, match="changed since it was read"):
            repo.save("default", doc_b, expected_version=version_b)

        # A's rule survives; B is told to retry rather than losing A's work.
        rules = repo.load()[0]["rules"]
        assert [r["name"] for r in rules] == ["First", "From SME A"]

    def test_write_is_atomic(self, repo, monkeypatch):
        """A crash mid-write must not truncate the record of approved rules."""
        original = json.dumps(RULE_DOC, indent=2)
        repo.path.write_text(original)
        doc, version = repo.load()

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("os.replace", boom)
        with pytest.raises(OSError):
            repo.save("default", doc, expected_version=version)

        assert json.loads(repo.path.read_text())["rules"][0]["id"] == "ALF-001"

    def test_next_rule_id(self, repo):
        assert repo.next_rule_id() == "ALF-002"

    def test_next_rule_id_ignores_malformed_ids(self, tmp_path):
        path = tmp_path / "r.json"
        path.write_text(
            json.dumps({"rules": [{"id": "not-an-alf-id"}, {"id": "ALF-007"}]})
        )
        assert JsonRuleRepository(path).next_rule_id() == "ALF-008"

    def test_missing_file_yields_an_empty_base(self, tmp_path):
        doc, version = JsonRuleRepository(tmp_path / "absent.json").load()
        assert doc["rules"] == [] and version == 0


class TestAuditSink:
    def test_events_are_durable_immediately(self, tmp_path):
        """No buffering: the session logger's crash-loses-everything bug."""
        sink = JsonlAuditSink(tmp_path / "audit.jsonl")
        ctx = RunContext(
            tenant_id="acme", case_id="case_005", actor="sme@x.com"
        )

        sink.emit(ctx, "rule_approved", {"rule_id": "ALF-001"})

        lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event_type"] == "rule_approved"
        assert record["tenant_id"] == "acme"
        assert record["actor"] == "sme@x.com", (
            "audit events must carry an actor"
        )

    def test_appends_rather_than_overwrites(self, tmp_path):
        sink = JsonlAuditSink(tmp_path / "audit.jsonl")
        ctx = RunContext()
        for i in range(3):
            sink.emit(ctx, "event", {"n": i})
        assert (
            len((tmp_path / "audit.jsonl").read_text().strip().splitlines())
            == 3
        )
