"""GST/ITC v1 (FR-26): vendor GST standing, shared identities, ITC at risk."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from invoice_processing import tax_ids
from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust import gst_itc
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.engine import EngineContext
from invoice_processing.trust.findings import model as finding_model
from invoice_processing.trust.graph import resolve
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.providers.fakes import FakeProvider
from invoice_processing.trust.providers.registry import Registry

T = "tenant-gst"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="worker")
AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def gstin(first14: str) -> str:
    """A GSTIN with a valid check character."""
    for ch in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if tax_ids.validate_gstin(first14 + ch)[0]:
            return first14 + ch
    raise AssertionError(first14)


G1 = gstin("27ABCDE1234F1Z")
G2 = gstin("29ABCDE1234F1Z")  # same PAN, another state
G3 = gstin("07PQRST5678K1Z")


class Env:
    def __init__(self, url):
        self.url = url
        self.graph = GraphStore(url)
        self.links = LinkStore(url)
        self.script: dict = {}
        self.seq = 0

    def vendor(self, name, gst=None, **extra):
        attrs = {"name": name, "gstin": gst, **extra}
        return self.graph.upsert_node(
            CTX,
            kind="vendor",
            natural_key=f"v:{name}",
            attrs=attrs,
            natural_keys=resolve.natural_keys(attrs),
            at=AT,
        )

    def invoice(self, vendor, amount, day="2025-06-01", tax="18", **extra):
        self.seq += 1
        attrs = {
            "invoice_number": f"INV-{self.seq}",
            "invoice_date": day,
            "amount": str(amount),
            "currency": "INR",
            "tax_amount": tax,
            **extra,
        }
        node = self.graph.upsert_node(
            CTX,
            kind="invoice",
            natural_key=f"i:{self.seq}",
            attrs=attrs,
            at=AT,
        )
        if vendor:
            self.graph.upsert_edge(
                CTX,
                src=node,
                dst=vendor,
                kind="issued_by",
                source_system="t",
                provenance="system_record",
                confidence=1.0,
                observed_at=AT,
                at=AT,
            )
        return node

    def ectx(self, invoice_node=None, thresholds=None):
        registry = Registry(self.graph)
        registry.register("gstin", "fake", lambda: FakeProvider(self.script))
        return EngineContext(
            ctx=CTX,
            mode="advisory",
            document=None if invoice_node is None else {"id": "d"},
            invoice_node=invoice_node,
            graph=self.graph,
            links=self.links,
            findings=None,
            providers=registry,
            settings={"trust_thresholds": thresholds or {}},
            decider=FakeDecider({}, fail=True),
            blobs=object(),
            url=self.url,
        )

    def run(self, invoice_node=None, **kw):
        return gst_itc.run(self.ectx(invoice_node, **kw))


@pytest.fixture
def env(scratch_db):
    url = scratch_db("gst.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Gst", slug="gst", status="active"))
    return Env(url)


def by_type(specs):
    out: dict = {}
    for s in specs:
        out.setdefault(s.type, []).append(s)
    return out


def active(**extra):
    return {"status": "active", "missed_periods": 0, **extra}


# -- provider verdicts ------------------------------------------------------


def test_types_are_registered_with_holds():
    for code in gst_itc.TYPES:
        assert finding_model.TYPE_ENGINE[code] == "gst_itc"
    assert {"GST-INACTIVE", "GST-UNVERIFIED"} <= finding_model.HOLD_TYPES
    for code in ("GST-NONFILER", "GST-SHARED-IDENTITY", "GST-ITC-RISK"):
        assert code not in finding_model.HOLD_TYPES
    assert gst_itc.ENGINE_VERSION == "gst_itc-1"


def test_active_vendor_yields_no_findings(env):
    env.script[G1] = active()
    inv = env.invoice(env.vendor("Acme", G1), 1000)
    assert env.run(inv) == []


def test_cancelled_gstin_is_an_inactive_hold_with_full_itc_at_risk(env):
    env.script[G1] = {"status": "cancelled", "missed_periods": 0}
    inv = env.invoice(env.vendor("Acme", G1), 1000, tax="180.50")
    found = by_type(env.run(inv))
    [hold] = found["GST-INACTIVE"]
    assert hold.transaction_node == inv
    assert hold.dedupe_key == f"GST-INACTIVE:{inv}"
    assert hold.impact is None
    cited = {c.node_id for c in hold.citations}
    assert inv in cited and len(cited) == 2  # the provider evidence too
    [risk] = found["GST-ITC-RISK"]
    assert risk.impact == Decimal("180.50")
    assert risk.dedupe_key.startswith("GST-ITC-RISK:")
    assert inv in {c.node_id for c in risk.citations}


def test_provider_unavailable_fails_closed_as_unverified(env):
    env.script[G1] = RuntimeError("GSP down")
    inv = env.invoice(env.vendor("Acme", G1), 1000)
    [f] = env.run(inv)
    assert f.type == "GST-UNVERIFIED" and f.details["reason"] == "unavailable"


def test_unknown_provider_answer_is_unverified(env):
    inv = env.invoice(env.vendor("Acme", G1), 1000)  # unscripted: "unknown"
    [f] = env.run(inv)
    assert f.type == "GST-UNVERIFIED"


def test_bad_checksum_is_unverified_without_calling_provider(env):
    bad = G1[:14] + ("0" if G1[14] != "0" else "1")
    env.script[bad] = active()
    ven = env.vendor("Acme", bad)
    inv = env.invoice(ven, 1000)
    [f] = env.run(inv)
    assert f.type == "GST-UNVERIFIED" and f.details["reason"] == "checksum"
    assert {c.node_id for c in f.citations} == {inv, ven}
    assert env.graph.nodes(T, kind="provider_evidence") == []


def test_invoice_tax_id_is_used_when_the_vendor_has_none(env):
    env.script[G1] = {"status": "suspended"}
    inv = env.invoice(env.vendor("Acme"), 1000, vendor_tax_id=G1.lower())
    assert "GST-INACTIVE" in by_type(env.run(inv))


def test_non_gstin_tax_id_is_skipped(env):
    inv = env.invoice(env.vendor("Aussie", "51824753556"), 1000)
    assert env.run(inv) == []


def test_nonfiler_at_threshold_is_advisory_with_half_factor(env):
    env.script[G1] = active(missed_periods=2)
    inv = env.invoice(env.vendor("Acme", G1), 1000, tax="100")
    found = by_type(env.run(inv))
    assert set(found) == {"GST-NONFILER", "GST-ITC-RISK"}
    assert found["GST-ITC-RISK"][0].impact == Decimal("50.0")
    env.script[G1] = active(missed_periods=1)
    assert env.run(inv) == []
