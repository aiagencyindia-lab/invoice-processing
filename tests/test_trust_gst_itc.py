"""GST/ITC v1 (FR-26): vendor GST standing, shared identities, ITC at risk."""

import logging
import random
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fresh_schema import fresh_schema

from benchmark.history.faults import gst_itc as family
from invoice_processing import tax_ids
from invoice_processing.db import models
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust import evaluate as ev
from invoice_processing.trust import gst_itc
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.engine import EngineContext
from invoice_processing.trust.findings import model as finding_model
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph import resolve
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.graph.sync import sync_tenant
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
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Gst", slug="gst", status="active"))
    return Env(url)


def by_type(specs):
    out: dict = {}
    for s in specs:
        out.setdefault(s.type, []).append(s)
    return out


def nkey(env, node_id):
    return env.graph.node(T, node_id)["natural_key"]


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
    assert hold.dedupe_key == f"GST-INACTIVE:{nkey(env, inv)}"
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


# -- history signals --------------------------------------------------------


def history(env, n=5, amount=1000):
    """An established vendor: invoices through 2024 set the tenant median."""
    env.script[G3] = active()
    old = env.vendor("Old", G3)
    for m in range(1, n + 1):
        env.invoice(old, amount, day=f"2024-{m:02d}-10")
    return old


def test_new_vendor_large_first_invoice(env):
    history(env)
    env.script[G1] = active()
    inv = env.invoice(env.vendor("New", G1), 10000, tax="1800")
    found = by_type(env.run(inv))
    assert set(found) == {"GST-NEW-LARGE-FIRST", "GST-ITC-RISK"}
    [f] = found["GST-NEW-LARGE-FIRST"]
    assert f.details["median"] == "1000"
    assert found["GST-ITC-RISK"][0].impact == Decimal("360.0")


def test_new_vendor_normal_invoice_is_quiet(env):
    old = history(env)
    env.script[G1] = active()
    assert env.run(env.invoice(env.vendor("New", G1), 2000)) == []
    # Large, but the vendor was first seen long before.
    assert env.run(env.invoice(old, 10000, day="2025-06-01")) == []


def test_shared_pan_across_gstins_is_flagged_once_per_invoice_and_pair(env):
    env.script[G1] = env.script[G2] = active()
    a, b = env.vendor("Alpha", G1), env.vendor("Beta", G2)
    inv_a, inv_b = env.invoice(a, 1000, tax="100"), env.invoice(b, 1000)
    pair = ":".join(sorted((G1, G2)))  # GSTINs survive a node re-version
    found = by_type(env.run(inv_a))
    [f] = found["GST-SHARED-IDENTITY"]
    assert f.dedupe_key == f"GST-SHARED-IDENTITY:{nkey(env, inv_a)}:{pair}"
    assert {c.node_id for c in f.citations} == {inv_a, a, b}
    assert f.details["shared"] == ["pan"]
    assert found["GST-ITC-RISK"][0].impact == Decimal("30.0")
    [g] = by_type(env.run(inv_b))["GST-SHARED-IDENTITY"]
    assert g.dedupe_key == f"GST-SHARED-IDENTITY:{nkey(env, inv_b)}:{pair}"


def test_shared_bank_account_or_domain_counts_but_name_does_not(env):
    env.script[G1] = env.script[G3] = active()
    a = env.vendor("Alpha", G1, email_domain="alpha.in")
    b = env.vendor("Gamma", G3, email_domain="alpha.in")
    env.vendor("Alpha Ltd", gstin("19ZZZZZ9999Z1Z"))  # same name only
    found = by_type(env.run(env.invoice(a, 1000)))
    [f] = found["GST-SHARED-IDENTITY"]
    assert f.details["shared"] == ["email_domain"]
    assert b in {c.node_id for c in f.citations}


def test_accepted_link_suppresses_shared_identity(env):
    env.script[G1] = env.script[G2] = active()
    a, b = env.vendor("Alpha", G1), env.vendor("Beta", G2)
    env.links.propose_or_link(CTX, a, b, 0.99, "human")
    assert env.run(env.invoice(a, 1000)) == []


def test_itc_risk_uses_max_factor(env):
    env.script[G1] = {"status": "cancelled", "missed_periods": 4}
    env.script[G2] = active()
    a = env.vendor("Alpha", G1)
    env.vendor("Beta", G2)
    found = by_type(env.run(env.invoice(a, 1000, tax="200")))
    assert {"GST-INACTIVE", "GST-NONFILER", "GST-SHARED-IDENTITY"} <= set(found)
    [risk] = found["GST-ITC-RISK"]
    assert risk.impact == Decimal("200.0")
    assert risk.details["signals"] == [
        "risk_inactive",
        "risk_nonfiler",
        "risk_shared",
    ]


