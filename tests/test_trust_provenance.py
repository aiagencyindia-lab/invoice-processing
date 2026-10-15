"""Provenance v1: PDF facts, the ledger and score, and tamper after approval."""

import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from invoice_processing.api import service
from invoice_processing.db import models
from invoice_processing.db.session import create_all, get_engine, tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust import evaluate as ev
from invoice_processing.trust import gate, provenance
from invoice_processing.trust.decide import FakeDecider
from invoice_processing.trust.engine import EngineContext
from invoice_processing.trust.findings import model as finding_model
from invoice_processing.trust.findings.store import FindingStore
from invoice_processing.trust.graph.links import LinkStore
from invoice_processing.trust.graph.store import GraphStore
from invoice_processing.trust.graph.sync import sync_tenant
from invoice_processing.trust.provenance import pdfmeta

T = "tenant-acme"
CTX = RunContext(tenant_id=T, case_id="", run_id="r", actor="worker")
NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)

WORDS = " ".join(f"word{i}" for i in range(40))


def make_pdf(
    *,
    producer: str | None = "Tally",
    creator: str | None = "TallyPrime",
    created: str | None = "D:20250101100000+05'30'",
    modified: str | None = "D:20250101100000+05'30'",
    pages: int = 1,
    size: tuple = (595.28, 841.89),
    font: str = "Helvetica",
    text: str = WORDS,
) -> bytes:
    """A small, valid PDF built by hand: no writer library is a dependency."""
    info = {
        "Producer": producer,
        "Creator": creator,
        "CreationDate": created,
        "ModDate": modified,
    }
    info_body = "".join(f"/{k} ({v})" for k, v in info.items() if v is not None)
    lines = [text[i : i + 60] for i in range(0, len(text), 60)] if text else []
    stream = (
        "BT /F1 10 Tf 12 TL 40 780 Td "
        + " ".join(f"({line}) Tj T*" for line in lines)
        + " ET"
    )
    n_fixed = 4  # catalog, pages, font, info
    page_ids = [n_fixed + 1 + 2 * i for i in range(pages)]
    objs = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids ["
        + " ".join(f"{p} 0 R" for p in page_ids)
        + f"] /Count {pages} >>",
        3: f"<< /Type /Font /Subtype /Type1 /BaseFont /{font} /FontDescriptor"
        f" << /Type /FontDescriptor /FontName /{font} /Flags 32"
        " /FontBBox [0 0 1000 1000] /ItalicAngle 0 /Ascent 800 /Descent -200"
        " /CapHeight 700 /StemV 80 >> >>",
        4: f"<< {info_body} >>",
    }
    for p in page_ids:
        objs[p] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {size[0]} {size[1]}]"
            f" /Resources << /Font << /F1 3 0 R >> >> /Contents {p + 1} 0 R >>"
        )
        objs[p + 1] = (
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
        )
    out = b"%PDF-1.4\n"
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n{objs[num]}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for num in sorted(objs):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info 4 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return out


# -- PDF facts ---------------------------------------------------------------


def test_facts_read_metadata_pages_fonts_and_text():
    facts = pdfmeta.facts(make_pdf(pages=2))
    assert facts["producer"] == "Tally" and facts["creator"] == "TallyPrime"
    assert facts["creation_date"] == "2025-01-01T10:00:00+05:30"
    assert facts["mod_date"] == "2025-01-01T10:00:00+05:30"
    assert facts["page_count"] == 2
    assert facts["page_sizes"] == [[595, 842]]
    assert facts["fonts"] == ["Helvetica"]
    assert facts["has_text"] is True


def test_no_text_layer_is_reported():
    assert pdfmeta.facts(make_pdf(text=""))["has_text"] is False


def test_subset_prefix_is_stripped_from_font_names():
    assert pdfmeta.facts(make_pdf(font="ABCDEF+Arial"))["fonts"] == ["Arial"]


def test_pdf_date_parses_offsets_and_partial_forms():
    utc = timezone.utc
    assert pdfmeta.pdf_date("D:20250102") == datetime(2025, 1, 2, tzinfo=utc)
    assert pdfmeta.pdf_date("D:20250102030405Z") == datetime(
        2025, 1, 2, 3, 4, 5, tzinfo=utc
    )
    assert pdfmeta.pdf_date("20250102030405-02'00'").isoformat() == (
        "2025-01-02T03:04:05-02:00"
    )
    assert pdfmeta.pdf_date("yesterday") is None
    assert pdfmeta.pdf_date(None) is None
    assert pdfmeta.pdf_date("D:20251399") is None


