"""Reality checks (FR-23): is what was invoiced corroborated by deliveries,
hours or seats on record?"""

from decimal import Decimal

import pytest

from invoice_processing.trust.reality import bands, classify

D = Decimal


# -- bands ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("found", "present", "result"),
    [
        ("100", True, "CORROBORATED"),
        ("120", True, "CORROBORATED"),  # over-delivery corroborates
        ("98", True, "CORROBORATED"),  # within 2 %
        ("97", True, "PARTIAL"),
        ("94", True, "PARTIAL"),  # within 3 x 2 %
        ("93", True, "CONTRADICTED"),
        ("0", True, "CONTRADICTED"),  # evidence, but none counts
        ("0", False, "NO_EVIDENCE"),
    ],
)
def test_band(found, present, result):
    assert bands.band(D("100"), D(found), present, D("0.02")) == result


# -- classification ----------------------------------------------------------


def _line(qty="1", unit=None, description="Widget"):
    return {
        "attrs": {"quantity": qty, "unit": unit, "description": description}
    }


@pytest.mark.parametrize(
    ("attrs", "lines", "kind"),
    [
        ({"po_number": "PO-1"}, [], "goods"),
        ({}, [_line("5", "kg")], "goods"),
        ({}, [_line("5")], "goods"),
        ({"po_number": "PO-1"}, [_line("10", "hr")], "services"),
        ({"work_authorization_reference": "WA-1"}, [_line()], "services"),
        ({}, [_line("3", "md")], "services"),
        ({}, [_line("40", "seat", "User licence seats")], "subscription"),
        ({}, [_line("1", None, "Annual subscription")], "subscription"),
        ({}, [_line("1")], "other"),
        ({}, [], "other"),
    ],
)
def test_classify(attrs, lines, kind):
    assert classify.classify({"attrs": attrs}, lines) == kind


def test_hours_of_reads_days_as_eight_hours():
    assert classify.hours_of(_line("2", "day")["attrs"]) == D("16")
    assert classify.hours_of(_line("5", "Hrs")["attrs"]) == D("5")
    assert classify.hours_of(_line("5", "kg")["attrs"]) is None


# -- the engine on a graph -----------------------------------------------------

import logging  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from fresh_schema import fresh_schema  # noqa: E402

from invoice_processing.db import models  # noqa: E402
from invoice_processing.db.session import tenant_session  # noqa: E402
from invoice_processing.ports import RunContext  # noqa: E402
from invoice_processing.trust import reality  # noqa: E402
from invoice_processing.trust.decide import FakeDecider  # noqa: E402
from invoice_processing.trust.engine import EngineContext  # noqa: E402
from invoice_processing.trust.graph.links import LinkStore  # noqa: E402
from invoice_processing.trust.graph.store import GraphStore  # noqa: E402
from invoice_processing.trust.providers.fakes import FakeProvider  # noqa: E402
from invoice_processing.trust.providers.registry import Registry  # noqa: E402

T = "tenant-real"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="worker")
AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