def test_unknown_tax_emits_no_itc_risk_and_logs_without_ids(env, caplog):
    env.script[G1] = {"status": "cancelled"}
    inv = env.invoice(env.vendor("Acme", G1), 1000, tax=None)
    with caplog.at_level(logging.INFO, logger=gst_itc.__name__):
        found = by_type(env.run(inv))
    assert set(found) == {"GST-INACTIVE"}
    [rec] = [r for r in caplog.records if r.msg == "gst_itc_tax_amount_unknown"]
    assert rec.invoices == 1
    assert inv not in str(rec.__dict__)


def test_batch_run_covers_every_invoice(env):
    env.script[G1] = {"status": "cancelled"}
    ven = env.vendor("Acme", G1)
    nodes = {env.invoice(ven, 1000), env.invoice(ven, 2000)}
    specs = env.run()
    assert {
        s.transaction_node for s in specs if s.type == "GST-INACTIVE"
    } == nodes
    assert len(env.graph.nodes(T, kind="provider_evidence")) == 1  # cached


def test_threshold_override_applies(env):
    env.script[G1] = {"status": "cancelled", "missed_periods": 3}
    inv = env.invoice(env.vendor("Acme", G1), 1000, tax="100")
    found = by_type(
        env.run(
            inv,
            thresholds={
                "gst.nonfiler_missed_periods": 5,
                "gst.risk_inactive": 0.9,
            },
        )
    )
    assert "GST-NONFILER" not in found
    assert found["GST-ITC-RISK"][0].impact == Decimal("90.0")


def test_contract_document_is_skipped(env):
    ectx = env.ectx()
    ectx.document = {"id": "c", "kind": "contract"}
    assert gst_itc.run(ectx) == []


# -- review fixes: malformed and non-positive inputs, shared-identity scale --


def gap_records(caplog):
    return [r for r in caplog.records if r.msg == "gst_itc_tax_amount_unknown"]


def test_credit_note_yields_no_itc_risk_and_no_new_large(env, caplog):
    history(env)
    env.script[G1] = {"status": "cancelled"}
    ven = env.vendor("New", G1)
    credit = env.invoice(ven, -10000, tax="-1800")
    zero = env.invoice(ven, 1000, tax="0")
    with caplog.at_level(logging.INFO, logger=gst_itc.__name__):
        assert set(by_type(env.run(credit))) == {"GST-INACTIVE"}
        assert set(by_type(env.run(zero))) == {"GST-INACTIVE"}
    assert gap_records(caplog) == []  # known, just not positive


def test_median_ignores_credit_notes(env):
    old = history(env)
    for m in range(1, 7):
        env.invoice(old, -1000, day=f"2024-{m:02d}-20")
    env.script[G1] = active()
    assert env.run(env.invoice(env.vendor("New", G1), 2000)) == []


def test_unparseable_tax_is_a_gap_and_does_not_abort_the_batch(env, caplog):
    env.script[G1] = {"status": "cancelled"}
    ven = env.vendor("Acme", G1)
    bad = env.invoice(ven, 1000, tax="N/A")
    good = env.invoice(ven, 1000, tax="10")
    with caplog.at_level(logging.INFO, logger=gst_itc.__name__):
        specs = env.run()
    risks = {s.transaction_node for s in specs if s.type == "GST-ITC-RISK"}
    assert risks == {good}
    assert bad in {
        s.transaction_node for s in specs if s.type == "GST-INACTIVE"
    }
    [rec] = gap_records(caplog)
    assert rec.invoices == 1


def test_unparseable_missed_periods_is_unverified(env):
    env.script[G1] = active(missed_periods="n/a")
    [f] = env.run(env.invoice(env.vendor("Acme", G1), 1000))
    assert f.type == "GST-UNVERIFIED" and f.details["reason"] == "unknown"


def test_public_mail_domain_is_not_a_shared_identity(env, monkeypatch):
    def quadratic(nodes):
        raise AssertionError("candidates() over every vendor")

    monkeypatch.setattr(resolve, "candidates", quadratic)
    gs = [G1, G3, gstin("19ZZZZZ9999Z1Z")]
    vendors = []
    for i, g in enumerate(gs):
        env.script[g] = active()
        vendors.append(env.vendor(f"V{i}", g, email_domain="Gmail.com"))
    assert env.run(env.invoice(vendors[0], 1000)) == []


