"""External answers are evidence; a provider outage is an evidence record too, never a pass."""

import pytest

from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.providers import fakes
from invoice_processing.trust.providers.registry import Registry

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="engine")


class Store:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, tenant_id):
        return self.data.get(tenant_id)


@pytest.fixture
def registry(scratch_db):
    url = scratch_db("providers.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    reg = Registry(GraphStore(url))
    fakes.install(reg)
    return reg


def test_fake_response_is_stored_as_hashed_evidence_node(registry):
    registry.register(
        "bank_verification",
        "fake",
        lambda: fakes.FakeProvider(
            {"123": {"name_match": True, "name": "ACME STEEL"}}
        ),
    )
    out = registry.call(
        CTX,
        "bank_verification",
        "123",
        "HDFC0001",
        "Acme Steel",
        settings_store=Store(),
    )
    assert out.status == "ok" and out.data["name_match"] is True
    assert out.provider == "fake"
    node = registry.graph.node(T, out.evidence_node)
    assert node["kind"] == "provider_evidence"
    assert node["attrs"]["raw_sha256"] == out.raw_sha256
    assert node["natural_key"] == f"bank_verification:{out.raw_sha256}"


def test_provider_error_becomes_unavailable_evidence_not_pass(registry):
    registry.register(
        "gstin",
        "fake",
        lambda: fakes.FakeProvider({"27X": RuntimeError("timeout")}),
    )
    out = registry.call(CTX, "gstin", "27X", settings_store=Store())
    assert out.status == "unavailable" and out.data == {"error": "timeout"}
    node = registry.graph.node(T, out.evidence_node)
    assert node["attrs"]["status"] == "unavailable"


def test_registry_picks_fake_by_default_and_real_by_setting(registry):
    registry.register(
        "vat", "vies", lambda: fakes.FakeProvider({"DE1": {"valid": True}})
    )
    default = registry.call(CTX, "vat", "DE1", settings_store=Store())
    assert default.provider == "fake" and default.data == {"result": "unknown"}
    real = registry.call(
        CTX,
        "vat",
        "DE1",
        settings_store=Store({T: {"trust_providers": {"vat": "vies"}}}),
    )
    assert real.provider == "vies" and real.data == {"valid": True}


def test_unknown_capability_or_provider_is_refused(registry):
    with pytest.raises(ValueError, match="capability"):
        registry.call(CTX, "telepathy", "x", settings_store=Store())
    with pytest.raises(ValueError, match="No provider"):
        registry.call(
            CTX,
            "vat",
            "DE1",
            settings_store=Store({T: {"trust_providers": {"vat": "nope"}}}),
        )


def test_evidence_attrs_are_masked(registry):
    registry.register(
        "bank_verification",
        "fake",
        lambda: fakes.FakeProvider(
            {"999": {"bank_account_number": "123456789012"}}
        ),
    )
    out = registry.call(
        CTX, "bank_verification", "999", "C", "N", settings_store=Store()
    )
    node = registry.graph.node(T, out.evidence_node)
    assert node["attrs"]["data"]["bank_account_number"] == "…9012"
