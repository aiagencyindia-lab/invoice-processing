"""The Wave 0 tables: time-travel uniqueness and the citation rule."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fresh_schema import fresh_schema
from sqlalchemy.exc import IntegrityError

from invoice_processing.db import models
from invoice_processing.db.models import TENANT_TABLES
from invoice_processing.db.session import tenant_session

T = "tenant-acme"
NOW = datetime(2026, 10, 14, tzinfo=UTC)


@pytest.fixture
def url(scratch_db):
    url = scratch_db("trust_schema.db")
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    return url


def test_the_eight_tables_are_tenant_tables():
    for name in (
        "graph_nodes",
        "graph_edges",
        "entity_links",
        "trust_history_imports",
        "history_records",
        "trust_findings",
        "trust_finding_citations",
        "trust_finding_events",
    ):
        assert name in TENANT_TABLES, name


def test_open_node_natural_key_is_unique_but_closed_rows_may_repeat(url):
    with tenant_session(T, url) as s:
        s.add(
            models.GraphNode(
                tenant_id=T, kind="vendor", natural_key="v1", valid_from=NOW
            )
        )
    with pytest.raises(IntegrityError), tenant_session(T, url) as s:
        s.add(
            models.GraphNode(
                tenant_id=T, kind="vendor", natural_key="v1", valid_from=NOW
            )
        )
    with tenant_session(T, url) as s:
        first = s.query(models.GraphNode).one()
        first.valid_to = NOW
    # A second open row is fine once the first is closed.
    with tenant_session(T, url) as s:
        s.add(
            models.GraphNode(
                tenant_id=T, kind="vendor", natural_key="v1", valid_from=NOW
            )
        )
    with tenant_session(T, url) as s:
        assert s.query(models.GraphNode).count() == 2


def test_dedupe_key_is_unique_per_tenant(url):
    with tenant_session(T, url) as s:
        node = models.GraphNode(
            tenant_id=T, kind="invoice", natural_key="i1", valid_from=NOW
        )
        s.add(node)
        s.flush()
        s.add(
            models.TrustFinding(
                tenant_id=T,
                type="BENCH-IMPORT",
                transaction_node=node.id,
                dedupe_key="k",
            )
        )
    with pytest.raises(IntegrityError), tenant_session(T, url) as s:
        node_id = s.query(models.GraphNode).one().id
        s.add(
            models.TrustFinding(
                tenant_id=T,
                type="BENCH-IMPORT",
                transaction_node=node_id,
                dedupe_key="k",
            )
        )


def test_the_wave2_tables_are_tenant_tables():
    for name in (
        "trust_audits",
        "trust_audit_runs",
        "trust_audit_samples",
        "trust_explanation_ratings",
    ):
        assert name in TENANT_TABLES, name


def test_one_rating_per_rater_per_finding(url):
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    with tenant_session(T, url) as s:
        node = models.GraphNode(
            tenant_id=T, kind="invoice", natural_key="invoices:i1"
        )
        s.add(node)
        s.flush()
        f = models.TrustFinding(
            tenant_id=T,
            type="T",
            transaction_node=node.id,
            dedupe_key="T:i1",
        )
        s.add(f)
        s.flush()
        s.add(
            models.TrustExplanationRating(
                tenant_id=T,
                finding_id=f.id,
                rater="a@x.test",
                correct=True,
                useful=True,
            )
        )
        s.flush()
        fid = f.id
    with pytest.raises(IntegrityError), tenant_session(T, url) as s:
        s.add(
            models.TrustExplanationRating(
                tenant_id=T,
                finding_id=fid,
                rater="a@x.test",
                correct=False,
                useful=True,
            )
        )


WAVE3_TABLES = (
    "trust_holds",
    "trust_evidence_requests",
    "api_keys",
    "webhook_endpoints",
    "webhook_outbox",
)


def test_the_wave3_tables_are_tenant_tables(url):
    from sqlalchemy import inspect  # noqa: PLC0415

    from invoice_processing.db.session import get_engine  # noqa: PLC0415

    names = set(inspect(get_engine(url)).get_table_names())
    for name in WAVE3_TABLES:
        assert name in TENANT_TABLES, name
        assert name in names, name


def test_an_api_key_prefix_is_unique_per_tenant(url):
    def key():
        return models.ApiKey(
            tenant_id=T, name="ci", key_prefix="abcd1234", key_hash="0" * 64
        )

    with tenant_session(T, url) as s:
        s.add(key())
    with pytest.raises(IntegrityError), tenant_session(T, url) as s:
        s.add(key())


WAVE4_TABLES = ("agents", "agent_mandates", "agent_actions")


def test_the_wave4_tables_are_tenant_tables(url):
    from sqlalchemy import inspect  # noqa: PLC0415

    from invoice_processing.db.session import get_engine  # noqa: PLC0415

    names = set(inspect(get_engine(url)).get_table_names())
    for name in WAVE4_TABLES:
        assert name in TENANT_TABLES, name
        assert name in names, name


def _agent(principal_id=None, name="bot"):
    return models.Agent(
        tenant_id=T,
        name=name,
        owner="ops@acme.test",
        principal_id=principal_id,
        kind="api_key" if principal_id else "external",
    )


def test_an_agent_principal_is_unique_per_tenant(url):
    with tenant_session(T, url) as s:
        s.add(_agent("api-key:1"))
    with pytest.raises(IntegrityError), tenant_session(T, url) as s:
        s.add(_agent("api-key:1", name="other"))


def test_external_agents_need_no_principal(url):
    with tenant_session(T, url) as s:
        s.add(_agent(name="erp-1"))
        s.add(_agent(name="erp-2"))


def test_a_mandate_version_is_unique_per_agent(url):
    with tenant_session(T, url) as s:
        agent = _agent("api-key:2")
        s.add(agent)
        s.flush()
        agent_id = agent.id

    def mandate():
        return models.AgentMandate(
            tenant_id=T,
            agent_id=agent_id,
            version=1,
            scope={"actions": ["invoice.approve"]},
            valid_from=NOW,
            valid_to=NOW,
            proposed_by="admin@acme.test",
        )

    with tenant_session(T, url) as s:
        s.add(mandate())
    with pytest.raises(IntegrityError), tenant_session(T, url) as s:
        s.add(mandate())
