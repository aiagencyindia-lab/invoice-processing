"""Approval chains, groups, rules and delegations live in the database: the
API and the worker are separate containers, and "what is waiting for me" and
"what is overdue" are queries, not scans of every case."""

from sqlalchemy import inspect

from invoice_processing.db import models
from invoice_processing.db.session import create_all


class TestSchema:
    def test_tables_are_tenant_tables(self):
        for name in (
            "approver_groups",
            "approval_rules",
            "delegations",
            "approval_steps",
        ):
            assert name in models.TENANT_TABLES

    def test_create_all_builds_them(self, scratch_db):
        inspector = inspect(create_all(scratch_db("schema.db")))
        for name in (
            "approver_groups",
            "approval_rules",
            "delegations",
            "approval_steps",
        ):
            assert inspector.has_table(name)
        assert "manager_email" in {
            c["name"] for c in inspector.get_columns("users")
        }
        step_cols = {c["name"] for c in inspector.get_columns("approval_steps")}
        assert {
            "case_id",
            "position",
            "assignee_kind",
            "assignee_value",
            "status",
            "due_at",
            "decided_by",
            "acting_for",
            "override_reason",
            "escalated_to",
            "escalated_at",
        } <= step_cols


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------
from datetime import date, datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402

from invoice_processing.api import service  # noqa: E402
from invoice_processing.approvals import Assignee, StepSpec  # noqa: E402
from invoice_processing.db.repositories import (  # noqa: E402
    InvalidDelegation,
    PgApprovalRepository,
    StepNotPending,
    UnknownGroup,
    UnknownRule,
    entity_type_for,
)
from invoice_processing.db.session import (  # noqa: E402
    get_engine,
    tenant_session,
)