class Env:
    def __init__(self, url):
        self.url = url
        self.graph = GraphStore(url)
        self.links = LinkStore(url)
        self.scripts: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.seq = 0

    def node(self, kind, attrs, key=None):
        self.seq += 1
        return self.graph.upsert_node(
            CTX,
            kind=kind,
            natural_key=key or f"{kind}:{self.seq}",
            attrs=attrs,
            at=AT,
        )

    def edge(self, src, dst, kind):
        self.graph.upsert_edge(
            CTX,
            src=src,
            dst=dst,
            kind=kind,
            source_system="t",
            provenance="system_record",
            confidence=1.0,
            observed_at=AT,
            at=AT,
        )

    def vendor(self, name="Acme"):
        return self.node("vendor", {"name": name}, f"v:{name}")

    def invoice(self, vendor, lines, day="2026-02-10", **attrs):
        inv = self.node(
            "invoice",
            {
                "invoice_number": f"INV-{self.seq}",
                "invoice_date": day,
                "amount": "1000.00",
                "currency": "INR",
                **attrs,
            },
        )
        self.edge(inv, vendor, "issued_by")
        for n, (qty, price, unit, desc) in enumerate(lines, 1):
            line = self.node(
                "invoice_line",
                {
                    "line_number": n,
                    "quantity": qty,
                    "unit_price": price,
                    "unit": unit,
                    "description": desc,
                },
            )
            self.edge(line, inv, "part_of")
        return inv

    def po(self, inv, number="PO-1"):
        po = self.node("po", {"po_number": number}, f"po:{number}")
        self.edge(inv, po, "references")
        return po

    def evidence(self, kind, po=None, vendor=None, **attrs):
        node = self.node(kind, attrs)
        if po:
            self.edge(node, po, "fulfils")
        if vendor:
            self.edge(node, vendor, "issued_by")
        return node

    def ectx(self, invoice_node=None, settings=None, mode="advisory"):
        registry = Registry(self.graph)
        for cap in ("logistics", "timesheets", "tickets", "usage"):

            def factory(cap=cap):
                fake = FakeProvider(self.scripts.get(cap))
                fake.calls = self.calls
                return fake

            registry.register(cap, "fake", factory)
        return EngineContext(
            ctx=CTX,
            mode=mode,
            document=None if invoice_node is None else {"id": "d"},
            invoice_node=invoice_node,
            graph=self.graph,
            links=self.links,
            findings=None,
            providers=registry,
            settings=settings or {},
            decider=FakeDecider({}, fail=True),
            blobs=object(),
            url=self.url,
            now=NOW,
        )

    def run(self, invoice_node=None, **kw):
        return reality.run(self.ectx(invoice_node, **kw))

    def check(self, invoice):
        key = self.graph.node(T, invoice)["natural_key"]
        return self.graph.open_node(T, "reality_check", f"reality:{key}")


@pytest.fixture
def env(scratch_db):
    url = scratch_db("reality.db")
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Real", slug="real", status="active"))
    return Env(url)


def goods(env, qty="100", price="10.00"):
    ven = env.vendor()
    inv = env.invoice(ven, [(qty, price, "kg", "Steel rod")], po_number="PO-1")
    return ven, inv, env.po(inv)


def test_corroborated_goods_write_the_node_and_edges_but_no_finding(env):
    _ven, inv, po = goods(env)
    grn = env.evidence("grn", po, qty_received="100", invoice_date="2026-02-08")
    assert env.run(inv) == []
    node = env.check(inv)
    assert node["attrs"] == {
        "result": "CORROBORATED",
        "kind": "goods",
        "expected": "100",
        "found": "100",
        "unit": "units",
        "evidence": [grn],
        "window_days": 45,
        "engine_version": "reality-1",
    }
    [e] = env.graph.edges(T, src=grn, kind="evidences")
    assert e["dst"] == inv


def test_short_delivery_is_contradicted_and_priced(env):
    _ven, inv, po = goods(env)
    grn = env.evidence("grn", po, quantity="80", invoice_date="2026-02-08")
    [f] = env.run(inv)
    assert f.type == "REAL-CONTRADICTED"
    assert f.impact == D("200.00")
    key = env.graph.node(T, inv)["natural_key"]
    assert f.dedupe_key == f"REAL-CONTRADICTED:{key}"
    node = env.check(inv)
    assert [c.node_id for c in f.citations] == [inv, node["id"], grn]
    assert f.engine_version == "reality-1" and f.rule_version == "FR-23-v1"
    assert f.details["expected"] == "100" and f.details["found"] == "80"


def test_partial_is_an_insight_with_the_same_price(env):
    _ven, inv, po = goods(env)
    env.evidence("grn", po, quantity="95", invoice_date="2026-02-08")
    [f] = env.run(inv)
    assert f.type == "REAL-PARTIAL" and f.impact == D("50.00")


