"""The multi-tenant spine, with isolation as the property that matters.

Everything else in the platform is recoverable. One tenant reading another's
invoices is not: it is a breach, it is reportable, and it ends a pilot. So these
tests assert isolation directly at the query layer rather than trusting the
database to enforce it.

That is not redundant with row-level security -- it is deliberate. RLS catches a
query that forgets its filter; the filter catches a policy that was never
applied or a role that bypasses it. Either alone leaves one mistake between a
customer and someone else's data. The tests run on SQLite, which has no RLS at
all, so they exercise the application-layer half; the policy half is covered by
test_rls_policies.py.
"""

import pytest

from invoice_processing.db.models import (
    TENANT_TABLES,
    Base,
    Invoice,
    Tenant,
)
from invoice_processing.db.repositories import (
    PgAuditSink,
    PgCaseRepository,
    PgRuleRepository,
)
from invoice_processing.db.rls import enable_rls_statements
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import (
    AuditSink,
    CaseRepository,
    RuleBaseConflict,
    RuleRepository,
    RunContext,
)

ACME = "tenant-acme"
GLOBEX = "tenant-globex"


@pytest.fixture
def db(tmp_path):
    """A fresh SQLite database with two tenants."""
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = get_engine(url)
    Base.metadata.drop_all(engine)
    create_all(url)
    for tenant_id, slug in ((ACME, "acme"), (GLOBEX, "globex")):
        with tenant_session(tenant_id, url) as session:
            session.add(Tenant(id=tenant_id, slug=slug, name=slug.title()))
    return url


class TestSchema:
    def test_every_tenant_table_carries_tenant_id(self, db):
        for table in TENANT_TABLES:
            assert "tenant_id" in Base.metadata.tables[table].columns, (
                f"{table} has no tenant_id and cannot be isolated"
            )

    def test_money_is_not_float(self, db):
        """A float total is a rounding argument with a supplier waiting to happen."""
        for column in ("total_ex_tax", "tax_amount", "total_inc_tax"):
            kind = Invoice.__table__.columns[column].type.__class__.__name__
            assert kind == "Numeric", f"{column} is {kind}, expected Numeric"


class TestCrossTenantIsolation:
    """The property whose failure ends a pilot."""

    def _seed(self, url, tenant_id, case_id):
        repo = PgCaseRepository(url)
        ctx = RunContext(tenant_id=tenant_id, case_id=case_id)
        repo.put_artifact(
            ctx, "extraction", {"invoice": {"vendor_name": tenant_id}}
        )
        return repo

    def test_one_tenant_cannot_read_anothers_case(self, db):
        self._seed(db, ACME, "case_secret")
        repo = PgCaseRepository(db)

        mine = repo.get_artifacts(
            RunContext(tenant_id=ACME, case_id="case_secret")
        )
        theirs = repo.get_artifacts(
            RunContext(tenant_id=GLOBEX, case_id="case_secret")
        )

        assert mine, "tenant cannot read its own case"
        assert theirs == {}, "cross-tenant read succeeded"

    def test_case_listing_is_scoped(self, db):
        self._seed(db, ACME, "case_a")
        self._seed(db, GLOBEX, "case_b")
        repo = PgCaseRepository(db)

        assert list(repo.iter_case_ids(ACME)) == ["case_a"]
        assert list(repo.iter_case_ids(GLOBEX)) == ["case_b"]

    def test_batch_read_cannot_reach_across_tenants(self, db):
        """Impact assessment batches ids; a leak here is silent and wide."""
        self._seed(db, ACME, "case_a")
        self._seed(db, GLOBEX, "case_b")
        repo = PgCaseRepository(db)

        batch = repo.get_many_artifacts(ACME, ["case_a", "case_b"])
        assert set(batch) == {"case_a"}, "batch read crossed a tenant boundary"

    def test_rules_are_scoped(self, db):
        repo = PgRuleRepository(db)
        doc, version = repo.load(ACME)
        doc["rules"].append(
            {
                "id": "ALF-001",
                "name": "Acme only",
                "conditions": [],
                "actions": [],
            }
        )
        repo.save(ACME, doc, expected_version=version)

        assert len(repo.load(ACME)[0]["rules"]) == 1
        assert repo.load(GLOBEX)[0]["rules"] == [], "rule leaked across tenants"

    def test_audit_events_are_scoped(self, db):
        sink = PgAuditSink(db)
        sink.emit(
            RunContext(tenant_id=ACME, actor="a@acme.com"), "rule_approved", {}
        )

        assert len(sink.query(ACME)) == 1
        assert sink.query(GLOBEX) == [], "audit event leaked across tenants"

    def test_case_ids_may_repeat_across_tenants(self, db):
        """Two tenants naming a case the same must not collide or overwrite."""
        self._seed(db, ACME, "case_001")
        self._seed(db, GLOBEX, "case_001")
        repo = PgCaseRepository(db)

        acme = repo.get_artifacts(
            RunContext(tenant_id=ACME, case_id="case_001")
        )
        globex = repo.get_artifacts(
            RunContext(tenant_id=GLOBEX, case_id="case_001")
        )
        assert acme["extraction"]["invoice"]["vendor_name"] == ACME
        assert globex["extraction"]["invoice"]["vendor_name"] == GLOBEX


