"""Phase 1 acceptance sweep.

Answers one question mechanically: is Phase 1 actually done?

It exists because "complete" was previously asserted from memory and was wrong.
Components had been built, tested in isolation, and never connected -- four
production adapters existed, passed their tests, and were instantiated nowhere.
A green suite plus a working demo is not the same as a working system, and only
a check that exercises the wiring can tell the difference.

Two tiers, because half of Phase 1 is infrastructure:

  OFFLINE  runs anywhere with no credentials. This is what CI runs.
  LIVE     (--live) exercises Cloud SQL, GCS and the real schema.

A check that cannot run reports SKIPPED with the reason. It never reports PASS.
Conflating "unverified" with "verified" is precisely the failure this sweep was
written to catch, so it must not commit that error itself.

    python scripts/verify_phase1.py
    python scripts/verify_phase1.py --live
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str, str]] = []


class Skip(Exception):
    """This check cannot run here. Not a failure, and not a pass either."""


def check(item: str, name: str, fn) -> None:
    try:
        results.append((item, name, PASS, fn() or ""))
    except Skip as exc:
        results.append((item, name, SKIP, str(exc)))
    except AssertionError as exc:
        results.append((item, name, FAIL, str(exc)[:200]))
    except Exception as exc:
        results.append(
            (item, name, FAIL, f"{type(exc).__name__}: {str(exc)[:180]}")
        )


def source(*parts: str) -> str:
    return (REPO.joinpath(*parts)).read_text(encoding="utf-8")


# ===========================================================================
# OFFLINE
# ===========================================================================


# -- 1.1 schema, migration, RLS --------------------------------------------
def schema_covers_the_pilot_tables():
    from invoice_processing.db.models import Base

    tables = set(Base.metadata.tables)
    required = {
        "tenants",
        "users",
        "documents",
        "invoices",
        "invoice_lines",
        "findings",
        "pipeline_runs",
        "rules",
        "corrections",
        "audit_events",
    }
    missing = required - tables
    assert not missing, f"missing tables: {sorted(missing)}"
    return f"{len(tables)} tables"


def every_tenant_table_has_a_policy():
    from invoice_processing.db.models import TENANT_TABLES
    from invoice_processing.db.rls import enable_rls_statements

    statements = " ".join(enable_rls_statements())
    for table in TENANT_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in statements, (
            table
        )
        # FORCE matters: without it the table owner -- which is the app's own
        # role, since it runs the migration -- bypasses every policy.
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in statements, (
            table
        )
    return f"{len(TENANT_TABLES)} tables, RLS forced"


def the_audit_table_is_append_only():
    from invoice_processing.db.rls import enable_rls_statements

    statements = " ".join(enable_rls_statements())
    assert "audit_events_select" in statements
    assert "audit_events_insert" in statements
    assert "audit_events_update" not in statements, (
        "an UPDATE policy would allow tampering"
    )
    assert "audit_events_delete" not in statements, (
        "a DELETE policy would allow erasure"
    )
    return "SELECT+INSERT only"


def the_migration_carries_rls():
    path = next(
        (REPO / "invoice_processing/db/migrations/versions").glob(
            "*_initial_schema.py"
        )
    )
    text = path.read_text()
    assert "enable_rls_statements" in text
    assert 'dialect.name == "postgresql"' in text, "RLS must be dialect-guarded"
    return path.name


# -- 1.2 storage ------------------------------------------------------------
def blob_keys_cannot_escape_the_tenant():
    from invoice_processing.gcs import GcsBlobStore

    store = GcsBlobStore("bucket", tenant_id="acme", client=object())
    assert store._key("case/x.pdf") == "t/acme/case/x.pdf"
    for hostile in ("../other/x.pdf", "a/../../b", ".."):
        try:
            store._key(hostile)
        except ValueError:
            continue
        raise AssertionError(f"{hostile!r} was not refused")
    return "prefixed, traversal refused"


# -- 1.3 queue and worker ---------------------------------------------------
def the_queue_is_at_least_once():
    import tempfile

    from invoice_processing.worker.queue import LocalQueue

    clock = [1000.0]
    with tempfile.TemporaryDirectory() as tmp:
        q = LocalQueue(tmp, ack_deadline_seconds=60, clock=lambda: clock[0])
        q.publish({"a": 1})
        [m] = q.pull()
        assert m.delivery_attempt == 1
        clock[0] += 61  # the worker died holding it
        [again] = q.pull()
        assert again.delivery_attempt == 2, (
            "an expired lease was not redelivered"
        )
    return "lease expiry redelivers"


def the_worker_is_idempotent():
    import tempfile

    from invoice_processing.worker.queue import LocalQueue
    from invoice_processing.worker.runner import ClaimStore, Worker

    with tempfile.TemporaryDirectory() as tmp:
        q = LocalQueue(Path(tmp) / "q")
        claims = ClaimStore(Path(tmp) / "c")
        calls = []
        worker = Worker(q, claims, handler=lambda m: calls.append(1) or {})
        payload = {"tenant_id": "t", "document_id": "d"}
        q.publish(payload)
        worker.run_once()
        q.publish(payload)  # redelivered after a lost ack
        worker.run_once()
        assert len(calls) == 1, "the same document was processed twice"
    return "redelivery does not reprocess"


def poison_messages_are_dead_lettered():
    import tempfile

    from invoice_processing.worker.queue import LocalQueue
    from invoice_processing.worker.runner import ClaimStore, Worker

    def boom(_):
        raise RuntimeError("bad pdf")

    with tempfile.TemporaryDirectory() as tmp:
        q = LocalQueue(Path(tmp) / "q")
        worker = Worker(
            q, ClaimStore(Path(tmp) / "c"), handler=boom, max_attempts=2
        )
        q.publish({"tenant_id": "t", "document_id": "d"})
        outcomes = [worker.run_once()[0] for _ in range(2)]
        assert outcomes[-1].startswith("dead-lettered"), outcomes
        assert q.dead_letters(), "the message was dropped rather than parked"
    return "parked after max attempts"


# -- 1.4 auth ---------------------------------------------------------------
def the_tenant_comes_only_from_the_token():
    from invoice_processing.api.auth import AuthError, principal_from_claims

    ok = principal_from_claims(
        {
            "sub": "u",
            "email": "a@b.c",
            "email_verified": True,
            "tenant_id": "t-1",
            "role": "clerk",
        }
    )
    assert ok.tenant_id == "t-1"
    try:
        principal_from_claims({"sub": "u", "email": "a@b.c"})
    except AuthError:
        pass
    else:
        raise AssertionError("a token with no tenant claim was accepted")
    return "no tenant claim -> refused"


def no_endpoint_accepts_a_tenant():
    from invoice_processing.api.service import app

    # A tenant as a path *parameter*, not the substring: `/api/admin/tenants`
    # is a resource name, and the danger is `/api/{tenant_id}/...` -- a value
    # the caller supplies.
    offenders = [
        r.path
        for r in app.routes
        if re.search(
            r"\{[^}]*tenant[^}]*\}", getattr(r, "path", ""), re.IGNORECASE
        )
    ]
    assert not offenders, offenders
    return f"{len(app.routes)} routes, none tenant-parameterised"


def the_dev_bypass_is_allow_listed():
    from invoice_processing.api import auth

    saved = dict(os.environ)
    try:
        os.environ["AUTH_DISABLED"] = "1"
        for env in ("production", "prd", "live", "production-eu", ""):
            os.environ["ENV"] = env
            assert auth.dev_principal() is None, (
                f"bypass active with ENV={env!r}"
            )
        os.environ["ENV"] = "local"
        assert auth.dev_principal() is not None
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return "only known-local environments"


# -- 1.5 ingestion ----------------------------------------------------------
def ingestion_refuses_what_it_should():
    from invoice_processing.api.ingest import IngestionRefused, check_acceptable

    check_acceptable(b"%PDF-1.4 ok", "a.pdf")
    for data, why in ((b"", "empty"), (b"MZ\x90 exe", "not a pdf")):
        try:
            check_acceptable(data, "a.pdf")
        except IngestionRefused:
            continue
        raise AssertionError(f"{why} was accepted")
    return "magic bytes checked"


def storage_keys_are_content_addressed():
    from invoice_processing.api.ingest import storage_key_for

    key = storage_key_for("a" * 64)
    assert ".." not in key and key.startswith("documents/")
    return key[:24] + "..."


def the_webhook_fails_closed():
    text = source("invoice_processing/api/ingest.py")
    assert "compare_digest" in text, (
        "the secret must be compared in constant time"
    )
    assert "503" in text, "an unconfigured webhook must refuse, not run open"
    return "503 when unconfigured, constant-time compare"


# -- 1.6 confidence ---------------------------------------------------------
def confidence_routing_is_applied():
    from invoice_processing import confidence

    high = confidence.routing_decision(
        {"_summary": {"min_confidence": 1.0, "has_text_layer": True}}
    )
    low = confidence.routing_decision(
        {"_summary": {"min_confidence": 0.3, "has_text_layer": True}}
    )
    assert high != low, "routing does not vary with confidence"
    assert "routing_decision" in source(
        "invoice_processing/shared_libraries/acting/general_invoice_agent.py"
    ), "the pipeline never calls it"
    return f"{high} vs {low}"


# -- 1.7 duplicates ---------------------------------------------------------
def duplicate_detection_is_real():
    from invoice_processing import duplicates

    text = source(
        "invoice_processing/shared_libraries/acting/general_invoice_agent.py"
    )
    assert "Duplicate check not implemented" not in text, "still the old stub"
    assert "find_duplicates" in text, "the pipeline does not call it"
    assert hasattr(duplicates, "find_duplicates")
    if '"duplicate_check_enabled": False' in text:
        return "implemented and wired, but DISABLED by default"
    return "implemented, wired, enabled"


def po_matching_is_real():
    """Step 2.2 reported passed=True while checking nothing."""
    from invoice_processing import po_matching

    text = source(
        "invoice_processing/shared_libraries/acting/general_invoice_agent.py"
    )
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    assert (
        "NOT_IMPLEMENTED" not in code
        or "2.2" not in code.split("NOT_IMPLEMENTED")[0][-400:]
    ), "step 2.2 still reports NOT_IMPLEMENTED"
    assert "po_matching.match_invoice_to_po" in code, (
        "the pipeline does not call the matcher"
    )

    class NoOrders:
        def find(self, po_number):
            return None

    # A check that can see no purchase orders must not report success.
    blocked = po_matching.match_invoice_to_po(
        {"po_number": "PO-1", "vendor_name": "A", "invoice_total_inc_tax": 10},
        NoOrders(),
    )
    assert blocked.blocking, "an unknown purchase order passed"

    # The failure that matters most: someone else's commitment.
    class OneOrder:
        def find(self, po_number):
            return {
                "po_number": "PO-1",
                "vendor_name": "Acme Pty Ltd",
                "total_amount": 1000.0,
                "currency": "AUD",
                "status": "open",
            }

    wrong_vendor = po_matching.match_invoice_to_po(
        {
            "po_number": "PO-1",
            "vendor_name": "Globex Inc",
            "invoice_total_inc_tax": 10,
            "currency": "AUD",
        },
        OneOrder(),
    )
    assert wrong_vendor.outcome == po_matching.VENDOR_MISMATCH
    return "two-way match; unknown PO and vendor mismatch both block"


# -- 1.8 workbench ----------------------------------------------------------
def the_workbench_serves_a_review_ui():
    from invoice_processing.api.service import app

    paths_ = {getattr(r, "path", "") for r in app.routes}
    for required in ("/", "/api/invoices", "/api/invoices/{case_id}/fields"):
        assert required in paths_, required
    # The single-file workbench was replaced by the Next.js app in frontend/.
    # The check follows it: what matters is that a reviewer can reach the whole
    # flow, not which file the markup lives in.
    workbench = source("frontend/src/app/invoices/page.tsx")
    assert "keydown" in workbench, "the UI is not keyboard-driven"
    missing = [w for w in ("upload", "export") if w not in workbench.lower()]
    if missing:
        return f"present, but no UI control for: {', '.join(missing)}"
    return "queue, detail, corrections, actions (frontend/)"


# -- 1.9 audit --------------------------------------------------------------
def the_audit_sink_follows_configuration():
    from invoice_processing.adapters import JsonlAuditSink
    from invoice_processing.api import service
    from invoice_processing.db.repositories import PgAuditSink

    saved = dict(os.environ)
    try:
        service._audit = None
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("CLOUD_SQL_INSTANCE", None)
        assert isinstance(service.get_audit(), JsonlAuditSink)
        service._audit = None
        os.environ["DATABASE_URL"] = "postgresql://example/db"
        assert isinstance(service.get_audit(), PgAuditSink)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        service._audit = None
    return "Postgres when configured, file otherwise"


# -- 1.10 export ------------------------------------------------------------
def export_requires_approval_and_posts_once():
    text = source("invoice_processing/api/service.py")
    assert 'state.get("decision") == "approve"' in text, (
        "export does not check approval"
    )
    assert "external_id=state.get" in text, (
        "a posted invoice could be posted twice"
    )
    xero = source("invoice_processing/connectors/xero.py")
    assert "client_secret" not in xero, (
        "the client secret must never be handled here"
    )
    return "approval gated, external_id honoured"


# -- adapters ---------------------------------------------------------------
def production_adapters_are_reachable():
    """The defect that motivated this sweep."""
    from invoice_processing.api import service
    from invoice_processing.db.repositories import (
        PgCaseRepository,
        PgDocumentRepository,
    )

    saved = dict(os.environ)
    try:
        service._cases = service._documents = None
        os.environ["DATABASE_URL"] = "postgresql://example/db"
        assert isinstance(service.get_cases(), PgCaseRepository), (
            "case repo not switchable"
        )
        assert isinstance(service.get_documents(), PgDocumentRepository), (
            "doc repo not switchable"
        )
    finally:
        os.environ.clear()
        os.environ.update(saved)
        service._cases = service._documents = None
    return "Pg case + document repositories reachable"


def misconfiguration_is_refused():
    from invoice_processing.api.preflight import ConfigurationError, enforce

    saved = dict(os.environ)
    try:
        for name in (
            "GCS_BUCKET",
            "DATABASE_URL",
            "CLOUD_SQL_INSTANCE",
            "GOOGLE_CLOUD_PROJECT",
        ):
            os.environ.pop(name, None)
        os.environ["ENV"] = "production"
        try:
            enforce()
        except ConfigurationError:
            pass
        else:
            raise AssertionError(
                "a misconfigured production start was permitted"
            )
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return "deployed + unconfigured -> refuses to start"


# -- Part D -----------------------------------------------------------------
def the_suite_passes():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout else ""
    assert proc.returncode == 0, tail
    return tail


def the_eval_gate_blocks_regressions():
    corpus = REPO / "tests/fixtures/eval/agent_output"
    assert corpus.exists(), "the frozen eval corpus is missing; CI would exit 1"
    assert (REPO / "eval/baseline.json").exists(), "no baseline to gate against"
    proc = subprocess.run(
        [
            sys.executable,
            "eval/eval.py",
            "--ground-truth",
            "invoice_processing/exemplary_data",
            "--agent-output",
            "tests/fixtures/eval/agent_output",
            "--skip-llm",
            "--require-baseline",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout.strip()[-300:]
    return "gate passes against the frozen corpus"


def isolation_covers_every_role():
    text = source("tests/test_tenant_isolation_api.py")
    for role in ("viewer", "clerk", "approver", "admin"):
        assert role in text, f"{role} is not covered"
    assert "test_every_route_is_covered" in text, (
        "route coverage is not enforced"
    )
    return "4 roles, route coverage enforced"


def impact_assessment_is_reproducible():
    text = source("invoice_processing/core/impact_assessor.py")
    # Strip comments first: the fix's own explanation names the old call, and a
    # naive substring search would match the prose describing the bug rather
    # than the bug. A check that reports a fixed defect is as misleading as one
    # that misses a live one.
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    if "random.sample(" in code:
        raise AssertionError(
            "random.sample makes the impact report non-reproducible; the "
            "'measured blast radius' claim is not yet true (Part D, deferred)"
        )
    return "deterministic sampling"


def ci_needs_no_credentials():
    workflow = REPO / ".github/workflows/ci.yml"
    assert workflow.exists(), "no CI workflow"
    text = workflow.read_text()
    # The GitHub Actions expression, not the word: an error message that says
    # "rotate the secrets" is not a credential reference.
    assert "${{ secrets." not in text, (
        "CI references a repository secret; it must run credential-free"
    )
    assert "tests/fixtures/eval/agent_output" in text, (
        "the eval step still points at the gitignored agent_output directory"
    )
    assert (REPO / ".git").exists(), (
        "not a git repository, so CI cannot trigger"
    )
    return "credential-free, frozen corpus, repo present"


# ===========================================================================
# LIVE
# ===========================================================================


def _require_cloud_sql():
    if not os.getenv("CLOUD_SQL_INSTANCE") and not os.getenv("DATABASE_URL"):
        raise Skip("needs CLOUD_SQL_INSTANCE or DATABASE_URL")


def live_rls_blocks_cross_tenant_reads():
    _require_cloud_sql()
    from sqlalchemy import text

    from invoice_processing.db.rls import set_tenant_statement
    from invoice_processing.db.session import get_engine

    engine = get_engine()
    with engine.connect() as c:
        if not c.execute(
            text("select to_regclass('public.invoices')")
        ).scalar():
            raise Skip("schema not migrated on this instance")
        bypass = c.execute(
            text(
                "select rolbypassrls from pg_roles where rolname = current_user"
            )
        ).scalar()
        assert bypass is False, (
            "the app role has BYPASSRLS; every policy is decorative"
        )

    a, b = "verify-a", "verify-b"
    ins_t = (
        "insert into tenants (id,slug,name,region,status,settings,created_at,updated_at)"
        " values (:i,:i,:i,'au','active','{}',now(),now()) on conflict (id) do nothing"
    )
    ins_i = (
        "insert into invoices (id,tenant_id,case_id,status,fields,created_at,updated_at)"
        " values (:i,:t,:c,'new','{}',now(),now()) on conflict (id) do nothing"
    )
    with engine.begin() as c:
        for t in (a, b):
            c.execute(text(ins_t), {"i": t})

    def as_tenant(tenant, sql, params=None):
        with engine.begin() as c:
            c.execute(text(set_tenant_statement()), {"tenant_id": tenant})
            r = c.execute(text(sql), params or {})
            return r.fetchall() if r.returns_rows else r.rowcount

    try:
        for t in (a, b):
            as_tenant(t, ins_i, {"i": f"inv-{t}", "t": t, "c": f"case-{t}"})
        seen = [r[0] for r in as_tenant(a, "select case_id from invoices")]
        assert seen == [f"case-{a}"], f"tenant A saw {seen}"
        assert (
            as_tenant(
                a, "select 1 from invoices where id=:x", {"x": f"inv-{b}"}
            )
            == []
        )
        assert (
            as_tenant(
                a,
                "update invoices set status='x' where id=:i",
                {"i": f"inv-{b}"},
            )
            == 0
        )
    finally:
        for t in (a, b):
            as_tenant(t, "delete from invoices where tenant_id=:t", {"t": t})
        with engine.begin() as c:
            c.execute(
                text("delete from tenants where id in ('verify-a','verify-b')")
            )
    return "reads, writes and updates all confined"


def live_audit_is_append_only():
    _require_cloud_sql()
    from sqlalchemy import text

    from invoice_processing.db.session import get_engine

    with get_engine().connect() as c:
        if not c.execute(
            text("select to_regclass('public.audit_events')")
        ).scalar():
            raise Skip("schema not migrated on this instance")
        policies = [
            r[0]
            for r in c.execute(
                text(
                    "select cmd from pg_policies where tablename='audit_events'"
                )
            )
        ]
    assert sorted(policies) == ["INSERT", "SELECT"], (
        f"unexpected policies: {policies}"
    )
    return "no UPDATE or DELETE policy exists"


def live_gcs_isolates_tenants():
    if not os.getenv("GCS_BUCKET"):
        raise Skip("needs GCS_BUCKET")
    from invoice_processing.gcs import GcsBlobStore

    acme = GcsBlobStore(tenant_id="verify-acme")
    other = GcsBlobStore(tenant_id="verify-globex")
    key = "verify/probe.txt"
    try:
        acme.put(key, b"probe", content_type="text/plain")
        assert acme.get(key) == b"probe"
        assert other.exists(key) is False, (
            "another tenant could read the object"
        )
        assert other.list_keys() == [], "another tenant could list it"
    finally:
        try:
            acme.delete(key)
        except Exception as exc:
            # Cleanup only; the check's verdict is already decided above.
            print(f"  (probe object not deleted: {exc})")
    return "round-trip ok, cross-tenant read refused"


# ===========================================================================


OFFLINE = [
    ("1.1", "schema covers the pilot tables", schema_covers_the_pilot_tables),
    (
        "1.1",
        "every tenant table has a forced policy",
        every_tenant_table_has_a_policy,
    ),
    (
        "1.1",
        "audit table is append-only by construction",
        the_audit_table_is_append_only,
    ),
    (
        "1.1",
        "migration carries RLS, dialect-guarded",
        the_migration_carries_rls,
    ),
    (
        "1.2",
        "blob keys cannot escape the tenant",
        blob_keys_cannot_escape_the_tenant,
    ),
    ("1.3", "queue redelivers after a lost ack", the_queue_is_at_least_once),
    ("1.3", "worker does not reprocess a redelivery", the_worker_is_idempotent),
    (
        "1.3",
        "poison messages are parked, not dropped",
        poison_messages_are_dead_lettered,
    ),
    (
        "1.4",
        "tenant comes only from the token",
        the_tenant_comes_only_from_the_token,
    ),
    ("1.4", "no endpoint accepts a tenant", no_endpoint_accepts_a_tenant),
    ("1.4", "dev bypass is allow-listed", the_dev_bypass_is_allow_listed),
    (
        "1.5",
        "ingestion refuses non-PDFs and empties",
        ingestion_refuses_what_it_should,
    ),
    (
        "1.5",
        "storage keys are content-addressed",
        storage_keys_are_content_addressed,
    ),
    ("1.5", "inbound webhook fails closed", the_webhook_fails_closed),
    ("1.6", "confidence drives routing", confidence_routing_is_applied),
    ("1.7", "duplicate detection is real", duplicate_detection_is_real),
    ("2.2", "PO two-way match is real", po_matching_is_real),
    ("1.8", "workbench serves the review UI", the_workbench_serves_a_review_ui),
    (
        "1.9",
        "audit sink follows configuration",
        the_audit_sink_follows_configuration,
    ),
    (
        "1.10",
        "export gated on approval, posts once",
        export_requires_approval_and_posts_once,
    ),
    (
        "wire",
        "production adapters are reachable",
        production_adapters_are_reachable,
    ),
    (
        "wire",
        "misconfigured deploy refuses to start",
        misconfiguration_is_refused,
    ),
    ("D", "test suite passes", the_suite_passes),
    ("D", "eval gate blocks regressions", the_eval_gate_blocks_regressions),
    ("D", "isolation covers every role", isolation_covers_every_role),
    (
        "D",
        "impact assessment is reproducible",
        impact_assessment_is_reproducible,
    ),
    ("D", "CI runs without credentials", ci_needs_no_credentials),
]

LIVE = [
    (
        "1.1",
        "RLS blocks cross-tenant access (live)",
        live_rls_blocks_cross_tenant_reads,
    ),
    ("1.9", "audit trail is append-only (live)", live_audit_is_append_only),
    ("1.2", "GCS isolates tenants (live)", live_gcs_isolates_tenants),
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="verify_phase1")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also run checks against Cloud SQL and GCS.",
    )
    args = parser.parse_args(argv)

    checks = list(OFFLINE) + (list(LIVE) if args.live else [])
    tier = "OFFLINE + LIVE" if args.live else "OFFLINE"
    print(f"\nPhase 1 acceptance sweep  [{tier}]")
    print("=" * 86)

    for item, name, fn in checks:
        check(item, name, fn)

    for item, name, status, detail in results:
        mark = {PASS: "ok  ", FAIL: "FAIL", SKIP: "skip"}[status]
        print(f"  {mark}  {item:<5} {name:<44} {detail[:70]}")

    passed = sum(1 for r in results if r[2] == PASS)
    failed = sum(1 for r in results if r[2] == FAIL)
    skipped = sum(1 for r in results if r[2] == SKIP)

    print("=" * 86)
    print(f"  {passed} passed, {failed} failed, {skipped} skipped")
    if skipped and not args.live:
        print("  (skipped checks need --live and configured infrastructure)")
    if failed:
        print("\n  Phase 1 is NOT complete. Failures above.")
    elif skipped:
        print(
            "\n  Offline tier clean. Skipped checks are unverified, not passed."
        )
    else:
        print("\n  Phase 1 verified.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