def test_late_delivery_does_not_count(env):
    _ven, inv, po = goods(env)
    env.evidence("grn", po, quantity="100", invoice_date="2026-04-30")
    [f] = env.run(inv)
    assert f.type == "REAL-CONTRADICTED" and f.details["found"] == "0"


def test_only_delivered_shipments_count(env):
    _ven, inv, po = goods(env)
    env.evidence("shipment", po, quantity="60", status="Delivered")
    env.evidence("shipment", po, quantity="40", status="in_transit")
    [f] = env.run(inv)
    assert f.details["found"] == "60"


def test_v1_receipt_is_summed_from_its_lines(env):
    _ven, inv, po = goods(env)
    grn = env.evidence("grn", po, received_on="2026-02-01")
    for q in ("60", "40"):
        line = env.node("grn_line", {"qty_received": q})
        env.edge(line, grn, "part_of")
    assert env.run(inv) == []
    assert env.check(inv)["attrs"]["found"] == "100"


def test_no_evidence_asks_the_provider_and_cites_its_answer(env):
    _ven, inv, _po = goods(env)
    [f] = env.run(inv)
    assert f.type == "REAL-NO-EVIDENCE" and f.impact is None
    assert env.calls == [("PO-1",)]
    prov = [
        c.node_id
        for c in f.citations
        if env.graph.node(T, c.node_id)["kind"] == "provider_evidence"
    ]
    assert len(prov) == 1


def test_provider_quantity_counts_as_evidence(env):
    _ven, inv, _po = goods(env)
    env.scripts["logistics"] = {
        "PO-1": {"quantity": 100, "status": "delivered"}
    }
    assert env.run(inv) == []


def test_services_hours_match_timesheets_and_tickets_in_the_month(env):
    ven = env.vendor()
    inv = env.invoice(ven, [("40", "50.00", "hr", "Consulting")])
    env.evidence("timesheet", vendor=ven, hours="30", invoice_date="2026-02-03")
    env.evidence("ticket", vendor=ven, hours="10", invoice_date="2026-02-20")
    env.evidence("timesheet", vendor=ven, hours="99", invoice_date="2026-01-31")
    assert env.run(inv) == []
    assert env.check(inv)["attrs"]["unit"] == "hours"


def test_service_period_overrides_the_invoice_month(env):
    ven = env.vendor()
    inv = env.invoice(
        ven,
        [("40", "50.00", "hr", "Consulting")],
        service_period_start="2026-01-01",
        service_period_end="2026-01-31",
    )
    env.evidence("timesheet", vendor=ven, hours="20", invoice_date="2026-01-15")
    [f] = env.run(inv)
    assert f.type == "REAL-CONTRADICTED" and f.impact == D("1000.00")


def test_services_without_hours_on_record_ask_timesheets_then_tickets(env):
    ven = env.vendor()
    inv = env.invoice(ven, [("5", "100.00", "day", "Engineer")])
    [f] = env.run(inv)
    assert f.type == "REAL-NO-EVIDENCE"
    assert [c[0] for c in env.calls] == ["Acme", "Acme 2026-02-01/2026-02-28"]
    assert env.check(inv)["attrs"]["expected"] == "40"


def test_seats_use_the_latest_count_by_period_end(env):
    ven = env.vendor()
    inv = env.invoice(ven, [("50", "8.00", "seat", "User licences")])
    env.evidence(
        "seat_count", vendor=ven, quantity="50", invoice_date="2026-01-05"
    )
    env.evidence(
        "seat_count", vendor=ven, quantity="40", invoice_date="2026-02-05"
    )
    env.evidence(
        "seat_count", vendor=ven, quantity="50", invoice_date="2026-03-05"
    )
    [f] = env.run(inv)
    assert f.type == "REAL-CONTRADICTED" and f.impact == D("80.00")
    assert f.details["unit"] == "seats"


