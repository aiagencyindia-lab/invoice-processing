"""Vendors live in the database, not a file: the API and the worker are
separate containers in production, so a JSON master written by one was
invisible to the other. Bank details and the dual-control queue that governs
them need a record both can read."""

from sqlalchemy import inspect

from invoice_processing.db import models
from invoice_processing.db.session import create_all


class TestSchema:
    def test_vendor_tables_are_tenant_tables(self):
        assert "vendors" in models.TENANT_TABLES
        assert "vendor_bank_changes" in models.TENANT_TABLES

    def test_create_all_builds_the_tables_and_document_columns(
        self, scratch_db
    ):
        engine = create_all(scratch_db("schema.db"))
        inspector = inspect(engine)
        assert inspector.has_table("vendors")
        assert inspector.has_table("vendor_bank_changes")
        document_columns = {
            c["name"] for c in inspector.get_columns("documents")
        }
        assert {"sender", "reply_to"} <= document_columns
        vendor_columns = {c["name"] for c in inspector.get_columns("vendors")}
        assert {
            "name",
            "tax_id",
            "kind",
            "normalized_name",
            "email_domain",
            "bank_account_name",
            "bank_account_number",
            "bank_code",
            "bank_iban",
            "bank_verified_at",
            "bank_verified_by",
        } <= vendor_columns