def test_fingerprint_ignores_page_count_and_text_but_not_fonts_or_producer():
    base = pdfmeta.facts(make_pdf())
    same = pdfmeta.facts(make_pdf(pages=3, text="other words entirely"))
    assert pdfmeta.fingerprint(base) == pdfmeta.fingerprint(same)
    for changed in (
        make_pdf(font="Courier"),
        make_pdf(producer="iLovePDF"),
        make_pdf(size=(612, 792)),
    ):
        assert pdfmeta.fingerprint(pdfmeta.facts(changed)) != (
            pdfmeta.fingerprint(base)
        )


def test_oversize_bytes_are_skipped_without_parsing(monkeypatch, caplog):
    class Exploding:
        def __getattr__(self, name):
            raise AssertionError("pdfplumber must not be touched")

    monkeypatch.setitem(sys.modules, "pdfplumber", Exploding())
    data = b"%PDF-1.4\n" + b"\0" * pdfmeta.MAX_BYTES
    with caplog.at_level(logging.INFO):
        assert pdfmeta.facts(data) is None
    assert [
        r.reason for r in caplog.records if r.msg == "provenance_skipped"
    ] == ["too_large"]


def test_only_the_first_max_pages_are_read(monkeypatch):
    import pdfplumber.page  # noqa: PLC0415

    reads = []
    real = pdfplumber.page.Page.extract_text
    monkeypatch.setattr(
        pdfplumber.page.Page,
        "extract_text",
        lambda self, *a, **kw: (
            reads.append(self.page_number) or real(self, *a, **kw)
        ),
    )
    facts = pdfmeta.facts(make_pdf(pages=pdfmeta.MAX_PAGES + 3))
    assert facts["page_count"] == pdfmeta.MAX_PAGES + 3
    assert reads == list(range(1, pdfmeta.MAX_PAGES + 1))


def test_non_pdf_bytes_give_none():
    assert pdfmeta.facts(b"\x89PNG\r\n\x1a\n...") is None
    assert pdfmeta.facts(b"%PDF-1.4 truncated garbage") is None


# -- ledger, score, tamper ---------------------------------------------------


class Blobs(dict):
    def get(self, key):
        return self[key]


class World:
    """A tenant's graph with vendors, invoices and their document bytes."""

    def __init__(self, url):
        self.url, self.graph, self.links = url, GraphStore(url), LinkStore(url)
        self.blobs = Blobs()
        self.vendors: dict[str, str] = {}

    def vendor(self, name="Acme Supplies", domain="acme.in"):
        nid = self.graph.upsert_node(
            CTX,
            kind="vendor",
            natural_key=f"vendors:{name}",
            attrs={"name": name, "email_domain": domain},
        )
        self.vendors[name] = nid
        return nid

    def invoice(
        self,
        doc_id,
        *,
        number="INV-1",
        sha=None,
        vendor="Acme Supplies",
        decision=None,
        amount="1180.00",
        source="email",
        sender_domain="acme.in",
    ):
        nid = self.graph.upsert_node(
            CTX,
            kind="invoice",
            natural_key=f"invoices:{doc_id}",
            attrs={
                "document_id": doc_id,
                "document_sha256": sha or doc_id.ljust(64, "0"),
                "document_source": source,
                "document_sender_domain": sender_domain,
                "invoice_number": number,
                "amount": amount,
                "currency": "INR",
                "decision": decision,
            },
        )
        ven = self.vendors.get(vendor) or self.vendor(vendor)
        self.graph.upsert_edge(
            CTX,
            src=nid,
            dst=ven,
            kind="issued_by",
            source_system="v1",
            provenance="system_record",
            confidence=1.0,
            observed_at=NOW,
        )
        return nid

    def ectx(self, node=None, doc=None, settings=None):
        return EngineContext(
            ctx=CTX,
            mode="advisory",
            document=doc,
            invoice_node=node,
            graph=self.graph,
            links=self.links,
            findings=None,
            providers=None,
            settings=settings or {},
            decider=None,
            blobs=self.blobs,
            url=self.url,
            now=NOW,
        )

    def ingest(self, doc_id, pdf=None, *, kind="invoice", settings=None, **kw):
        """Plant the invoice and its bytes, then run the engine on the document."""
        node = self.invoice(doc_id, **kw)
        attrs = self.graph.node(T, node)["attrs"]
        self.blobs[f"documents/{doc_id}.pdf"] = (
            make_pdf() if pdf is None else pdf
        )
        doc = {
            "id": doc_id,
            "kind": kind,
            "sha256": attrs["document_sha256"],
            "storage_key": f"documents/{doc_id}.pdf",
            "source": attrs["document_source"],
            "source_ref": None,
            "sender": None,
            "reply_to": None,
            "filename": f"{doc_id}.pdf",
        }
        node = None if kind != "invoice" else node
        return node, provenance.run(self.ectx(node, doc, settings))

    def version(self, doc_id):
        sha = doc_id.ljust(64, "0")
        return self.graph.open_node(
            T, "document_version", f"documents:{doc_id}:{sha}"
        )