def test_other_and_lineless_invoices_get_no_check(env):
    ven = env.vendor()
    inv = env.invoice(ven, [("1", "10.00", None, "Fee")])
    bare = env.invoice(ven, [], po_number="PO-9")
    assert env.run() == []
    assert env.check(inv) is None and env.check(bare) is None


def test_a_contract_document_is_skipped(env):
    goods(env)
    ectx = env.ectx()
    ectx.document = {"id": "contract"}
    assert reality.run(ectx) == []


def test_logs_carry_no_ids(env, caplog):
    _ven, inv, _po = goods(env)
    with caplog.at_level(logging.INFO):
        env.run()
    for rec in caplog.records:
        extra = {k: v for k, v in rec.__dict__.items() if isinstance(v, str)}
        assert inv not in extra.values()


# -- plausibility --------------------------------------------------------------


def history(env, n=5, price="10.00", qty="100", desc="Steel Rod"):
    ven = env.vendor()
    for k in range(n):
        env.invoice(ven, [(qty, price, "kg", desc)], day=f"2025-0{k + 1}-10")
    return ven


def of_type(specs, code):
    return [s for s in specs if s.type == code]


def test_price_jump_against_the_vendors_history(env):
    ven = history(env)
    inv = env.invoice(ven, [("100", "30.00", "kg", "steel  rod")])
    [f] = of_type(env.run(inv), "REAL-PRICE-JUMP")
    assert f.impact == D("2000.00")
    key = env.graph.node(T, inv)["natural_key"]
    assert f.dedupe_key == f"REAL-PRICE-JUMP:{key}:1"
    assert f.details["median"] == "10.00" and f.details["z"] == "inf"
    assert inv in [c.node_id for c in f.citations]


def test_too_little_history_is_quiet(env):
    ven = history(env, n=4)
    inv = env.invoice(ven, [("100", "30.00", "kg", "Steel Rod")])
    assert of_type(env.run(inv), "REAL-PRICE-JUMP") == []


def test_a_contract_rate_written_during_the_history_explains_the_jump(env):
    ven = history(env)
    contract = env.node("contract", {"filename": "msa.pdf"})
    env.edge(contract, ven, "governs")
    clause = env.node("contract_clause", {"type": "rate"})
    env.edge(clause, contract, "part_of")
    inv = env.invoice(ven, [("100", "30.00", "kg", "Steel Rod")])
    assert of_type(env.run(inv), "REAL-PRICE-JUMP") == []


def test_a_clause_older_than_the_history_does_not(env):
    ven = env.vendor()
    contract = env.node("contract", {"filename": "msa.pdf"})
    env.edge(contract, ven, "governs")
    clause = env.node("contract_clause", {"type": "rate"})
    env.edge(clause, contract, "part_of")
    for k in range(5):
        env.invoice(
            ven, [("100", "10.00", "kg", "Steel Rod")], day=f"2026-0{k + 2}-10"
        )
    inv = env.invoice(
        ven, [("100", "30.00", "kg", "Steel Rod")], day="2026-07-10"
    )
    assert len(of_type(env.run(inv), "REAL-PRICE-JUMP")) == 1


def test_volume_spike(env):
    ven = history(env)
    inv = env.invoice(ven, [("1000", "10.00", "kg", "Steel Rod")])
    [f] = of_type(env.run(inv), "REAL-VOLUME-SPIKE")
    assert f.impact == D("9000.00")
    assert of_type(env.run(inv), "REAL-PRICE-JUMP") == []


def test_cheaper_or_smaller_is_not_a_jump_or_spike(env):
    ven = history(env)
    inv = env.invoice(ven, [("10", "5.00", "kg", "Steel Rod")])
    out = env.run(inv)
    assert (
        of_type(out, "REAL-PRICE-JUMP")
        == of_type(out, "REAL-VOLUME-SPIKE")
        == []
    )


