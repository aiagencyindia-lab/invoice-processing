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
