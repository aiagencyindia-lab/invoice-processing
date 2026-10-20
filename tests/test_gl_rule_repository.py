"""GL coding rules live in the database: the API edits them and the worker
reads them, and they are separate containers."""

import pytest
from fresh_schema import fresh_schema
from sqlalchemy import inspect

from invoice_processing.db import models
from invoice_processing.db.repositories import PgGlRuleRepository
from invoice_processing.db.session import create_all, tenant_session

T = "tenant-a"


@pytest.fixture
def repo(scratch_db):
    url = scratch_db("gl_rules.db")
    fresh_schema(url)
    for tenant_id, slug in ((T, "a"), ("tenant-b", "b")):
        with tenant_session(tenant_id, url) as session:
            session.add(models.Tenant(id=tenant_id, name=slug, slug=slug))
    return PgGlRuleRepository(url)


def make(repo, tenant=T, **fields):
    fields = {
        "vendor_name": "Acme",
        "keyword": None,
        "gl_account": "6100",
        "created_by": "admin@x.com",
        **fields,
    }
    return repo.create(tenant, **fields)


def test_is_a_tenant_table_create_all_builds(scratch_db):
    assert "gl_rules" in models.TENANT_TABLES
    assert inspect(create_all(scratch_db("schema.db"))).has_table("gl_rules")


def test_create_returns_the_contract_shape(repo):
    r = make(repo, keyword="freight")
    assert set(r) == {
        "id",
        "vendor_name",
        "keyword",
        "gl_account",
        "enabled",
        "created_by",
        "created_at",
    }
    assert r["vendor_name"] == "Acme" and r["keyword"] == "freight"
    assert r["gl_account"] == "6100" and r["enabled"] is True
    assert r["created_by"] == "admin@x.com"
    assert isinstance(r["created_at"], str)
    # SQLite hands timestamps back without a zone; compare the rest.
    fetched = repo.get(T, r["id"])
    assert fetched.pop("created_at") and r.pop("created_at")
    assert fetched == r


def test_list_is_ordered_by_creation(repo):
    ids = [make(repo, gl_account=str(n))["id"] for n in range(3)]
    assert [r["id"] for r in repo.list_rules(T)] == ids


def test_update_and_delete(repo):
    r = make(repo)
    out = repo.update(T, r["id"], enabled=False, gl_account="7000")
    assert out["enabled"] is False and out["gl_account"] == "7000"
    assert repo.update(T, r["id"], keyword="tax")["keyword"] == "tax"
    assert repo.update(T, "nope", gl_account="1") is None
    assert repo.delete(T, r["id"]) is True
    assert repo.delete(T, r["id"]) is False
    assert repo.get(T, r["id"]) is None and repo.list_rules(T) == []


def test_update_rejects_unknown_fields(repo):
    r = make(repo)
    with pytest.raises(TypeError):
        repo.update(T, r["id"], tenant_id="tenant-b")


def test_scoped_to_tenant(repo):
    r = make(repo)
    assert repo.list_rules("tenant-b") == []
    assert repo.get("tenant-b", r["id"]) is None
    assert repo.update("tenant-b", r["id"], gl_account="9999") is None
    assert repo.delete("tenant-b", r["id"]) is False
    assert repo.get(T, r["id"])["gl_account"] == "6100"