def test_shutdown_period_overlap(env):
    ven = env.vendor()
    inv = env.invoice(ven, [("1", "10.00", None, "Fee")])
    periods = {"trust_shutdown_periods": [["2026-02-01", "2026-02-15"]]}
    [f] = env.run(inv, settings=periods)
    assert f.type == "REAL-SHUTDOWN" and f.impact == D("1000.00")
    later = {"trust_shutdown_periods": [["2026-03-01", "2026-03-15"]]}
    assert env.run(inv, settings=later) == []


def test_service_period_overlapping_a_shutdown_counts(env):
    ven = env.vendor()
    inv = env.invoice(
        ven,
        [("1", "10.00", None, "Fee")],
        day="2026-04-02",
        service_period_start="2026-03-01",
        service_period_end="2026-03-31",
    )
    periods = {"trust_shutdown_periods": [["2026-03-20", "2026-03-22"]]}
    assert [f.type for f in env.run(inv, settings=periods)] == ["REAL-SHUTDOWN"]


# -- through evaluate: modes and holds ----------------------------------------

from datetime import date  # noqa: E402

from invoice_processing.trust import evaluate as ev  # noqa: E402
from invoice_processing.trust import holds  # noqa: E402
from invoice_processing.trust.findings.model import (  # noqa: E402
    Citation,
    FindingSpec,
)
from invoice_processing.trust.findings.store import FindingStore  # noqa: E402
from invoice_processing.trust.graph.sync import sync_tenant  # noqa: E402
from invoice_processing.trust.reality.requests import (  # noqa: E402
    RequestClosed,
    RequestStore,
)


class Store:
    def __init__(self, settings):
        self.settings = settings

    def get(self, tenant_id):
        return self.settings


def evaluate(env, mode, document_id=None):
    return ev.evaluate(
        CTX,
        document_id,
        graph=env.graph,
        links=env.links,
        url=env.url,
        settings_store=Store({"trust_engine_modes": {"reality": mode}}),
        providers=env.ectx().providers,
        decider=FakeDecider({}, fail=True),
        blobs=object(),
    )


@pytest.mark.parametrize(
    ("mode", "held"), [("enforcing", True), ("advisory", False)]
)
def test_contradiction_holds_only_when_enforcing(env, mode, held):
    _ven, inv, po = goods(env)
    env.evidence("grn", po, quantity="50", invoice_date="2026-02-08")
    evaluate(env, mode)
    evaluate(env, mode)  # a re-run refreshes, never doubles
    store = FindingStore(env.url)
    [f] = store.list(T, type="REAL-CONTRADICTED")
    assert f["details"]["hold"] is True and f["details"]["mode"] == mode
    assert len(env.graph.nodes(T, kind="reality_check")) == 1
    assert bool(holds.open_for(T, inv, url=env.url)) is held


def test_evidence_arriving_releases_the_hold(env):
    with tenant_session(T, env.url) as s:
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
                invoice_date=date(2026, 2, 10),
                vendor_name="Acme",
                po_number="PO-1",
                total_inc_tax=D("1000"),
                status="done",
            )
        )
        s.flush()
        s.add(
            models.InvoiceLine(
                tenant_id=T,
                invoice_id="inv-1",
                line_number=1,
                description="Steel rod",
                quantity=D("100"),
                unit_price=D("10"),
            )
        )
    po = env.node("po", {"po_number": "PO-1"}, "po:PO-1")
    env.evidence("grn", po, quantity="50", invoice_date="2026-02-08")
    sync_tenant(CTX, env.graph, env.links, url=env.url, document_id="doc-1")
    evaluate(env, "enforcing", "doc-1")
    store = FindingStore(env.url)
    [f] = store.list(T, type="REAL-CONTRADICTED")
    assert f["status"] == "detected" and D(f["impact"]) == D("500")
    env.evidence("grn", po, quantity="50", invoice_date="2026-02-09")
    evaluate(env, "enforcing", "doc-1")
    [f] = store.list(T, type="REAL-CONTRADICTED")
    assert f["status"] == "rejected"