class TestPortConformance:
    def test_repositories_satisfy_the_phase_0_ports(self, db):
        assert isinstance(PgCaseRepository(db), CaseRepository)
        assert isinstance(PgRuleRepository(db), RuleRepository)
        assert isinstance(PgAuditSink(db), AuditSink)


class TestCaseRepository:
    def test_artifacts_round_trip(self, db):
        repo = PgCaseRepository(db)
        ctx = RunContext(tenant_id=ACME, case_id="case_001")
        repo.put_artifact(ctx, "extraction", {"waf_count": 1})
        repo.put_artifact(ctx, "phase1", {"decision": "CONTINUE"})

        artifacts = repo.get_artifacts(ctx)
        assert artifacts["extraction"] == {"waf_count": 1}
        assert artifacts["phase1"] == {"decision": "CONTINUE"}

    def test_writing_one_artifact_preserves_the_others(self, db):
        """JSON columns are not tracked in place; a mutation would be lost."""
        repo = PgCaseRepository(db)
        ctx = RunContext(tenant_id=ACME, case_id="case_001")
        repo.put_artifact(ctx, "extraction", {"a": 1})
        repo.put_artifact(ctx, "phase1", {"b": 2})
        assert set(repo.get_artifacts(ctx)) == {"extraction", "phase1"}

    def test_iteration_order_is_deterministic(self, db):
        repo = PgCaseRepository(db)
        for case in ("case_003", "case_001", "case_002"):
            repo.put_artifact(RunContext(tenant_id=ACME, case_id=case), "e", {})
        assert list(repo.iter_case_ids(ACME)) == [
            "case_001",
            "case_002",
            "case_003",
        ]

    def test_unknown_case_returns_empty(self, db):
        repo = PgCaseRepository(db)
        assert (
            repo.get_artifacts(RunContext(tenant_id=ACME, case_id="nope")) == {}
        )


class TestRuleRepositoryConcurrency:
    def test_concurrent_write_is_rejected(self, db):
        """Two SMEs approving at once must not silently lose one's work."""
        repo = PgRuleRepository(db)
        doc_a, version_a = repo.load(ACME)
        doc_b, version_b = repo.load(ACME)

        doc_a["rules"].append(
            {"id": "ALF-001", "name": "From A", "conditions": [], "actions": []}
        )
        repo.save(ACME, doc_a, expected_version=version_a)

        doc_b["rules"].append(
            {"id": "ALF-002", "name": "From B", "conditions": [], "actions": []}
        )
        with pytest.raises(RuleBaseConflict):
            repo.save(ACME, doc_b, expected_version=version_b)

        names = [r["name"] for r in repo.load(ACME)[0]["rules"]]
        assert names == ["From A"], "A's rule did not survive"

    def test_next_rule_id_is_per_tenant(self, db):
        repo = PgRuleRepository(db)
        doc, version = repo.load(ACME)
        doc["rules"].append(
            {"id": "ALF-007", "name": "x", "conditions": [], "actions": []}
        )
        repo.save(ACME, doc, expected_version=version)

        assert repo.next_rule_id(ACME) == "ALF-008"
        assert repo.next_rule_id(GLOBEX) == "ALF-001", (
            "id space leaked across tenants"
        )


class TestAuditTrail:
    def test_event_records_actor_and_action(self, db):
        sink = PgAuditSink(db)
        ctx = RunContext(
            tenant_id=ACME, case_id="case_005", actor="sme@acme.com"
        )
        sink.emit(ctx, "rule_approved", {"rule_id": "ALF-001"})

        event = sink.query(ACME)[0]
        assert event.action == "rule_approved"
        assert event.actor == "sme@acme.com"
        assert event.entity_id == "case_005"
        assert event.after == {"rule_id": "ALF-001"}

    def test_events_accumulate(self, db):
        sink = PgAuditSink(db)
        ctx = RunContext(tenant_id=ACME)
        for i in range(3):
            sink.emit(ctx, "event", {"n": i})
        assert len(sink.query(ACME)) == 3

    def test_system_actor_when_no_user(self, db):
        sink = PgAuditSink(db)
        sink.emit(RunContext(tenant_id=ACME), "pipeline_run", {})
        assert sink.query(ACME)[0].actor_type == "system"


def test_audit_table_has_no_update_or_delete_policy():
    """Append-only by construction. The absence of the policies is the control."""
    statements = " ".join(enable_rls_statements())
    assert "audit_events_select" in statements
    assert "audit_events_insert" in statements
    assert "audit_events_isolation" not in statements, (
        "a general policy would permit UPDATE and DELETE on the audit trail"
    )


def test_rls_forces_policies_on_the_table_owner():
    """Without FORCE, the app's own role bypasses every policy."""
    statements = enable_rls_statements()
    for table in TENANT_TABLES:
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;" in statements
