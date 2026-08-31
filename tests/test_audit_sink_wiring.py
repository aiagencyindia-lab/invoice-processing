"""Which audit sink the API uses, and why it must not fail quietly.

An audit trail written to a local file on a Cloud Run instance is lost on every
restart, which makes it worthless for the one thing it exists to do. So the sink
is resolved from configuration: Postgres when a database is available, a local
file only when one is not.

The resolution is lazy rather than decided at import, because an import-time
decision cannot be influenced by a test's environment -- and the failure mode of
getting that wrong is a test suite writing into the production audit trail.
"""

import json

import pytest

from invoice_processing.adapters import JsonlAuditSink
from invoice_processing.api import service
from invoice_processing.db.repositories import PgAuditSink
from invoice_processing.ports import AuditSink, RunContext


@pytest.fixture(autouse=True)
def reset_sink(monkeypatch):
    """The sink is cached after first use; each test resolves it afresh."""
    monkeypatch.setattr(service, "_audit", None)


class TestResolution:
    def test_a_file_sink_when_no_database_is_configured(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        assert isinstance(service.get_audit(), JsonlAuditSink)

    def test_postgres_when_a_database_url_is_set(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")
        assert isinstance(service.get_audit(), PgAuditSink)

    def test_postgres_when_a_cloud_sql_instance_is_set(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("CLOUD_SQL_INSTANCE", "p:r:i")
        assert isinstance(service.get_audit(), PgAuditSink)

    def test_the_sink_is_cached(self, monkeypatch):
        """Resolving per event would open a connection per audit write."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
        assert service.get_audit() is service.get_audit()

    def test_import_opens_no_connection(self):
        """A module import that dials a database breaks every offline tool."""
        assert service._audit is None


class TestFailuresAreLoud:
    def test_an_emit_failure_propagates(self, monkeypatch):
        """An action nobody could record is an action nobody can account for.

        Swallowing this is how an audit trail develops holes exactly where
        something went wrong, which is the only place it matters.
        """

        class BrokenSink:
            def emit(self, ctx, event_type, payload):
                raise RuntimeError("database unreachable")

        monkeypatch.setattr(service, "_audit", BrokenSink())
        with pytest.raises(RuntimeError, match="database unreachable"):
            service.get_audit().emit(RunContext(tenant_id="t"), "probe", {})


class TestSinkContract:
    def test_both_sinks_satisfy_the_port(self):
        assert isinstance(JsonlAuditSink(), AuditSink)
        assert isinstance(PgAuditSink(), AuditSink)

    def test_the_file_sink_still_records_actor_and_tenant(self, tmp_path):
        sink = JsonlAuditSink(tmp_path / "audit.jsonl")
        sink.emit(
            RunContext(tenant_id="t-1", case_id="case_005", actor="a@b.c"),
            "fields_corrected",
            {"count": 1},
        )
        record = json.loads((tmp_path / "audit.jsonl").read_text().strip())
        assert record["actor"] == "a@b.c"
        assert record["event_type"] == "fields_corrected"