def test_shared_identity_key_is_stable_across_a_vendor_reversion(env):
    env.script[G1] = env.script[G2] = active()
    a = env.vendor("Alpha", G1)
    env.vendor("Beta", G2)
    inv = env.invoice(a, 1000)
    [before] = by_type(env.run(inv))["GST-SHARED-IDENTITY"]
    b2 = env.vendor("Beta", G2, email_domain="beta.in")  # new node id
    [after] = by_type(env.run(inv))["GST-SHARED-IDENTITY"]
    assert after.dedupe_key == before.dedupe_key
    assert b2 in {c.node_id for c in after.citations}


def test_dedupe_keys_survive_an_invoice_reversion(env):
    """A re-versioned invoice node gets a new id; its findings must not
    double up."""
    history(env)
    env.script[G1] = {"status": "cancelled", "missed_periods": 3}
    env.script[G2] = active()
    a = env.vendor("Alpha", G1)
    env.vendor("Beta", G2)
    inv = env.invoice(a, 10000, tax="100")
    store = FindingStore(env.url)
    first = env.run(inv)
    for spec in first:
        store.upsert(CTX, spec)
    old = env.graph.node(T, inv)
    new = env.graph.upsert_node(
        CTX,
        kind="invoice",
        natural_key=old["natural_key"],
        attrs={**old["attrs"], "due_date": "2025-07-01"},
        at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    assert new != inv
    env.graph.upsert_edge(
        CTX,
        src=new,
        dst=a,
        kind="issued_by",
        source_system="t",
        provenance="system_record",
        confidence=1.0,
        observed_at=AT,
    )
    second = env.run(new)
    assert {s.dedupe_key for s in second} == {s.dedupe_key for s in first}
    for spec in second:
        store.upsert(CTX, spec)
    types = [f["type"] for f in store.list(T) if f["type"].startswith("GST-")]
    assert sorted(types) == sorted(set(types))
    assert set(types) == {
        "GST-INACTIVE",
        "GST-NONFILER",
        "GST-NEW-LARGE-FIRST",
        "GST-SHARED-IDENTITY",
        "GST-ITC-RISK",
    }


# -- through evaluate -------------------------------------------------------


class Store:
    def __init__(self, settings):
        self.settings = settings

    def get(self, tenant_id):
        return self.settings


def test_evaluate_enforcing_holds_and_clean_rerun_releases(env):
    with tenant_session(T, env.url) as s:
        s.add(
            models.Vendor(
                tenant_id=T, name="Acme", normalized_name="acme", tax_id=G1
            )
        )
        s.add(
            models.Document(
                tenant_id=T, id="doc-1", sha256="a" * 64, storage_key="k"
            )
        )
        s.flush()
        s.add(
            models.Invoice(
                tenant_id=T,
                id="inv-1",
                document_id="doc-1",
                case_id="case-1",
                invoice_number="INV-1",
                vendor_name="Acme",
                vendor_tax_id=G1,
                total_inc_tax=Decimal("1180"),
                tax_amount=Decimal("180"),
                status="done",
            )
        )
    sync_tenant(
        CTX,
        env.graph,
        env.links,
        url=env.url,
        decider=FakeDecider({}, fail=True),
    )
    env.script[G1] = {"status": "cancelled"}

    def evaluate():
        return ev.evaluate(
            CTX,
            "doc-1",
            graph=env.graph,
            links=env.links,
            url=env.url,
            settings_store=Store(
                {"trust_engine_modes": {"gst_itc": "enforcing"}}
            ),
            providers=env.ectx().providers,
            decider=FakeDecider({}, fail=True),
            blobs=object(),
        )

    out = evaluate()
    assert out["engines"]["gst_itc"]["findings"] == 2
    store = FindingStore(env.url)
    [hold] = store.list(T, type="GST-INACTIVE")
    assert hold["details"]["hold"] is True and hold["status"] == "detected"
    [risk] = store.list(T, type="GST-ITC-RISK")
    assert Decimal(risk["impact"]) == Decimal("180")
    env.script[G1] = active()
    evaluate()
    [hold] = store.list(T, type="GST-INACTIVE")
    assert hold["status"] == "rejected"


# -- benchmark family -------------------------------------------------------


def test_fault_family_plants_valid_gstins_and_priced_faults():
    rows = {"vendor": [], "ap_invoice": []}
    faults: list = []
    family.inject(
        random.Random(1),  # noqa: S311 - synthetic data
        rows,
        faults,
        start=date(2025, 1, 1),
        months=18,
    )
    gstins = [v["gstin"] for v in rows["vendor"]]
    assert all(tax_ids.validate_gstin(g)[0] for g in gstins)
    twins = [v for v in rows["vendor"] if v["pan"]]
    assert len({v["gstin"][2:12] for v in twins}) == 1 and len(twins) == 2
    priced = [f for f in faults if f["type"] == "GST-ITC-RISK"]
    assert priced and all(Decimal(f["expected_impact"]) > 0 for f in priced)