# -- evidence requests ---------------------------------------------------------


def finding_on(env, inv, code="REAL-NO-EVIDENCE"):
    fid, _ = FindingStore(env.url).upsert(
        CTX,
        FindingSpec(
            type=code,
            transaction_node=inv,
            dedupe_key=f"{code}:x:{inv}",
            impact=None,
            currency="INR",
            confidence=0.5,
            engine_version="reality-1",
            rule_version="FR-23-v1",
            model_version="none",
            summary="s",
            citations=(Citation(inv),),
        ),
    )
    return fid


def test_request_lifecycle(env):
    _ven, inv, _po = goods(env)
    fid = finding_on(env, inv)
    store = RequestStore(env.url)
    row = store.create(
        CTX,
        fid,
        owner="ops@acme.test",
        due_date=date(2026, 2, 1),
        note="POD please",
        requested_by="clerk@acme.test",
    )
    assert row["status"] == "open" and row["due_date"] == "2026-02-01"
    assert (
        store.list(T, overdue=True, today=date(2026, 2, 2))[0]["id"]
        == row["id"]
    )
    assert store.list(T, overdue=True, today=date(2026, 1, 31)) == []
    assert store.list(T, status="fulfilled") == []
    done = store.fulfil(CTX, row["id"], "doc-p", at=NOW)
    assert (
        done["status"] == "fulfilled" and done["proof_document_id"] == "doc-p"
    )
    assert done["overdue"] is False
    assert store.proofs(T) == {inv: ["doc-p"]}
    with pytest.raises(RequestClosed):
        store.cancel(CTX, row["id"])
    other = store.create(
        CTX, fid, owner="o", due_date=None, note=None, requested_by="c"
    )
    assert store.cancel(CTX, other["id"])["status"] == "cancelled"
    with pytest.raises(KeyError):
        store.get(T, "nope") or store.cancel(CTX, "nope")
    assert store.get("tenant-other", row["id"]) is None


def proof(env, inv, code):
    store = RequestStore(env.url)
    row = store.create(
        CTX,
        finding_on(env, inv, code),
        owner="o",
        due_date=None,
        note=None,
        requested_by="c",
    )
    store.fulfil(CTX, row["id"], "doc-p", at=NOW)
    return env.graph.upsert_node(
        CTX,
        kind="document_version",
        natural_key="documents:doc-p:abc",
        ref_table="documents",
        ref_id="doc-p",
        attrs={"kind": "proof"},
        at=AT,
    )


def test_a_proof_corroborates_an_invoice_with_no_evidence(env):
    _ven, inv, _po = goods(env)
    node = proof(env, inv, "REAL-NO-EVIDENCE")
    assert env.run(inv) == []
    check = env.check(inv)["attrs"]
    assert check["result"] == "CORROBORATED" and node in check["evidence"]


def test_a_proof_never_outweighs_contrary_evidence(env):
    _ven, inv, po = goods(env)
    env.evidence("grn", po, quantity="50", invoice_date="2026-02-08")
    proof(env, inv, "REAL-CONTRADICTED")
    [f] = env.run(inv)
    assert f.type == "REAL-CONTRADICTED"


# -- explanations --------------------------------------------------------------

from invoice_processing.trust.explain import engine as explain  # noqa: E402
from invoice_processing.trust.explain.templates import TEMPLATES  # noqa: E402
from tests.test_trust_explain import load  # noqa: E402

REAL_CODES = (
    "REAL-CONTRADICTED",
    "REAL-PARTIAL",
    "REAL-NO-EVIDENCE",
    "REAL-PRICE-JUMP",
    "REAL-VOLUME-SPIKE",
    "REAL-SHUTDOWN",
)


