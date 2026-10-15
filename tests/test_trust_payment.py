"""Payment Trust (FR-21) through evaluate(), and the AC-21 replay harness."""

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_processing.adapters import LocalBlobStore
from invoice_processing.api import service
from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust import evaluate as ev
from invoice_processing.trust import gate, payment
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.findings import model as finding_model
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.graph.sync import sync_tenant
from invoice_processing.trust.providers import fakes
from invoice_processing.trust.providers.registry import Registry

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="worker")
NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
ACCOUNT = "50100234567891"
FIXTURES = Path(__file__).parent / "fixtures" / "trust_payment"
NO_MODEL = FakeDecider({}, fail=True)


class Store:
    def __init__(self, settings):
        self.settings = settings

    def get(self, tenant_id):
        return self.settings


def _history(n=6, amount=100000, last="2026-09-10"):
    end = datetime.fromisoformat(last)
    return [
        {
            "amount": str(amount + (i % 3 - 1) * 2000),
            "invoice_date": (end - timedelta(days=30 * i)).date().isoformat(),
        }
        for i in range(n)
    ]


VENDOR = {
    "name": "Acme Steel",
    "tax_id": "27AAACA1234A1Z5",
    "email_domain": "acme-steel.in",
    "bank_account_name": "Acme Steel Pvt Ltd",
    "bank_account_number": ACCOUNT,
    "bank_code": "HDFC0001234",
    "bank_verified_at": None,
    "history": _history(),
}
INVOICE = {
    "invoice_number": "INV-7",
    "invoice_date": "2026-10-10",
    "total_inc_tax": "101000",
    "currency": "INR",
    "source": "upload",
    "sender": None,
    "reply_to": None,
}


