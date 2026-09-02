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

from invoice_processing.db import session as session_module
from invoice_processing.db.models import (
    TENANT_TABLES,
    Base,
    Correction,
    Document,
    Finding,
    Invoice,
    InvoiceLine,
    PipelineRun,
    Tenant,
    User,
)
from invoice_processing.db.repositories import (
    PgAuditSink,
    PgCaseRepository,
    PgCorrectionStore,
    PgRuleRepository,
    entity_type_for,
)
from invoice_processing.db.rls import enable_rls_statements
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import (
    AuditSink,
    CaseRepository,
    CorrectionStore,
    RuleBaseConflict,
    RuleRepository,
    RunContext,
)
from invoice_processing.worker.runner import seen_invoices_for

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


class TestAnEmptyUrlIsNotAUrl:
    """`DATABASE_URL=` means "unset", and must not crash.

    Blanking a variable that way is how this repo's own run commands and the
    end-to-end harness switch off cloud services. A getenv default does not
    apply to a variable that is present but empty, so the empty string reached
    `create_engine` and failed with "Could not parse SQLAlchemy URL" -- a
    message naming neither the variable nor the fix.
    """

    def test_empty_falls_back_to_sqlite(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "")
        assert session_module.database_url() == "sqlite://"

    def test_absent_falls_back_to_sqlite(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert session_module.database_url() == "sqlite://"

    def test_a_configured_url_still_wins(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:///configured.db")
        assert session_module.database_url() == "sqlite:///configured.db"


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


ARTIFACTS = {
    "extraction": {
        "invoice": {
            "invoice_number": "INV-00042",
            "invoice_date": "2026-02-11",
            "due_date": "2026-03-11",
            "po_number": "PO-9",
            "currency": "AUD",
            "vendor_name": "QuickFix HVAC Repairs",
            "vendor_tax_id": "51824753556",
            "invoice_total_ex_tax": 1000.0,
            "tax_amount": 100.0,
            "invoice_total_inc_tax": 1100.0,
            "bank_account_number": "12345678",
            "_routing": "REVIEW",
            "_evidence": {"_summary": {"min_confidence": 0.3}},
            "line_items": [
                {"description": "Labour", "amount_ex_tax": 700.0},
                {"description": "Parts", "amount_ex_tax": 300.0},
            ],
        }
    },
    "transformer": {
        "line_items_mapped": [
            {
                "line_number": 1,
                "item_code": "LABOUR",
                "description": "Labour",
                "quantity": "7.00",
                "unit_cost": "100.00",
                "line_cost": "700.00",
                "tax": "70.00",
                "tax_code": "TAX",
            },
            {
                "line_number": 2,
                "item_code": "PARTS",
                "description": "Parts",
                "quantity": "1.00",
                "unit_cost": "300.00",
                "line_cost": "300.00",
                "tax": "30.00",
                "tax_code": "TAX",
            },
        ]
    },
    "phase1": {
        "validations": [
            {"step": "1.1", "rule": "Extraction succeeded", "passed": True},
            {
                "step": "1.3",
                "rule": "Tax invoice",
                "passed": False,
                "rejection_template": "Not a tax invoice",
            },
        ]
    },
    "phase4": {
        "validations": [
            {
                "step": "4.1",
                "rule": "Total = Subtotal + Tax",
                "passed": False,
                "informational": True,
            }
        ]
    },
    "decision": {
        "final_decision": "REJECT",
        "rejection_template": "Not a tax invoice",
    },
    "audit_log": {
        "version": "1.0",
        "processing_summary": {
            "processing_time_seconds": 56.61,
            "total_llm_calls": 5,
            "total_cost_usd": 0.010035,
        },
    },
}


class TestProjection:
    """A decision nobody can query is a decision nobody can defend.

    Everything the pipeline produced used to land in one JSON column: both
    documented indexes indexed nothing but nulls, the review queue was sorted
    in Python after loading every case, and three tables that were designed,
    migrated and deployed held no rows at all.
    """

    def _project(self, db, case_id="case-1", artifacts=None):
        cases = PgCaseRepository(db)
        ctx = RunContext(tenant_id=ACME, case_id=case_id)
        cases.project_case(
            ctx, artifacts if artifacts is not None else ARTIFACTS
        )
        return cases, ctx

    def test_the_typed_columns_are_written(self, db):
        self._project(db)
        with tenant_session(ACME, db) as session:
            invoice = session.query(Invoice).filter_by(case_id="case-1").one()
            assert invoice.vendor_name == "QuickFix HVAC Repairs"
            assert invoice.invoice_number == "INV-00042"
            assert invoice.vendor_tax_id == "51824753556"
            assert float(invoice.total_inc_tax) == 1100.0
            assert invoice.decision == "REJECT"
            assert invoice.decision_reason == "Not a tax invoice"
            assert invoice.routing == "REVIEW"
            assert float(invoice.min_confidence) == 0.3

    def test_status_moves_off_received_once_a_decision_exists(self, db):
        self._project(db)
        with tenant_session(ACME, db) as session:
            invoice = session.query(Invoice).filter_by(case_id="case-1").one()
            assert invoice.status == "processed"

    def test_a_case_without_a_decision_stays_received(self, db):
        self._project(
            db,
            case_id="case-2",
            artifacts={"extraction": ARTIFACTS["extraction"]},
        )
        with tenant_session(ACME, db) as session:
            invoice = session.query(Invoice).filter_by(case_id="case-2").one()
            assert invoice.status == "received"

    def test_line_items_become_rows(self, db):
        self._project(db)
        with tenant_session(ACME, db) as session:
            lines = (
                session.query(InvoiceLine)
                .order_by(InvoiceLine.line_number)
                .all()
            )
            assert [line.item_code for line in lines] == ["LABOUR", "PARTS"]
            assert float(lines[0].amount_ex_tax) == 700.0
            assert float(lines[0].quantity) == 7.0

    def test_findings_become_rows_with_severity(self, db):
        self._project(db)
        with tenant_session(ACME, db) as session:
            findings = {f.step: f for f in session.query(Finding).all()}
            assert findings["1.1"].severity == "pass"
            assert findings["1.3"].severity == "blocking"
            assert findings["1.3"].passed is False
            # 4.1 fails but is informational -- recording it as blocking is
            # how a reviewer comes to treat a note as a rejection.
            assert findings["4.1"].severity == "info"

    def test_the_run_is_recorded_with_its_cost(self, db):
        self._project(db)
        with tenant_session(ACME, db) as session:
            run = session.query(PipelineRun).one()
            assert run.llm_calls == 5
            assert float(run.cost_usd) == 0.010035
            assert run.duration_ms == 56610

    def test_reprojecting_does_not_duplicate_lines_or_findings(self, db):
        """A retry re-runs the pipeline over the same case."""
        self._project(db)
        self._project(db)
        with tenant_session(ACME, db) as session:
            assert session.query(InvoiceLine).count() == 2
            assert session.query(Finding).count() == 3

    def test_the_document_is_linked_when_it_exists(self, db):
        with tenant_session(ACME, db) as session:
            session.add(
                Document(
                    id="case-1",
                    tenant_id=ACME,
                    storage_key="documents/x.pdf",
                    sha256="x",
                )
            )
        self._project(db)
        with tenant_session(ACME, db) as session:
            invoice = session.query(Invoice).filter_by(case_id="case-1").one()
            assert invoice.document_id == "case-1"

    def test_a_case_with_no_document_row_still_projects(self, db):
        """A dangling foreign key would fail the whole write."""
        self._project(db, case_id="orphan")
        with tenant_session(ACME, db) as session:
            invoice = session.query(Invoice).filter_by(case_id="orphan").one()
            assert invoice.document_id is None
            assert invoice.vendor_name == "QuickFix HVAC Repairs"

    def test_the_projection_stays_inside_the_tenant(self, db):
        self._project(db)
        with tenant_session(GLOBEX, db) as session:
            assert (
                session.query(Invoice).filter_by(tenant_id=GLOBEX).count() == 0
            )
            assert (
                session.query(InvoiceLine).filter_by(tenant_id=GLOBEX).count()
                == 0
            )


class TestCorrections:
    """The asset that compounds -- kept where a deploy cannot erase it.

    Corrections were appended to a file on the API container's disk. The audit
    trail recorded that fields had been corrected and by whom; the old and new
    values, which are the whole training signal, lived only in that file and
    went with the container.
    """

    def _store(self, db):
        return PgCorrectionStore(db), RunContext(
            tenant_id=ACME, case_id="case-1"
        )

    def _correction(self, **overrides):
        record = {
            "case_id": "case-1",
            "field_path": "vendor_name",
            "old_value": "Acme Crp",
            "new_value": "Acme Corp",
            "actor": "clerk@acme.test",
            "tenant_id": ACME,
        }
        record.update(overrides)
        return record

    def test_it_satisfies_the_port(self, db):
        assert isinstance(PgCorrectionStore(db), CorrectionStore)

    def test_a_correction_survives_as_a_row(self, db):
        store, ctx = self._store(db)
        store.record(ctx, self._correction())

        with tenant_session(ACME, db) as session:
            saved = session.query(Correction).one()
            assert saved.field_path == "vendor_name"
            assert saved.old_value == "Acme Crp"
            assert saved.new_value == "Acme Corp"

    def test_it_reads_back_in_the_shape_the_workbench_expects(self, db):
        store, ctx = self._store(db)
        store.record(ctx, self._correction())

        [read] = store.list(ACME, "case-1")
        assert read["case_id"] == "case-1"
        assert read["field_path"] == "vendor_name"
        assert read["old_value"] == "Acme Crp"
        assert read["new_value"] == "Acme Corp"
        assert read["at"] is not None

    def test_the_actor_is_resolved_to_the_user_who_made_it(self, db):
        with tenant_session(ACME, db) as session:
            session.add(
                User(
                    id="u-1",
                    tenant_id=ACME,
                    email="clerk@acme.test",
                    role="clerk",
                )
            )
        store, ctx = self._store(db)
        store.record(ctx, self._correction())

        [read] = store.list(ACME, "case-1")
        assert read["actor"] == "clerk@acme.test"

    def test_an_unknown_actor_still_keeps_the_correction(self, db):
        """Attribution is in the audit trail either way. Losing the values
        because the email did not match a row would be the worse trade."""
        store, ctx = self._store(db)
        store.record(ctx, self._correction(actor="ghost@nowhere.test"))

        [read] = store.list(ACME, "case-1")
        assert read["new_value"] == "Acme Corp"
        assert read["actor"] is None

    def test_a_non_string_value_is_kept_readable(self, db):
        store, ctx = self._store(db)
        store.record(
            ctx,
            self._correction(
                field_path="invoice_total_inc_tax",
                old_value=1100.5,
                new_value=None,
            ),
        )

        [read] = store.list(ACME, "case-1")
        assert read["old_value"] == "1100.5"
        assert read["new_value"] is None

    def test_corrections_do_not_cross_tenants(self, db):
        store, ctx = self._store(db)
        store.record(ctx, self._correction())

        assert store.list(GLOBEX) == []

    def test_listing_filters_by_case(self, db):
        store, _ = self._store(db)
        store.record(
            RunContext(tenant_id=ACME, case_id="case-1"), self._correction()
        )
        store.record(
            RunContext(tenant_id=ACME, case_id="case-2"),
            self._correction(case_id="case-2", field_path="currency"),
        )

        assert [c["field_path"] for c in store.list(ACME, "case-2")] == [
            "currency"
        ]
        assert len(store.list(ACME)) == 2


class TestDuplicateCorpusIsTenantScoped:
    """Duplicate detection compares against other invoices. Which other
    invoices is a tenancy question, and it was answered by a directory scan
    with no tenant in it -- so a worker serving two tenants could report one
    tenant's invoice as a duplicate of another tenant's case, naming that case
    id in the validation record a reviewer reads.
    """

    def _store(self, db, tenant_id, case_id, invoice):
        PgCaseRepository(db).put_artifact(
            RunContext(tenant_id=tenant_id, case_id=case_id),
            "extraction",
            {"invoice": invoice},
        )

    def test_the_corpus_holds_only_this_tenants_cases(self, db):
        self._store(
            db, ACME, "acme-case", {"vendor_name": "A", "invoice_number": "1"}
        )
        self._store(
            db,
            GLOBEX,
            "globex-case",
            {"vendor_name": "G", "invoice_number": "2"},
        )
        cases = PgCaseRepository(db)

        acme_corpus = seen_invoices_for(
            RunContext(tenant_id=ACME, case_id="new"), cases
        )()
        globex_corpus = seen_invoices_for(
            RunContext(tenant_id=GLOBEX, case_id="new"), cases
        )()

        assert [key.case_id for key in acme_corpus] == ["acme-case"]
        assert [key.case_id for key in globex_corpus] == ["globex-case"]

    def test_it_survives_a_restart(self, db):
        """The old corpus was container-local disk: a redeploy emptied it and
        a duplicate submitted afterwards went undetected. A second repository
        instance stands in for the process that comes back."""
        self._store(
            db, ACME, "earlier", {"vendor_name": "A", "invoice_number": "1"}
        )

        corpus = seen_invoices_for(
            RunContext(tenant_id=ACME, case_id="new"), PgCaseRepository(db)
        )()

        assert [key.case_id for key in corpus] == ["earlier"]


class TestTheTrailCanAnswerWhatChanged:
    """An after-state with nothing to compare it to is not an audit trail.

    In production `before`, `ip` and `request_id` were null on every one of 44
    rows, and `entity_type` was null on 33 -- so the trail could say that
    fields were corrected and by whom, but not what they had been, from where,
    or which events belonged to the same action.
    """

    def test_before_is_recorded_when_it_is_given(self, db):
        sink = PgAuditSink(db)
        ctx = RunContext(tenant_id=ACME, case_id="case-1", actor="a@acme.test")

        sink.emit(
            ctx,
            "fields_corrected",
            {"values": {"vendor_name": "Acme Corp"}},
            before={"values": {"vendor_name": "Acme Crp"}},
        )

        [event] = sink.query(ACME)
        assert event.before == {"values": {"vendor_name": "Acme Crp"}}
        assert event.after == {"values": {"vendor_name": "Acme Corp"}}

    def test_the_request_id_and_address_are_recorded(self, db):
        sink = PgAuditSink(db)
        ctx = RunContext(
            tenant_id=ACME,
            case_id="case-1",
            run_id="req-123",
            actor="a@acme.test",
            settings={"ip": "203.0.113.7"},
        )

        sink.emit(ctx, "invoice_approved", {})

        [event] = sink.query(ACME)
        assert event.request_id == "req-123"
        assert event.ip == "203.0.113.7"

    def test_an_event_says_what_it_is_about(self, db):
        assert entity_type_for("document_ingested") == "document"
        assert entity_type_for("email_received") == "document"
        assert entity_type_for("fields_corrected") == "invoice"
        assert entity_type_for("invoice_approved") == "invoice"
        assert entity_type_for("settings_changed") == "settings"
        assert entity_type_for("tenant_self_provisioned") == "tenant"

    def test_a_document_event_is_not_labelled_an_invoice(self, db):
        """It was, whenever a case id happened to be present."""
        sink = PgAuditSink(db)
        sink.emit(
            RunContext(tenant_id=ACME, case_id="doc-1", actor="a@acme.test"),
            "document_ingested",
            {},
        )

        [event] = sink.query(ACME)
        assert event.entity_type == "document"