def explained(code):
    f, graph, _ = load(code)
    return explain.build(T, f, graph=graph)


@pytest.mark.parametrize("code", REAL_CODES)
def test_every_code_has_a_template_that_passes_check(code):
    assert code in TEMPLATES
    x = explained(code)  # build() runs check()
    assert "evidence request" in x.next_action.lower()


def test_contradicted_names_expected_and_found_with_the_unit():
    x = explained("REAL-CONTRADICTED")
    assert "100 units" in x.what_happened and "80" in x.what_happened
    assert any("GRN" in w and "POD" in w for w in x.would_change)
    assert any(s.node_id == "n-grn" for s in x.why)


@pytest.mark.parametrize(
    ("kind", "words"),
    [("services", "timesheet"), ("subscription", "seat report")],
)
def test_would_change_names_the_missing_document(kind, words):
    f, graph, _ = load("REAL-NO-EVIDENCE")
    graph._nodes["n-real"]["attrs"]["kind"] = kind
    x = explain.build(T, f, graph=graph)
    assert any(words in w.lower() for w in x.would_change)


# -- benchmark family ----------------------------------------------------------

import json  # noqa: E402
from collections import Counter  # noqa: E402

from benchmark.history import eval as history_eval  # noqa: E402
from benchmark.history.faults import reality as family  # noqa: E402
from benchmark.history.generate import generate  # noqa: E402


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    data = tmp_path_factory.mktemp("bench") / "data"
    generate(7, data)
    return data


def test_family_plants_the_spec_counts_with_exact_prices(generated):
    manifest = json.loads((generated / "manifest.json").read_text())
    mine = [f for f in manifest["faults"] if f["type"].startswith("REAL-")]
    by_kind = Counter(
        (f["type"], f.get("kind"))
        for f in mine
        if f["type"] != "REAL-NO-EVIDENCE"
    )
    assert by_kind == {
        ("REAL-CONTRADICTED", "goods"): 30,
        ("REAL-CONTRADICTED", "services"): 15,
        ("REAL-CONTRADICTED", "subscription"): 10,
        ("REAL-PRICE-JUMP", "goods"): 20,
        ("REAL-VOLUME-SPIKE", "goods"): 10,
        # Leakage's contract lines the payment family billed at 5x.
        ("REAL-VOLUME-SPIKE", None): by_kind[("REAL-VOLUME-SPIKE", None)],
    }
    assert by_kind[("REAL-VOLUME-SPIKE", None)] > 0
    for f in mine:
        if f["type"] == "REAL-NO-EVIDENCE":
            assert f["expected_impact"] is None
        else:
            assert D(f["expected_impact"]) > 0
    again = generated.parent / "again"
    generate(7, again)
    assert (again / "manifest.json").read_text() == (
        generated / "manifest.json"
    ).read_text()
    assert (again / "timesheet.csv").read_bytes() == (
        generated / "timesheet.csv"
    ).read_bytes()


def test_eval_meets_the_reality_gates(generated, scratch_db):
    out = history_eval.run(scratch_db("bench-reality.db"), generated)
    gates = out["gates"]
    for code in family.GATES:
        for metric in family.GATES[code]:
            assert gates[f"{code}.{metric}"], (code, metric, out["types"][code])
    assert (
        gates["reality_coverage_goods"] and gates["reality_coverage_services"]
    )
    assert out["reality"]["reality_coverage_goods"] >= 0.70
    assert out["passed"], {k: v for k, v in gates.items() if not v}


def test_coverage_with_nothing_checked_is_not_a_failure(env):
    from benchmark.history import reality as measure_module  # noqa: PLC0415

    out = measure_module.measure(T, env.graph, None, {}, env.url)
    assert out["reality_coverage_goods"] is None
    assert out["gates"] == {
        "reality_coverage_goods": True,
        "reality_coverage_services": True,
    }
