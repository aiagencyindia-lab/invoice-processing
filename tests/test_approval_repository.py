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