@pytest.fixture
def world(scratch_db, tmp_path, monkeypatch):
    url = scratch_db("payment.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    monkeypatch.setattr(ev, "RUNNERS", {})
    return url, tmp_path / "blobs"


def build(world, vendor=None, invoice=None, eml=None):
    url, blobs_dir = world
    vendor = {**VENDOR, **(vendor or {})}
    invoice = {**INVOICE, **(invoice or {})}
    source_ref = None
    if eml is not None:
        source_ref = "email/msg.eml"
        LocalBlobStore(blobs_dir).put(source_ref, eml)
    verified = vendor.get("bank_verified_at")
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
        s.flush()
        s.add(
            models.Vendor(
                tenant_id=T,
                id="ven-1",
                name=vendor["name"],
                normalized_name=vendor["name"].lower(),
                tax_id=vendor["tax_id"],
                email_domain=vendor["email_domain"],
                bank_account_name=vendor["bank_account_name"],
                bank_account_number=vendor["bank_account_number"],
                bank_code=vendor["bank_code"],
                bank_verified_at=datetime.fromisoformat(verified)
                if verified
                else None,
            )
        )
        s.add(
            models.Document(
                tenant_id=T,
                id="doc-1",
                sha256="a" * 64,
                storage_key="documents/a.pdf",
                source=invoice["source"],
                source_ref=source_ref,
                sender=invoice["sender"],
                reply_to=invoice["reply_to"],
            )
        )
        s.flush()
        for i, h in enumerate(vendor.get("history") or []):
            s.add(
                models.Invoice(
                    tenant_id=T,
                    id=f"h-{i}",
                    case_id=f"case-h{i}",
                    invoice_number=f"OLD-{i}",
                    invoice_date=h["invoice_date"],
                    vendor_name=vendor["name"],
                    vendor_tax_id=vendor["tax_id"],
                    total_inc_tax=Decimal(h["amount"]),
                    currency="INR",
                    status="done",
                )
            )
        s.add(
            models.Invoice(
                tenant_id=T,
                id="inv-1",
                document_id="doc-1",
                case_id="case-1",
                invoice_number=invoice["invoice_number"],
                invoice_date=invoice["invoice_date"],
                vendor_name=vendor["name"],
                vendor_tax_id=vendor["tax_id"],
                total_inc_tax=Decimal(invoice["total_inc_tax"]),
                currency=invoice["currency"],
                status="done",
            )
        )
        change = vendor.get("bank_change")
        if change:
            when = NOW - timedelta(days=change["days_ago"])
            s.add(
                models.VendorBankChange(
                    tenant_id=T,
                    id="chg-1",
                    vendor_id="ven-1",
                    proposed_account_number=ACCOUNT,
                    reason="vendor letter",
                    status=change.get("status", "approved"),
                    proposed_by="a@x.test",
                    decided_by="b@x.test",
                    created_at=when,
                    decided_at=when,
                )
            )
    graph, links = GraphStore(url), LinkStore(url)
    sync_tenant(CTX, graph, links, url=url, decider=NO_MODEL)
    return graph, links


def evaluate(world, graph, links, *, script=None, mode="advisory", doc="doc-1"):
    url, blobs_dir = world
    registry = Registry(graph)
    fakes.install(registry)
    provider = fakes.FakeProvider(script)
    registry.register("bank_verification", "fake", lambda: provider)
    out = ev.evaluate(
        CTX,
        doc,
        graph=graph,
        links=links,
        url=url,
        settings_store=Store({"trust_engine_modes": {"payment": mode}}),
        providers=registry,
        decider=NO_MODEL,
        blobs=LocalBlobStore(blobs_dir),
        now=NOW,
    )
    assert out["engines"]["payment"] not in ("failed", "missing"), out
    return FindingStore(url).list(T), provider


def by_type(findings):
    return {f["type"]: f for f in findings}


def test_hold_types_registered():
    assert {"PAY-UNVERIFIED", "PAY-NAME-MISMATCH", "PAY-BEC"} <= (
        finding_model.HOLD_TYPES
    )
    assert "PAY-ANOMALY" not in finding_model.HOLD_TYPES
    assert "PAY-BEC-SIGNAL" not in finding_model.HOLD_TYPES
    for code in ("PAY-UNVERIFIED", "PAY-ANOMALY", "PAY-BEC-SIGNAL"):
        assert finding_model.TYPE_ENGINE[code] == "payment"
    assert payment.ENGINE_VERSION == "payment-1"


def test_unverified_vendor_is_held_citing_evidence(world):
    graph, links = build(world)
    findings, provider = evaluate(world, graph, links)
    f = by_type(findings)["PAY-UNVERIFIED"]
    assert f["details"]["hold"] is True and f["impact"] == "101000.0000"
    url, _ = world
    cited = FindingStore(url).get(T, f["id"])["citations"]
    kinds = {graph.node(T, c["node_id"])["kind"] for c in cited}
    assert {"provider_evidence", "invoice", "vendor"} <= kinds
    assert provider.calls == [(ACCOUNT, "HDFC0001234", "Acme Steel Pvt Ltd")]
    assert f["details"]["account"] == "…7891"


def test_verified_master_makes_no_provider_call(world):
    graph, links = build(
        world, vendor={"bank_verified_at": "2026-01-01T00:00:00"}
    )
    findings, provider = evaluate(world, graph, links)
    assert provider.calls == []
    assert findings == []


def test_provider_verified_clears(world):
    graph, links = build(world)
    findings, _ = evaluate(
        world, graph, links, script={ACCOUNT: {"result": "verified"}}
    )
    assert findings == []


def test_provider_unavailable_fails_closed(world):
    graph, links = build(world)
    findings, _ = evaluate(
        world, graph, links, script={ACCOUNT: RuntimeError("verifier down")}
    )
    f = by_type(findings)["PAY-UNVERIFIED"]
    assert f["details"]["provider_status"] == "unavailable"


@pytest.mark.parametrize(
    "answer",
    [
        {"result": "verified", "name_match": 0.4},
        {"result": "verified", "account_name": "Zeta Garments Traders"},
    ],
)
def test_name_mismatch_holds(world, answer):
    graph, links = build(world)
    findings, _ = evaluate(world, graph, links, script={ACCOUNT: answer})
    types = by_type(findings)
    assert set(types) == {"PAY-NAME-MISMATCH"}
    assert types["PAY-NAME-MISMATCH"]["details"]["hold"] is True


def test_no_account_on_file_is_unverified(world):
    graph, links = build(world, vendor={"bank_account_number": None})
    findings, provider = evaluate(world, graph, links)
    f = by_type(findings)["PAY-UNVERIFIED"]
    assert f["details"]["reason"] == "no_account_on_file"
    assert provider.calls == []


def test_amount_outlier_is_an_anomaly_with_impact(world):
    graph, links = build(
        world,
        vendor={"bank_verified_at": "2026-01-01T00:00:00"},
        invoice={"total_inc_tax": "500000"},
    )
    findings, _ = evaluate(world, graph, links)
    f = by_type(findings)["PAY-ANOMALY"]
    assert list(f["details"]["signals"]) == ["amount_zscore"]
    assert Decimal(f["impact"]) == Decimal(500000) - Decimal(100000)
    assert f["details"]["hold"] is False


def test_cadence_gap_is_an_anomaly(world):
    graph, links = build(
        world,
        vendor={"bank_verified_at": "2026-01-01T00:00:00"},
        invoice={"invoice_date": "2026-09-12"},
    )
    findings, _ = evaluate(world, graph, links)
    assert "cadence" in by_type(findings)["PAY-ANOMALY"]["details"]["signals"]


def test_first_time_vendor_is_an_anomaly(world):
    graph, links = build(
        world, vendor={"bank_verified_at": "2026-01-01T00:00:00", "history": []}
    )
    findings, _ = evaluate(world, graph, links)
    f = by_type(findings)["PAY-ANOMALY"]
    assert list(f["details"]["signals"]) == ["first_time_vendor"]
    assert f["impact"] is None


def test_batch_checks_only_open_invoices(world):
    graph, links = build(world)
    paid = graph.nodes_by_ref(T, "invoices", ["h-0"])["h-0"]
    pay = graph.upsert_node(CTX, kind="payment", natural_key="pay-1")
    graph.upsert_edge(
        CTX,
        src=pay,
        dst=paid,
        kind="settles",
        source_system="test",
        provenance="system_record",
        confidence=1.0,
        observed_at=NOW,
    )
    findings, provider = evaluate(world, graph, links, doc=None)
    held = {
        f["transaction_node"] for f in findings if f["type"] == "PAY-UNVERIFIED"
    }
    assert paid not in held and len(held) == 6  # inv-1 and h-1..h-5
    assert len(provider.calls) == 1  # one vendor, one verification


def test_contract_document_without_invoice_emits_nothing(world):
    graph, links = build(world)
    url, _ = world
    with tenant_session(T, url) as s:
        s.add(
            models.Document(
                tenant_id=T,
                id="doc-c",
                sha256="c" * 64,
                storage_key="k",
                kind="contract",
            )
        )
    findings, provider = evaluate(world, graph, links, doc="doc-c")
    assert findings == [] and provider.calls == []


def test_enforcing_hold_gates_payment(world, monkeypatch):
    graph, links = build(world)
    evaluate(world, graph, links, mode="enforcing")
    monkeypatch.setattr(service, "_graph_store", graph)
    url, _ = world
    message = gate.payment_hold(
        T,
        "case-1",
        url=url,
        settings_store=Store({"trust_engine_modes": {"payment": "enforcing"}}),
    )
    assert message and "PAY-UNVERIFIED" in message


def test_no_raw_account_number_in_findings_or_logs(world, caplog):
    other = "77778888999900"
    eml = (
        b"From: ap@acrne-steel.in\r\nReply-To: x@evil.co\r\n\r\n"
        + f"URGENT: pay to A/c {other} IFSC ICIC0004321 now.".encode()
    )
    graph, links = build(
        world,
        invoice={
            "source": "email",
            "sender": "ap@acrne-steel.in",
            "reply_to": "x@evil.co",
        },
        eml=eml,
    )
    with caplog.at_level(logging.DEBUG):
        findings, _ = evaluate(
            world,
            graph,
            links,
            script={ACCOUNT: RuntimeError(f"bad {ACCOUNT}")},
        )
    assert {"PAY-BEC", "PAY-UNVERIFIED"} <= set(by_type(findings))
    url, _ = world
    full = [FindingStore(url).get(T, f["id"]) for f in findings]
    dumped = json.dumps(full, default=str, ensure_ascii=False)
    logs = caplog.text + json.dumps(
        [r.__dict__ for r in caplog.records], default=str
    )
    for raw in (ACCOUNT, other):
        assert raw not in dumped and raw not in logs
    assert "…9900" in dumped


# -- AC-21 replay harness -----------------------------------------------------

SCENARIOS = sorted(p.name for p in FIXTURES.iterdir() if p.is_dir())


def test_replay_has_the_six_scenarios():
    assert SCENARIOS == sorted(
        [
            "clean",
            "lookalike_domain",
            "reply_to_swap",
            "bank_change_urgency",
            "unverified_first_payment",
            "provider_down",
        ]
    )


def _script(raw: dict) -> dict:
    """provider.json keys the vendor's account as "ACCOUNT", so no fixture
    file holds a raw number; {"raise": msg} scripts an outage."""
    return {
        (ACCOUNT if k == "ACCOUNT" else k): RuntimeError(v["raise"])
        if isinstance(v, dict) and "raise" in v
        else v
        for k, v in raw.items()
    }


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_replay(world, scenario):
    d = FIXTURES / scenario
    vendor = json.loads((d / "vendor.json").read_text())
    invoice = json.loads((d / "invoice.json").read_text())
    expected = json.loads((d / "expected.json").read_text())
    provider = d / "provider.json"
    script = (
        _script(json.loads(provider.read_text())) if provider.exists() else None
    )
    eml = (d / "email.eml").read_bytes()
    graph, links = build(world, vendor=vendor, invoice=invoice, eml=eml)
    findings, _ = evaluate(world, graph, links, script=script)
    types = by_type(findings)
    assert sorted(types) == sorted(expected["types"])
    assert sorted(t for t in types if t in finding_model.HOLD_TYPES) == sorted(
        expected["holds"]
    )
    for code, signals in expected.get("signals", {}).items():
        assert sorted(types[code]["details"]["signals"]) == sorted(signals)
    for code, details in expected.get("details", {}).items():
        for k, v in details.items():
            assert types[code]["details"][k] == v
    assert "50100234567891" not in json.dumps(findings, default=str)