T = "tenant-a"
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo(scratch_db):
    url = scratch_db("approvals.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    for tenant_id, slug in ((T, "a"), ("tenant-b", "b")):
        with tenant_session(tenant_id, url) as session:
            session.add(models.Tenant(id=tenant_id, name=slug, slug=slug))
    return PgApprovalRepository(url)


def at(iso):
    # SQLite hands timestamps back without a zone; Postgres keeps +00:00.
    return datetime.fromisoformat(iso).replace(tzinfo=None)


def spec(kind, value, rule=None):
    return StepSpec(Assignee(kind, value), rule)


def seed(repo, case="c1", *specs):
    specs = specs or (
        spec("user", "a@x.com"),
        spec("group", "Finance"),
        spec("role", "admin"),
    )
    return repo.replace_pending(T, case, list(specs), sla_days=2, now=NOW)


class TestGuard:
    def test_no_repository_without_the_table(self):
        assert service.get_approvals() is None

    def test_repository_when_the_table_exists(self, repo, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", repo.url)
        monkeypatch.setattr(service, "_approvals", None)
        assert isinstance(service.get_approvals(), PgApprovalRepository)

    def test_audit_entity_types(self):
        assert entity_type_for("approval_step_decided") == "approval"
        assert entity_type_for("delegation_created") == "delegation"


class TestGroups:
    def test_crud_and_map(self, repo):
        g = repo.create_group(T, "Finance", ["a@x.com", "b@x.com"])
        assert g["name"] == "Finance" and g["members"] == ["a@x.com", "b@x.com"]
        repo.update_group(T, g["id"], members=["c@x.com"])
        assert repo.groups_map(T) == {"Finance": ["c@x.com"]}
        repo.update_group(T, g["id"], name="Ops")
        assert [x["name"] for x in repo.list_groups(T)] == ["Ops"]
        repo.delete_group(T, g["id"])
        assert repo.list_groups(T) == []

    def test_scoped_to_tenant(self, repo):
        g = repo.create_group(T, "Finance", ["a@x.com"])
        assert repo.list_groups("tenant-b") == []
        assert repo.groups_map("tenant-b") == {}
        with pytest.raises(UnknownGroup):
            repo.update_group("tenant-b", g["id"], name="Stolen")
        with pytest.raises(UnknownGroup):
            repo.delete_group("tenant-b", g["id"])
        assert len(repo.list_groups(T)) == 1


class TestRules:
    def test_crud(self, repo):
        steps = [{"kind": "user", "value": "a@x.com"}]
        r = repo.create_rule(
            T,
            name="Big",
            kind="amount",
            match={"min_amount": 1000},
            steps=steps,
        )
        assert r["enabled"] is True and r["steps"] == steps
        out = repo.update_rule(T, r["id"], enabled=False, name="Huge")
        assert out["enabled"] is False and out["name"] == "Huge"
        assert repo.list_rules(T)[0]["match"] == {"min_amount": 1000}
        assert repo.list_rules("tenant-b") == []
        with pytest.raises(UnknownRule):
            repo.update_rule("tenant-b", r["id"], name="x")
        with pytest.raises(UnknownRule):
            repo.delete_rule("tenant-b", r["id"])
        repo.delete_rule(T, r["id"])
        assert repo.list_rules(T) == []


class TestDelegations:
    def make(self, repo):
        return repo.create_delegation(
            T,
            delegator="Boss@x.com",
            delegate="dep@x.com",
            starts_on=date(2026, 10, 10),
            ends_on=date(2026, 10, 12),
            created_by="admin@x.com",
        )

    def test_window_is_inclusive(self, repo):
        d = self.make(repo)
        assert d["starts_on"] == "2026-10-10" and d["ends_on"] == "2026-10-12"
        at = lambda day: repo.active_delegators_of(  # noqa: E731
            T, "DEP@x.com", date(2026, 10, day)
        )
        assert at(9) == []
        assert at(10) == ["boss@x.com"]
        assert at(12) == ["boss@x.com"]
        assert at(13) == []
        assert (
            repo.active_delegators_of(
                "tenant-b", "dep@x.com", date(2026, 10, 11)
            )
            == []
        )

    def test_list_filter_and_delete(self, repo):
        d = self.make(repo)
        assert len(repo.list_delegations(T)) == 1
        assert len(repo.list_delegations(T, email="BOSS@x.com")) == 1
        assert repo.list_delegations(T, email="other@x.com") == []
        assert repo.list_delegations("tenant-b") == []
        repo.delete_delegation("tenant-b", d["id"])
        assert len(repo.list_delegations(T)) == 1
        repo.delete_delegation(T, d["id"])
        assert repo.list_delegations(T) == []

    def test_rejects_bad_ranges(self, repo):
        with pytest.raises(InvalidDelegation):
            repo.create_delegation(
                T,
                delegator="a@x.com",
                delegate="b@x.com",
                starts_on=date(2026, 10, 12),
                ends_on=date(2026, 10, 10),
                created_by="a@x.com",
            )
        with pytest.raises(InvalidDelegation):
            repo.create_delegation(
                T,
                delegator="a@x.com",
                delegate="A@x.com",
                starts_on=date(2026, 10, 10),
                ends_on=date(2026, 10, 10),
                created_by="a@x.com",
            )


class TestReplacePending:
    def test_new_chain_has_positions_and_first_due(self, repo):
        chain = seed(repo)
        assert [s["position"] for s in chain] == [1, 2, 3]
        assert [s["status"] for s in chain] == ["pending"] * 3
        assert at(chain[0]["due_at"]) == (NOW + timedelta(days=2)).replace(
            tzinfo=None
        )
        assert chain[1]["due_at"] is None and chain[2]["due_at"] is None
        assert repo.current(T, "c1")["id"] == chain[0]["id"]

    def test_keeps_decided_and_continues_numbering(self, repo):
        chain = seed(repo)
        repo.decide(
            T,
            chain[0]["id"],
            approve=True,
            decided_by="a@x.com",
            sla_days=2,
            now=NOW,
        )
        out = repo.replace_pending(
            T, "c1", [spec("role", "approver")], sla_days=3, now=NOW
        )
        assert [(s["position"], s["status"]) for s in out] == [
            (1, "approved"),
            (2, "pending"),
        ]
        assert out[1]["assignee_kind"] == "role"
        assert at(out[1]["due_at"]) == (NOW + timedelta(days=3)).replace(
            tzinfo=None
        )

    def test_other_case_and_tenant_untouched(self, repo):
        seed(repo, "c2")
        seed(repo, "c1")
        repo.replace_pending(T, "c1", [], sla_days=2, now=NOW)
        assert repo.chain(T, "c1") == []
        assert len(repo.chain(T, "c2")) == 3
        assert repo.chain("tenant-b", "c2") == []

    def test_append_step(self, repo):
        seed(repo)
        s = repo.append_step(
            T, "c1", spec("user", "z@x.com"), sla_days=2, now=NOW
        )
        assert s["position"] == 4 and s["due_at"] is None
        empty = repo.append_step(
            T, "new", spec("user", "z@x.com"), sla_days=2, now=NOW
        )
        assert empty["position"] == 1
        assert at(empty["due_at"]) == (NOW + timedelta(days=2)).replace(
            tzinfo=None
        )


class TestDecide:
    def test_approve_advances_and_sets_next_due(self, repo):
        chain = seed(repo)
        later = NOW + timedelta(days=1)
        step, done = repo.decide(
            T,
            chain[0]["id"],
            approve=True,
            decided_by="a@x.com",
            acting_for="boss@x.com",
            override_reason="ooo",
            note="ok",
            sla_days=2,
            now=later,
        )
        assert done is False
        assert step["status"] == "approved"
        assert step["decided_by"] == "a@x.com"
        assert step["acting_for"] == "boss@x.com"
        assert step["override_reason"] == "ooo"
        assert step["note"] == "ok"
        nxt = repo.current(T, "c1")
        assert nxt["id"] == chain[1]["id"]
        assert at(nxt["due_at"]) == (later + timedelta(days=2)).replace(
            tzinfo=None
        )

    def test_last_approval_completes(self, repo):
        chain = seed(repo, "c1", spec("role", "approver"))
        _, done = repo.decide(
            T,
            chain[0]["id"],
            approve=True,
            decided_by="a@x.com",
            sla_days=2,
            now=NOW,
        )
        assert done is True and repo.current(T, "c1") is None

    def test_reject_cancels_the_rest(self, repo):
        chain = seed(repo)
        repo.decide(
            T,
            chain[0]["id"],
            approve=True,
            decided_by="a@x.com",
            sla_days=2,
            now=NOW,
        )
        step, done = repo.decide(
            T,
            chain[1]["id"],
            approve=False,
            decided_by="b@x.com",
            note="no",
            sla_days=2,
            now=NOW,
        )
        assert done is True and step["status"] == "rejected"
        assert [s["status"] for s in repo.chain(T, "c1")] == [
            "approved",
            "rejected",
            "cancelled",
        ]

    def test_refuses_non_pending_and_out_of_order(self, repo):
        chain = seed(repo)
        with pytest.raises(StepNotPending):
            repo.decide(
                T,
                chain[1]["id"],
                approve=True,
                decided_by="x@x.com",
                sla_days=2,
                now=NOW,
            )
        repo.decide(
            T,
            chain[0]["id"],
            approve=True,
            decided_by="a@x.com",
            sla_days=2,
            now=NOW,
        )
        with pytest.raises(StepNotPending):
            repo.decide(
                T,
                chain[0]["id"],
                approve=True,
                decided_by="a@x.com",
                sla_days=2,
                now=NOW,
            )

    def test_other_tenant_cannot_decide(self, repo):
        chain = seed(repo)
        with pytest.raises(StepNotPending):
            repo.decide(
                "tenant-b",
                chain[0]["id"],
                approve=True,
                decided_by="x@x.com",
                sla_days=2,
                now=NOW,
            )
        assert repo.current(T, "c1")["status"] == "pending"


class TestMine:
    def ask(self, repo, tenant=T, **kw):
        args = {
            "email": "m@x.com",
            "role": "clerk",
            "groups_map": repo.groups_map(tenant),
            "delegators_of": [],
            "today": date(2026, 10, 8),
        }
        args.update(kw)
        return repo.mine(tenant, **args)

    def test_group_member_sees_current_step_with_why(self, repo):
        repo.create_group(T, "Finance", ["m@x.com"])
        seed(repo, "c1", spec("group", "Finance"))
        out = self.ask(repo)
        assert [s["case_id"] for s in out] == ["c1"]
        assert out[0]["why"] == "member of Finance"

    def test_only_the_current_step_counts(self, repo):
        repo.create_group(T, "Finance", ["m@x.com"])
        seed(repo, "c1", spec("user", "a@x.com"), spec("group", "Finance"))
        assert self.ask(repo) == []

    def test_not_visible_to_others_or_other_tenants(self, repo):
        repo.create_group(T, "Finance", ["m@x.com"])
        seed(repo, "c1", spec("group", "Finance"))
        assert self.ask(repo, email="n@x.com") == []
        assert (
            self.ask(
                repo, tenant="tenant-b", groups_map={"Finance": ["m@x.com"]}
            )
            == []
        )

    def test_delegate_sees_delegators_step(self, repo):
        seed(repo, "c1", spec("user", "boss@x.com"))
        out = self.ask(repo, delegators_of=["boss@x.com"])
        assert out[0]["why"] == "delegate for boss@x.com"


class TestOverdueAndEscalate:
    def test_overdue_is_pending_past_due_unescalated(self, repo):
        seed(repo, "late", spec("user", "a@x.com"))
        seed(repo, "fresh", spec("user", "a@x.com"))
        seed(repo, "done", spec("user", "a@x.com"))
        done = repo.current(T, "done")
        repo.decide(
            T,
            done["id"],
            approve=True,
            decided_by="a@x.com",
            sla_days=2,
            now=NOW,
        )
        # Re-seed "fresh" later so its due date is in the future.
        repo.replace_pending(
            T, "fresh", [spec("user", "a@x.com")], sla_days=9, now=NOW
        )
        check = NOW + timedelta(days=3)
        out = repo.overdue(T, check)
        assert [s["case_id"] for s in out] == ["late"]
        assert repo.overdue("tenant-b", check) == []

    def test_escalate_sets_fields_and_clears_overdue(self, repo):
        step = seed(repo, "late", spec("user", "a@x.com"))[0]
        later = NOW + timedelta(days=3)
        out = repo.escalate(T, step["id"], to="role:admin", now=later)
        assert out["escalated_to"] == "role:admin"
        assert at(out["escalated_at"]) == later.replace(tzinfo=None)
        assert repo.overdue(T, later) == []


class TestTenantsAndManagers:
    def test_tenant_ids_lists_every_tenant(self, repo):
        assert sorted(repo.tenant_ids()) == ["tenant-a", "tenant-b"]

    def test_manager_of(self, repo):
        with tenant_session(T, repo.url) as session:
            session.add(
                models.User(
                    tenant_id=T, email="Dev@x.com", manager_email="boss@x.com"
                )
            )
            session.add(models.User(tenant_id=T, email="top@x.com"))
        assert repo.manager_of(T, "dev@X.com") == "boss@x.com"
        assert repo.manager_of(T, "top@x.com") is None
        assert repo.manager_of(T, "nobody@x.com") is None
        assert repo.manager_of("tenant-b", "dev@x.com") is None