@pytest.fixture
def world(scratch_db):
    url = scratch_db("provenance.db")
    models.Base.metadata.drop_all(get_engine(url))
    create_all(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    w = World(url)
    w.vendor()
    return w


def test_types_are_registered_and_tamper_is_a_hold():
    assert provenance.ENGINE_VERSION == "provenance-1"
    assert finding_model.TYPE_ENGINE["PROV-TAMPER"] == "provenance"
    assert "PROV-TAMPER" in finding_model.HOLD_TYPES
    assert "PROV-SUSPECT" not in finding_model.HOLD_TYPES


def test_ledger_node_and_evidences_edge(world):
    node, specs = world.ingest("d1")
    assert specs == []
    v = world.version("d1")
    a = v["attrs"]
    assert v["ref_table"] == "documents" and v["ref_id"] == "d1"
    assert a["sha256"] == "d1".ljust(64, "0") and a["source"] == "email"
    assert a["sender_domain"] == "acme.in" and a["producer"] == "Tally"
    assert a["page_count"] == 1 and a["has_text"] is True
    assert a["fingerprint"] == pdfmeta.fingerprint(pdfmeta.facts(make_pdf()))
    assert a["actor"] == "worker"
    assert a["components"] == {
        "channel": 1.0,
        "template": 1.0,
        "text": 1.0,
        "meta": 1.0,
    }
    assert a["score"] == 1.0
    [edge] = world.graph.edges(T, src=v["id"], kind="evidences")
    assert edge["dst"] == node and edge["provenance"] == "system_record"
    assert edge["confidence"] == 1.0


@pytest.mark.parametrize(
    "source,sender,channel",
    [
        ("email", "acme.in", 1.0),
        ("email", "ACME.IN", 1.0),
        ("email", "acme-billing.in", 0.3),
        ("api", None, 0.8),
        ("upload", None, 0.6),
        ("whatsapp", None, 0.3),
    ],
)
def test_channel_values_by_source(world, source, sender, channel):
    world.ingest("d1", source=source, sender_domain=sender)
    assert world.version("d1")["attrs"]["components"]["channel"] == channel


def test_email_scores_other_when_the_vendor_has_no_domain(world):
    world.vendor("No Domain Ltd", domain=None)
    world.ingest("d1", vendor="No Domain Ltd")
    assert world.version("d1")["attrs"]["components"]["channel"] == 0.3


def test_template_is_one_below_min_history(world):
    world.ingest("d1")
    world.ingest("d2", number="INV-2", pdf=make_pdf(font="Courier"))
    world.ingest("d3", number="INV-3", pdf=make_pdf(font="Times-Roman"))
    # two prior documents, both different: still under the minimum of three
    assert world.version("d3")["attrs"]["components"]["template"] == 1.0


def test_template_is_share_of_matching_prior_fingerprints(world):
    for i in range(3):
        world.ingest(f"p{i}", number=f"INV-{i}")
    world.ingest("p3", number="INV-3", pdf=make_pdf(font="Courier"))
    # another vendor's documents never count
    world.vendor("Other Co", "other.in")
    world.ingest(
        "o1", number="X-1", vendor="Other Co", sender_domain="other.in"
    )
    world.ingest("d9", number="INV-9")
    assert world.version("d9")["attrs"]["components"]["template"] == 0.75
    assert world.version("d9")["attrs"]["template"] == {
        "prior": 4,
        "matching": 3,
    }


def test_template_threshold_is_read_from_the_tenant(world):
    world.ingest("p0", number="INV-0", pdf=make_pdf(font="Courier"))
    world.ingest(
        "d1",
        number="INV-1",
        settings={"trust_thresholds": {"provenance.template_min_history": 1}},
    )
    assert world.version("d1")["attrs"]["components"]["template"] == 0.0


@pytest.mark.parametrize(
    "pdf",
    [
        make_pdf(producer=None),
        make_pdf(created=None),
        make_pdf(created="D:20250201", modified="D:20250101"),
        make_pdf(created="D:20250101", modified="D:20990101"),
    ],
)
def test_meta_zero_when_dates_out_of_order_or_no_producer(world, pdf):
    world.ingest("d1", pdf=pdf)
    assert world.version("d1")["attrs"]["components"]["meta"] == 0.0


def test_missing_mod_date_counts_as_unmodified(world):
    world.ingest("d1", pdf=make_pdf(modified=None))
    assert world.version("d1")["attrs"]["components"]["meta"] == 1.0


def test_suspect_below_threshold_cites_the_version(world):
    for i in range(3):
        world.ingest(f"p{i}", number=f"INV-{i}")
    node, specs = world.ingest(
        "d1",
        number="INV-9",
        source="email",
        sender_domain="gmail.com",
        pdf=make_pdf(producer=None, font="Courier", text=""),
    )
    [spec] = specs
    v = world.version("d1")
    # 0.4*0.3 + 0.3*0 + 0.2*0 + 0.1*0
    assert v["attrs"]["score"] == 0.12
    assert spec.type == "PROV-SUSPECT" and spec.transaction_node == node
    assert spec.dedupe_key == "PROV-SUSPECT:d1"
    assert spec.impact is None and spec.engine_version == "provenance-1"
    assert [c.node_id for c in spec.citations] == [v["id"]]
    assert spec.details["components"] == v["attrs"]["components"]


def test_score_at_threshold_is_not_suspect(world):
    # upload 0.6 -> 0.24 + template 0.3 + text 0 + meta 0 = 0.54
    _, specs = world.ingest(
        "d1", source="upload", pdf=make_pdf(producer=None, text="")
    )
    assert world.version("d1")["attrs"]["score"] == 0.54
    assert specs == []
    _, specs = world.ingest(
        "d2",
        number="INV-2",
        source="upload",
        pdf=make_pdf(producer=None, text=""),
        settings={"trust_thresholds": {"provenance.suspect_below": 0.6}},
    )
    assert [s.type for s in specs] == ["PROV-SUSPECT"]


def test_non_pdf_document_is_skipped_and_logged(world, caplog):
    with caplog.at_level(logging.INFO):
        _, specs = world.ingest("d1", pdf=b"\x89PNG\r\n\x1a\nimage")
    assert specs == [] and world.version("d1") is None
    [rec] = [r for r in caplog.records if r.msg == "provenance_skipped"]
    assert rec.reason == "not_pdf"


def test_contract_document_is_ignored(world):
    node, specs = world.ingest("c1", kind="contract")
    assert node is None and specs == []
    assert world.version("c1") is None


# -- tamper ------------------------------------------------------------------


def test_tamper_when_an_approved_twin_has_another_sha(world):
    # its version exists from its own evaluation
    old, _ = world.ingest("old", number="INV-0042", decision="approve")
    new, specs = world.ingest("new", number="inv 42", amount="2360.50")
    [spec] = specs
    assert spec.type == "PROV-TAMPER" and spec.transaction_node == new
    assert spec.impact == Decimal("2360.50") and spec.currency == "INR"
    assert spec.dedupe_key == "PROV-TAMPER:invoices:new"
    cited = {c.node_id for c in spec.citations}
    assert cited == {
        new,
        old,
        world.version("new")["id"],
        world.version("old")["id"],
    }


def test_no_tamper_for_same_sha_or_unapproved_twin_or_other_vendor(world):
    world.invoice("a", number="INV-1", decision="approve", sha="s" * 64)
    world.invoice("b", number="INV-2", decision=None)
    world.vendor("Other Co", "other.in")
    world.invoice("c", number="INV-3", decision="approve", vendor="Other Co")
    world.invoice("d", number="INV-4", decision="reject")
    for doc, number in (("x1", "INV-1"), ("x2", "INV-2"), ("x3", "INV-3")):
        _, specs = world.ingest(
            doc, number=number, sha="s" * 64 if doc == "x1" else None
        )
        assert specs == []
    _, specs = world.ingest("x4", number="INV-4")
    assert specs == []


def test_approval_step_counts_as_approved(world):
    old = world.invoice("old", number="INV-7")
    step = world.graph.upsert_node(
        CTX,
        kind="approval",
        natural_key="approval_steps:s1",
        attrs={"position": 1, "status": "approved", "decided_by": "a@x.test"},
    )
    world.graph.upsert_edge(
        CTX,
        src=step,
        dst=old,
        kind="approves",
        source_system="v1",
        provenance="system_record",
        confidence=1.0,
        observed_at=NOW,
    )
    _, specs = world.ingest("new", number="INV-7")
    assert [s.type for s in specs] == ["PROV-TAMPER"]


def test_linked_vendor_nodes_count_as_one_vendor(world):
    a = world.vendors["Acme Supplies"]
    b = world.vendor("ACME SUPPLIES PVT LTD", "acme.in")
    world.links.propose_or_link(CTX, a, b, 0.99, "test")
    world.invoice("old", number="INV-5", decision="approve")
    _, specs = world.ingest(
        "new", number="INV-5", vendor="ACME SUPPLIES PVT LTD"
    )
    assert [s.type for s in specs] == ["PROV-TAMPER"]


def test_batch_run_finds_tamper_across_the_tenant(world):
    old = world.invoice("old", number="INV-1", decision="approve")
    new = world.invoice("new", number="INV-1")
    world.invoice("other", number="INV-2")
    specs = provenance.run(world.ectx())
    assert [(s.type, s.transaction_node) for s in specs] == [
        ("PROV-TAMPER", new)
    ]
    assert {c.node_id for c in specs[0].citations} == {new, old}
    assert world.graph.nodes(T, kind="document_version") == []


# -- end to end ----------------------------------------------------------------


class Settings:
    def __init__(self, modes):
        self.modes = modes

    def get(self, tenant_id):
        return {"trust_engine_modes": self.modes}


def test_evaluate_end_to_end_holds_in_enforcing(world, monkeypatch):
    url = world.url
    with tenant_session(T, url) as s:
        s.add(
            models.Vendor(
                tenant_id=T,
                id="v1",
                name="Acme Supplies",
                normalized_name="acme supplies",
                tax_id="27AAPFU0939F1ZV",
                email_domain="acme.in",
            )
        )
        for doc, sha, case, decision in (
            ("d-old", "1" * 64, "case-old", "approve"),
            ("d-new", "2" * 64, "case-new", None),
        ):
            s.add(
                models.Document(
                    tenant_id=T,
                    id=doc,
                    sha256=sha,
                    storage_key=f"documents/{sha}.pdf",
                    source="email",
                    sender="billing@acme.in",
                )
            )
            s.flush()
            s.add(
                models.Invoice(
                    tenant_id=T,
                    id=f"inv-{doc}",
                    document_id=doc,
                    case_id=case,
                    invoice_number="INV-0042",
                    vendor_name="Acme Supplies",
                    vendor_tax_id="27AAPFU0939F1ZV",
                    total_inc_tax=Decimal("1180.00"),
                    currency="INR",
                    decision=decision,
                    status="done",
                )
            )
            world.blobs[f"documents/{sha}.pdf"] = make_pdf()
    sync_tenant(CTX, world.graph, world.links, url=url)
    monkeypatch.setattr(service, "_graph_store", world.graph)
    monkeypatch.setattr(ev, "RUNNERS", {})
    settings = Settings({"provenance": "enforcing"})

    def evaluate(doc):
        return ev.evaluate(
            CTX,
            doc,
            graph=world.graph,
            links=world.links,
            url=url,
            settings_store=settings,
            decider=FakeDecider({}, fail=True),
            blobs=world.blobs,
        )

    assert evaluate("d-old")["engines"]["provenance"] == {
        "mode": "enforcing",
        "findings": 0,
    }
    assert evaluate("d-new")["engines"]["provenance"]["findings"] == 1
    [f] = FindingStore(url).list(T, type="PROV-TAMPER")
    assert f["details"]["hold"] is True and f["details"]["mode"] == "enforcing"
    assert Decimal(f["impact"]) == Decimal("1180.00")
    held = gate.payment_hold(T, "case-new", url=url, settings_store=settings)
    assert held and "PROV-TAMPER" in held
    assert (
        gate.payment_hold(T, "case-old", url=url, settings_store=settings)
        is None
    )
    versions = world.graph.nodes(T, kind="document_version")
    assert len(versions) == 2
    assert all(v["attrs"]["components"]["channel"] == 1.0 for v in versions)


def test_tamper_still_checked_for_a_non_pdf_document(world):
    world.invoice("old", number="INV-8", decision="approve")
    _, specs = world.ingest("new", number="INV-8", pdf=b"GIF89a image")
    assert [s.type for s in specs] == ["PROV-TAMPER"]
    assert world.version("new") is None


def test_tamper_summary_for_a_changed_resubmission(world):
    world.invoice("old", number="INV-8", decision="approve")
    _, [spec] = world.ingest("new", number="INV-8")
    assert "this copy may have been altered" in spec.summary


def test_tamper_summary_is_symmetric_when_both_copies_are_approved(world):
    world.invoice("a", number="INV-8", decision="approve")
    _, [spec] = world.ingest("b", number="INV-8", decision="approve")
    assert spec.summary == (
        "Two approved copies of invoice INV-8 from this vendor have "
        "different document bytes."
    )
    batch = provenance.run(world.ectx())
    assert len(batch) == 2
    assert all(s.summary.startswith("Two approved copies") for s in batch)
