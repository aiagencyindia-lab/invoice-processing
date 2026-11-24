"""Can a Hugging Face vision model read our invoices as well as Gemini Pro?

Renders each benchmark PDF's pages to images, asks the model (via the HF
router's OpenAI-compatible chat API) for the same fields the pipeline extracts,
and scores them with the benchmark's own matcher against ground truth. Pro's
scores on the same invoices come from the 2026-11-05 production run.

    set -a; source .env; set +a   # HF_TOKEN; Vision needs ADC + PROJECT_ID
    uv run python -m benchmark.experiments.hf_model --n 10

An experiment, not product code: nothing in the pipeline imports it.
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import pypdfium2

from benchmark.harness import dataset, normalize
from benchmark.harness.score import HEADER_FIELDS, TOTAL_FIELDS, _match
from invoice_processing import tax_ids
from invoice_processing.shared_libraries.acting import (
    general_invoice_agent as gia,
)

URL = "https://router.huggingface.co/v1/chat/completions"
FIELDS = [app for app, _, _ in HEADER_FIELDS + TOTAL_FIELDS]

EXTRA = [
    "other_charges_total",
    "discount",
    "round_off",
    "invoice_count",
    "document_type",
]  # for the checks only

PROMPT = f"""You are reading an Indian GST tax invoice. Its pages are attached
as images, and its text (from the PDF's own text layer, or OCR for a scan)
follows below. Copy identifiers and amounts from that text where it has them:
it is exact, the images are not.
Return ONLY one JSON object, no prose, with these keys:
{json.dumps(FIELDS + EXTRA)} and "lines".
- Copy identifiers (invoice number, PO number, work authorization reference,
  GSTINs) exactly as printed. Use null for anything not on the invoice.
- Dates as YYYY-MM-DD. Amounts as plain numbers in rupees, no commas.
- invoice_total_ex_tax = taxable value plus any freight/other charges, before GST.
- tax_amount = total GST (CGST + SGST + IGST).
- invoice_total_inc_tax = the grand total payable.
- customer_name = the buyer (bill-to) legal name.
- other_charges_total = freight, packing, insurance and other charges (0 if none);
  discount = any discount (0 if none); round_off as printed (0 if none).
- "lines": one object per goods/service line item, in order, with
  "description" and "amount_ex_tax" (the line's taxable value before GST).
  Freight, packing, other charges, discounts, round-off and tax rows are NOT
  line items, even when they sit inside the item table. If the invoice has
  no itemized table (only a lump sum), return "lines": [].
- invoice_count = how many separate invoices this file contains.
- document_type = what the document calls itself, one of: "tax_invoice",
  "proforma_invoice", "quotation", "credit_note", "debit_note",
  "delivery_challan", "purchase_order", "other". Go by its printed title.
- Never correct the invoice. If its GSTIN looks invalid or its numbers do not
  add up, report exactly what is printed: catching those errors is the point."""


def invoice_text(pdf: Path) -> tuple[str, int]:
    """The production order: the PDF's own text, else Vision OCR.

    Returns the text and how many pages went to Vision (for cost)."""
    text = gia.extract_pdf_to_markdown(pdf)
    if gia._has_text(text):
        return text, 0
    text = gia.extract_pdf_with_vision(pdf)
    return text, text.count("## Page ")


def _amt(got: dict, k: str) -> float:
    v = got.get(k)
    return 0.0 if v is None else normalize.amount(str(v)) or 0.0


IDENT_KEYS = (
    "invoice_number",
    "po_number",
    "work_authorization_reference",
    "vendor_tax_id",
    "customer_tax_id",
)
# tax_amount and invoice_total_ex_tax are often sums, not printed figures;
# the arithmetic checks below cover them.
AMOUNT_KEYS = (
    "invoice_total_inc_tax",
    "cgst_amount",
    "sgst_amount",
    "igst_amount",
)


def _flat(text: str) -> str:
    # Keep spaces between tokens: "4,897.80 18%" must not become "4897.8018%".
    return re.sub(r"[,₹]", "", text).upper()


def printed(value, flat: str, amount: bool) -> bool:
    """Is this value literally in the invoice text? A cheap model asked to
    make an invoice add up invents the number that would; it cannot invent
    one that is printed."""
    if not amount:
        return re.sub(r"\s", "", str(value)).upper() in re.sub(r"\s", "", flat)
    a = normalize.amount(str(value))
    if a is None:
        return False
    forms = {f"{a:.2f}", f"{a:.1f}"} | ({str(int(a))} if a == int(a) else set())
    return any(
        re.search(rf"(?<![\d.]){re.escape(f)}(?!\d)", flat) for f in forms
    )


def stops(got: dict, text: str) -> list[str]:
    """Reasons to hand the invoice to Pro without a retry: retrying these
    only teaches the model to hide them."""
    out = []
    if got.get("document_type") != "tax_invoice":
        out.append(f"not a tax invoice: {got.get('document_type')}")
    if (normalize.amount(str(got.get("invoice_count") or 1)) or 1) > 1:
        out.append("more than one invoice in the file")
    if not got.get("lines"):
        out.append("no line items")
    flat = _flat(text)
    for k in ("vendor_tax_id", "customer_tax_id"):
        v = got.get(k)
        if (
            v
            and printed(v, flat, False)
            and not tax_ids.validate_gstin(str(v))[0]
        ):
            out.append(f"{k} as printed fails the GSTIN check")
    return out


def problems(got: dict, text: str) -> list[str]:
    """What a reader can check without ground truth: every value printed in
    the invoice, and the invoice's own arithmetic. Worded so a retry copies
    more carefully, never so it can compute the 'right' value."""
    out = []
    flat = _flat(text)
    for k in IDENT_KEYS:
        if got.get(k) and not printed(got[k], flat, False):
            out.append(
                f"{k} {got[k]!r} does not appear in the invoice text; copy it exactly as printed"
            )
    for k in AMOUNT_KEYS:
        if _amt(got, k) and not printed(got[k], flat, True):
            out.append(
                f"{k} {got[k]} does not appear in the invoice text; copy it exactly as printed"
            )
    for i, ln in enumerate(got.get("lines") or [], 1):
        if _amt(ln, "amount_ex_tax") and not printed(
            ln["amount_ex_tax"], flat, True
        ):
            out.append(
                f"line {i} amount {ln['amount_ex_tax']} does not appear in the invoice text; copy it exactly as printed"
            )
    parts = sum(
        _amt(got, k) for k in ("cgst_amount", "sgst_amount", "igst_amount")
    )
    tax = _amt(got, "tax_amount")
    if abs(parts - tax) > 1:
        out.append(
            f"cgst+sgst+igst ({parts:.2f}) differs from tax_amount ({tax:.2f}); check you copied each as printed"
        )
    ex, inc = (
        _amt(got, "invoice_total_ex_tax"),
        _amt(got, "invoice_total_inc_tax"),
    )
    if abs(ex + tax + _amt(got, "round_off") - inc) > 1.5:
        out.append(
            "invoice_total_ex_tax + tax_amount + round_off differs from invoice_total_inc_tax; check you copied each as printed"
        )
    lines = sum(_amt(ln, "amount_ex_tax") for ln in got.get("lines") or [])
    base = ex - _amt(got, "other_charges_total")
    if min(abs(lines - base), abs(lines - base - _amt(got, "discount"))) > 1:
        out.append(
            "the lines do not add up to the subtotal; check whether you listed a non-item row (freight, charges, tax) as a line or missed an item row"
        )
    return out


_PDFIUM = threading.Lock()  # pdfium is not thread-safe: it aborts the process


def page_images(pdf: Path, scale: float = 1.5) -> list[str]:
    with _PDFIUM:
        return _render(pdf, scale)


def _render(pdf: Path, scale: float) -> list[str]:
    doc = pypdfium2.PdfDocument(str(pdf))
    out = []
    for page in doc:
        buf = io.BytesIO()
        page.render(scale=scale).to_pil().convert("RGB").save(
            buf, "JPEG", quality=85
        )
        out.append(
            "data:image/jpeg;base64,"
            + base64.b64encode(buf.getvalue()).decode()
        )
    return out


DEADLINE_S = 240


def _post_with_deadline(body: dict, token: str) -> httpx.Response:
    """httpx's timeout is per read, and a router that trickles bytes never
    trips it: one request hung a run for 13 hours. Bound the whole call."""
    one = ThreadPoolExecutor(1)
    fut = one.submit(
        httpx.post,
        URL,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    try:
        return fut.result(timeout=DEADLINE_S)
    finally:
        one.shutdown(
            wait=False
        )  # ponytail: a hung thread is abandoned; main os._exit()s


def ask(
    model: str, images: list[str], token: str, text: str, fix: list[str] = ()
) -> tuple[dict, dict]:
    note = ""
    if fix:
        note = (
            "\n\nA previous reading of this invoice may have copied something wrongly. Re-read these spots and copy exactly what is printed. If the invoice itself prints it that way, keep it: do not correct the invoice.\n- "
            + "\n- ".join(fix)
        )
    content = [
        {"type": "text", "text": PROMPT + note + "\n\nINVOICE TEXT:\n" + text}
    ] + [{"type": "image_url", "image_url": {"url": u}} for u in images]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
    }
    for attempt in range(4):
        r = _post_with_deadline(body, token)
        if r.status_code not in (429, 500, 502, 503, 504):
            break
        time.sleep(5 * 2**attempt)
    r.raise_for_status()
    d = r.json()
    text = d["choices"][0]["message"]["content"] or ""
    start = text.find("{")  # skips ```json fences and prose
    if start < 0:
        raise ValueError(f"no JSON in reply: {text[:200]!r}")
    # raw_decode stops at the first object: replies sometimes carry two
    got = json.JSONDecoder().raw_decode(text[start:])[0]
    lines = got.get("lines") or []
    if not isinstance(lines, list) or not all(isinstance(x, dict) for x in lines):
        raise ValueError("reply has the wrong shape")  # an error goes to Pro
    return got, d.get("usage") or {}


def run_one(rec: dict, model: str, token: str, scale: float = 1.5) -> dict:
    out = {"invoice_id": rec["invoice_id"], "pages": rec["page_count"]}
    t0 = time.time()
    pdf = dataset.DATASET / rec["document_file"]
    hf_cost = 0.0
    try:
        text, vision_pages = invoice_text(pdf)
        images = page_images(pdf, scale)
        first = []
        try:
            got, usage = ask(model, images, token, text)
            hf_cost += usage.get("cost") or 0
            first = [] if stops(got, text) else problems(got, text)
        except ValueError as e:  # unparseable reply: one more try, below
            got, first = None, [f"the reply was not valid JSON ({e})"]
        if first:
            got, usage = ask(model, images, token, text, first)
            hf_cost += usage.get("cost") or 0
        final = stops(got, text) + problems(got, text)
    except Exception as e:
        return out | {
            "error": f"{type(e).__name__}: {e}"[:300],
            "seconds": round(time.time() - t0, 1),
        }
    sub = dict(rec["as_submitted"])
    sub["_total_before_tax"] = str(
        (normalize.amount(sub.get("taxable_amount")) or 0)
        + (normalize.amount(sub.get("other_charges_total")) or 0)
    )
    as_str = {k: None if got.get(k) is None else str(got[k]) for k in FIELDS}
    correct = {
        app: _match(kind, as_str[app], sub.get(gt))
        for app, gt, kind in HEADER_FIELDS + TOTAL_FIELDS
    }
    lines = got.get("lines") or []
    want = sub.get("lines") or []
    lines_ok = len(lines) == len(want) and all(
        normalize.amounts_equal(str(g.get("amount_ex_tax")), w["line_total"])
        for g, w in zip(lines, want, strict=False)
    )
    return out | {
        "correct": correct,
        "lines_correct": lines_ok,
        "all_correct": all(v is not False for v in correct.values())
        and lines_ok,
        "wrong": [k for k, v in correct.items() if v is False]
        + ([] if lines_ok else ["lines"]),
        "got": got,
        "source": "vision" if vision_pages else "pdf_text",
        "retried": bool(first),
        "problems_first": first,
        "problems_final": final,
        "checks_pass": not final,
        "cost_usd": round(hf_cost + vision_pages * gia.VISION_USD_PER_PAGE, 6),
        "seconds": round(time.time() - t0, 1),
    }


def report(model: str, results: list[dict], pro: dict[str, dict]) -> str:
    n = len(results)
    ok = [r for r in results if "error" not in r]
    costs = [r["cost_usd"] for r in ok if r.get("cost_usd") is not None]
    secs = sorted(r["seconds"] for r in ok) or [0]
    pro_all = [
        pro[r["invoice_id"]].get("header_and_totals_correct") == "True"
        and pro[r["invoice_id"]].get("line_count") == "True"
        for r in ok
        if r["invoice_id"] in pro
    ]
    L = [
        f"# {model} on {n} invoices\n",
        "| | |",
        "|---|---|",
        f"| Ran without error | {len(ok)} / {n} |",
        f"| Every field and line correct | {sum(r['all_correct'] for r in ok)} / {len(ok)} (Pro: {sum(pro_all)} / {len(pro_all)}) |",
        f"| Retried after a failed check | {sum(r['retried'] for r in ok)} |",
        f"| **Pass every check after retry (would skip Pro)** | **{sum(r['checks_pass'] for r in ok)} / {n}** |",
        f"| Passed the checks but a field is wrong (unsafe) | **{sum(r['checks_pass'] and not r['all_correct'] for r in ok)}** |",
        f"| Failed the checks (would go to Pro) | {sum(not r['checks_pass'] for r in ok)} |",
        f"| Cost per invoice (HF + Vision) | ${sum(costs) / max(len(costs), 1):.5f} |",
        f"| Median seconds | {secs[len(secs) // 2]:.1f} |",
        "",
        "| Field | This model | Pro (prod run 2026-11-05) |",
        "|---|---|---|",
    ]
    for f in [*FIELDS, "lines"]:
        mine = [
            r["correct"][f] if f != "lines" else r["lines_correct"] for r in ok
        ]
        mine = [m for m in mine if m is not None]
        theirs = [
            pro[r["invoice_id"]].get(f if f != "lines" else "line_count")
            for r in ok
            if r["invoice_id"] in pro
        ]
        theirs = [t == "True" for t in theirs if t in ("True", "False")]
        L.append(
            f"| {f} | {sum(mine)}/{len(mine)} | {sum(theirs)}/{len(theirs)} |"
        )
    errs = [r for r in results if "error" in r]
    if errs:
        L += ["", "## Errors\n"] + [
            f"- {r['invoice_id']}: {r['error']}" for r in errs
        ]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="thinkingmachines/Inkling-2-Mini")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument(
        "--ids", help="comma-separated invoice ids instead of a sample"
    )
    ap.add_argument("--out", default="benchmark/runs/hf-model")
    ap.add_argument(
        "--pro", default="benchmark/runs/prod-2026-11-05/extraction.csv"
    )
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument(
        "--scale", type=float, default=1.5, help="render scale (1 = 72 dpi)"
    )
    args = ap.parse_args(argv)
    token = os.environ["HF_TOKEN"]
    gt = dataset.ground_truth()
    pool = sorted(k for k, r in gt.items() if r["record_kind"] != "NONDOC")
    ids = (
        args.ids.split(",")
        if args.ids
        else sorted(random.Random(args.seed).sample(pool, args.n))  # noqa: S311
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = out / "results.jsonl"  # one line per finished invoice; rerun resumes
    done = {}
    if log.exists():
        for line in log.read_text().splitlines():
            r = json.loads(line)
            if "error" not in r:
                done[r["invoice_id"]] = r
    todo = [i for i in ids if i not in done]
    ex = ThreadPoolExecutor(args.workers)
    futs = {
        ex.submit(run_one, gt[i], args.model, token, args.scale): i
        for i in todo
    }
    with log.open("a") as f:
        for n, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            done[r["invoice_id"]] = r
            f.write(json.dumps(r, default=str) + "\n")
            f.flush()
            print(
                f"{n}/{len(todo)} {r['invoice_id']} {r.get('error') or r['wrong']}",
                flush=True,
            )
    ex.shutdown(wait=False)
    results = [done[i] for i in ids]
    pro = {}
    if Path(args.pro).exists():
        pro = {r["invoice_id"]: r for r in csv.DictReader(open(args.pro))}
    (out / "results.json").write_text(
        json.dumps(results, indent=1, default=str)
    )
    md = report(args.model, results, pro)
    (out / "report.md").write_text(md)
    print(md, flush=True)
    os._exit(0)  # don't wait on a thread abandoned by _post_with_deadline


if __name__ == "__main__":
    main()
