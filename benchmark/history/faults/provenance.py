"""Provenance fault family: an approved invoice re-submitted with changed bytes.

`inject` records the tampered cases in the manifest; `seed_graph` plants them
(and decoys) as graph nodes, because document bytes and approval are not CSV
history. Each case is an approved original and a re-submission with the same
number and vendor but a different `document_sha256`. Decoys that must not
fire: the same bytes re-sent, the same number under another vendor, and a
changed copy of an invoice nobody approved.

Planted numbers are `PRV-*`, never a history number, and amounts differ
within a pair, so other engines' duplicate rules do not see the pairs.

Provenance v2 adds `PROV-EDITED`: approved invoices whose PDF (real bytes,
planted under `data_dir/blobs` at their content address) was saved on top
of itself after its issue date, numbered `PRE-*`. The batch run scores them
from the bytes. Decoys that must not fire: one save (a signature), two
saves on the issue day, an editor-produced file never re-saved, a whole
rewrite on a later day, and a clean file.
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

TYPE = "PROV-TAMPER"
CASES = 6
EDITED = "PROV-EDITED"
EDITED_CASES = 10
# Long before the generated history, with no amount on the invoice: the
# payment engine's window and vendor medians never see these plants.
ISSUED = "2020-03-10"
GATES = {
    TYPE: {"precision": 1.0, "recall": 1.0, "impact_error": 0.0},
    EDITED: {"precision": 1.0, "recall": 0.9},
}


def inject(rng, rows, faults, *, start, months) -> None:
    vendors = [v["vendor_key"] for v in rows["vendor"]]
    for k in range(CASES):
        faults.append(
            {
                "type": TYPE,
                "doc_number": f"PRV-{k:03d}",
                "vendor_key": rng.choice(vendors),
                "expected_impact": str(rng.randint(5_000, 500_000)),
            }
        )
    # No draws from the shared rng: the families after this one see the
    # same random stream as before PROV-EDITED existed.
    for k in range(EDITED_CASES):
        faults.append(
            {
                "type": EDITED,
                "doc_number": f"PRE-{k:03d}",
                "vendor_key": vendors[k % len(vendors)],
                "expected_impact": None,
            }
        )


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
    amount_line: str | None = None,
    amount_font: str | None = None,
    saves: tuple = (),
) -> bytes:
    """A small, valid PDF built by hand: no writer library is a dependency.

    `amount_line` is written on its own line in `amount_font` (default: the
    body font); each date in `saves` appends an incremental update whose new
    Info dictionary carries that ModDate, as an editor's "save" does."""
    info = {
        "Producer": producer,
        "Creator": creator,
        "CreationDate": created,
        "ModDate": modified,
    }

    def info_body(values) -> str:
        return "".join(
            f"/{k} ({v})" for k, v in values.items() if v is not None
        )

    lines = [text[i : i + 60] for i in range(0, len(text), 60)] if text else []
    stream = (
        "BT /F1 10 Tf 12 TL 40 780 Td "
        + " ".join(f"({line}) Tj T*" for line in lines)
        + (f" /F2 10 Tf ({amount_line}) Tj T*" if amount_line else "")
        + " ET"
    )

    def font_obj(name: str) -> str:
        return (
            f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} /FontDescriptor"
            f" << /Type /FontDescriptor /FontName /{name} /Flags 32"
            " /FontBBox [0 0 1000 1000] /ItalicAngle 0 /Ascent 800"
            " /Descent -200 /CapHeight 700 /StemV 80 >> >>"
        )

    n_fixed = 5  # catalog, pages, body font, info, amount font
    page_ids = [n_fixed + 1 + 2 * i for i in range(pages)]
    objs = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids ["
        + " ".join(f"{p} 0 R" for p in page_ids)
        + f"] /Count {pages} >>",
        3: font_obj(font),
        4: f"<< {info_body(info)} >>",
        5: font_obj(amount_font or font),
    }
    for p in page_ids:
        objs[p] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {size[0]} {size[1]}]"
            " /Resources << /Font << /F1 3 0 R /F2 5 0 R >> >>"
            f" /Contents {p + 1} 0 R >>"
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
    size_n = len(objs) + 1
    out += (
        f"trailer\n<< /Size {size_n} /Root 1 0 R /Info 4 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    for when in saves:
        num, at = size_n, len(out)
        out += (
            f"{num} 0 obj\n<< {info_body({**info, 'ModDate': when})} >>\n"
            "endobj\n"
        ).encode("latin-1")
        prev, xref = xref, len(out)
        size_n += 1
        out += (
            f"xref\n{num} 1\n{at:010d} 00000 n \n"
            f"trailer\n<< /Size {size_n} /Root 1 0 R /Info {num} 0 R"
            f" /Prev {prev} >>\nstartxref\n{xref}\n%%EOF\n"
        ).encode()
    return out


def _sha(*parts) -> str:
    return hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()


def _plant(
    ctx,
    graph,
    vendor,
    number,
    role,
    *,
    sha,
    amount,
    approved,
    at,
    version=True,
    attrs=None,
):
    doc_id = f"prv-{number}-{role}"
    nid = graph.upsert_node(
        ctx,
        kind="invoice",
        natural_key=f"bench:provenance:{doc_id}",
        attrs={
            "invoice_number": number,
            "doc_number": number,
            **({"amount": str(amount)} if amount is not None else {}),
            "currency": "INR",
            "document_id": doc_id,
            "document_sha256": sha,
            "decision": "approve" if approved else None,
            **(attrs or {}),
        },
        at=at,
    )
    graph.upsert_edge(
        ctx,
        src=nid,
        dst=vendor,
        kind="issued_by",
        source_system="bench",
        provenance="system_record",
        confidence=1.0,
        observed_at=at,
        at=at,
    )
    if not version:
        return
    vid = graph.upsert_node(
        ctx,
        kind="document_version",
        natural_key=f"documents:{doc_id}:{sha}",
        ref_table="documents",
        ref_id=doc_id,
        attrs={"sha256": sha, "source": "email"},
        at=at,
    )
    graph.upsert_edge(
        ctx,
        src=vid,
        dst=nid,
        kind="evidences",
        source_system="bench",
        provenance="system_record",
        confidence=1.0,
        observed_at=at,
        at=at,
    )


# (producer, created, saves) per planted document; ISSUED is 2020-03-10.
_EDITED_PDFS = (
    ("PDF-XChange Editor 9.5", "D:20200310", ("D:20200402", "D:20200403")),
    ("Tally", "D:20200310", ("D:20200320", "D:20200321", "D:20200322")),
)
_DECOYS = (
    ("Tally", "D:20200310", ("D:20200312",)),  # one save: a signature
    ("Tally", "D:20200310", ("D:20200310", "D:20200310")),  # issue day
    ("iLovePDF", "D:20200310", ()),  # an editor, never re-saved
    ("Tally", "D:20200310", ()),  # clean
)


def _plant_pdf(ctx, graph, blobs, vendor, number, spec, *, amount, at):
    producer, created, saves = spec
    data = make_pdf(
        producer=producer,
        created=created,
        modified=created,
        text=f"Tax invoice {number} {WORDS}",
        amount_line=f"Total {amount}.00",
        saves=saves,
    )
    sha = hashlib.sha256(data).hexdigest()
    blobs.mkdir(parents=True, exist_ok=True)
    (blobs / f"{sha}.pdf").write_bytes(data)
    _plant(
        ctx, graph, vendor, number, "pdf", sha=sha, amount=None,
        approved=True, at=at, version=False,
        attrs={"invoice_date": ISSUED, "document_source": "upload"},
    )  # fmt: skip


def seed_graph(ctx, graph, data_dir: Path) -> None:
    at = datetime.now(UTC)
    manifest = json.loads((data_dir / "manifest.json").read_text())
    vendor_of: dict[str, str] = {}
    # Master-backed vendor nodes first; a run without a vendor file still
    # has the nodes the sync synthesised from the invoices themselves.
    for n in sorted(
        graph.nodes(ctx.tenant_id, kind="vendor"),
        key=lambda n: n["ref_table"] != "history_records",
    ):
        vendor_of.setdefault(n["attrs"].get("name"), n["id"])

    def vendor(name: str) -> str:
        if name not in vendor_of:
            vendor_of[name] = graph.upsert_node(
                ctx,
                kind="vendor",
                natural_key=f"bench:provenance:{name}",
                attrs={"name": name},
                at=at,
            )
        return vendor_of[name]

    for fault in manifest["faults"]:
        if fault["type"] != TYPE:
            continue
        number, amount = fault["doc_number"], Decimal(fault["expected_impact"])
        ven = vendor(fault["vendor_key"])
        _plant(
            ctx, graph, ven, number, "orig",
            sha=_sha(number, "orig"), amount=amount - 1, approved=True, at=at,
        )  # fmt: skip
        _plant(
            ctx, graph, ven, number, "resub",
            sha=_sha(number, "resub"), amount=amount, approved=False, at=at,
        )  # fmt: skip

    blobs = data_dir / "blobs" / "documents"
    for k, fault in enumerate(
        f for f in manifest["faults"] if f["type"] == EDITED
    ):
        _plant_pdf(
            ctx, graph, blobs, vendor(fault["vendor_key"]), fault["doc_number"],
            _EDITED_PDFS[k % len(_EDITED_PDFS)], amount=20_000 + 137 * k, at=at,
        )  # fmt: skip

    names = sorted(n for n in vendor_of if n)
    rng = random.Random(f"{manifest['seed']}:provenance-decoys")  # noqa: S311
    for k in range(6):
        number = f"PRV-D{k:02d}"
        a, b = rng.sample(names, 2)
        amount = Decimal(rng.randint(5_000, 500_000))
        same_bytes, other_vendor = k < 2, 2 <= k < 4
        _plant(
            ctx, graph, vendor(a), number, "orig",
            sha=_sha(number, "orig"), amount=amount - 1,
            approved=k < 4, at=at,
        )  # fmt: skip
        _plant(
            ctx, graph, vendor(b if other_vendor else a), number, "resub",
            sha=_sha(number, "orig" if same_bytes else "resub"),
            amount=amount, approved=False, at=at,
        )  # fmt: skip

    # PROV-EDITED decoys: scored from their bytes, never edited.
    for k, spec in enumerate(_DECOYS * 2):
        _plant_pdf(
            ctx, graph, blobs, vendor(names[k % len(names)]), f"PRE-D{k:02d}",
            spec, amount=40_000 + 211 * k, at=at,
        )  # fmt: skip
