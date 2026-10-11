"""Renders submissions to PDF: 7 invoice layouts, non-invoice documents, scan/adversarial degradation, multi-invoice merges."""
import os, io, copy, datetime as dt, math
from decimal import Decimal
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, PageBreak, KeepTogether
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pypdf import PdfReader, PdfWriter
from pdf2image import convert_from_path
from PIL import Image, ImageFilter, ImageEnhance, ImageDraw, ImageFont
import numpy as np
import gen_core as g
from gen_core import D, COMPANIES, VBY, STATES

FONTDIR = os.environ.get("DEJAVU_FONTDIR", "/usr/share/fonts/truetype/dejavu/")
pdfmetrics.registerFont(TTFont("DV", FONTDIR+"DejaVuSans.ttf")); pdfmetrics.registerFont(TTFont("DV-B", FONTDIR+"DejaVuSans-Bold.ttf"))
pdfmetrics.registerFont(TTFont("DVC", FONTDIR+"DejaVuSansCondensed.ttf")); pdfmetrics.registerFont(TTFont("DVC-B", FONTDIR+"DejaVuSansCondensed-Bold.ttf"))
pdfmetrics.registerFont(TTFont("DVS", FONTDIR+"DejaVuSerif.ttf")) if os.path.exists(FONTDIR+"DejaVuSerif.ttf") else None
pdfmetrics.registerFont(TTFont("DVS-B", FONTDIR+"DejaVuSerif-Bold.ttf")) if os.path.exists(FONTDIR+"DejaVuSerif-Bold.ttf") else None
pdfmetrics.registerFont(TTFont("DVM", FONTDIR+"DejaVuSansMono.ttf")) if os.path.exists(FONTDIR+"DejaVuSansMono.ttf") else None

LAYOUTS = {
 "L1": dict(font=("DV","DV-B"), size=8.5, title="TAX INVOICE", accent=colors.HexColor("#1F3A5F"), grid=True, per_line_tax=True, cur="₹", datef="%d-%b-%Y", header="left", page=A4, words=True, numfmt="in"),
 "L2": dict(font=("DVC","DVC-B"), size=8, title="Tax Invoice", accent=colors.HexColor("#8B0000"), grid=False, per_line_tax=False, cur="Rs.", datef="%d/%m/%Y", header="right", page=A4, words=True, numfmt="in"),
 "L3": dict(font=("DVS","DVS-B") if os.path.exists(FONTDIR+"DejaVuSerif.ttf") else ("DV","DV-B"), size=9, title="TAX INVOICE", accent=colors.HexColor("#2E5E4E"), grid=True, per_line_tax=False, cur="INR", datef="%Y-%m-%d", header="center", page=LETTER, words=False, numfmt="intl"),
 "L4": dict(font=("DVM","DVM") if os.path.exists(FONTDIR+"DejaVuSansMono.ttf") else ("DV","DV-B"), size=8, title="GST INVOICE", accent=colors.black, grid=True, per_line_tax=True, cur="Rs", datef="%d.%m.%Y", header="left", page=A4, words=True, numfmt="in"),
 "L5": dict(font=("DV","DV-B"), size=8, title="TAX INVOICE", accent=colors.HexColor("#4A4A4A"), grid=False, per_line_tax=False, cur="₹", datef="%d %B %Y", header="band", page=A4, words=True, numfmt="in"),
 "L6": dict(font=("DVC","DVC-B"), size=8.5, title="Tax Invoice / Bill", accent=colors.HexColor("#0B6E4F"), grid=True, per_line_tax=True, cur="₹", datef="%d-%m-%Y", header="right", page=A4, words=False, numfmt="in"),
 "L7": dict(font=("DV","DV-B"), size=7.5, title="TAX INVOICE", accent=colors.HexColor("#3B2F63"), grid=True, per_line_tax=False, cur="INR", datef="%b %d, %Y", header="band", page=LETTER, words=True, numfmt="intl"),
}

def fmt_amt(x, L):
    d = D(x); neg = d < 0; d = abs(d)
    s = f"{d:.2f}"; ip, fp = s.split(".")
    if L["numfmt"]=="in" and len(ip) > 3:
        head, tail = ip[:-3], ip[-3:]
        parts = []
        while len(head) > 2: parts.insert(0, head[-2:]); head = head[:-2]
        if head: parts.insert(0, head)
        ip = ",".join(parts) + "," + tail
    elif L["numfmt"]=="intl":
        ip = f"{int(ip):,}"
    out = f"{ip}.{fp}"
    cur = L["cur"]
    return ("-" if neg else "") + (f"{cur} {out}" if cur in ("Rs.","INR","Rs") else f"{cur}{out}")

def fmt_date(iso, L):
    return dt.date.fromisoformat(iso).strftime(L["datef"])

ONES = ["","One","Two","Three","Four","Five","Six","Seven","Eight","Nine","Ten","Eleven","Twelve","Thirteen","Fourteen","Fifteen","Sixteen","Seventeen","Eighteen","Nineteen"]
TENS = ["","","Twenty","Thirty","Forty","Fifty","Sixty","Seventy","Eighty","Ninety"]
def _w99(n): return ONES[n] if n < 20 else TENS[n//10] + (" " + ONES[n%10] if n%10 else "")
def _w999(n): return (ONES[n//100] + " Hundred" + (" " + _w99(n%100) if n%100 else "")) if n >= 100 else _w99(n)
def words_inr(x):
    d = D(x); rupees = int(d); paise = int((d - rupees) * 100)
    parts = []
    crore, rupees = divmod(rupees, 10**7); lakh, rupees = divmod(rupees, 10**5); thou, hund = divmod(rupees, 1000)
    if crore: parts.append(_w999(crore) + " Crore")
    if lakh: parts.append(_w99(lakh) + " Lakh")
    if thou: parts.append(_w99(thou) + " Thousand")
    if hund: parts.append(_w999(hund))
    s = "Rupees " + (" ".join(parts) if parts else "Zero")
    if paise: s += f" and {_w99(paise)} Paise"
    return s + " Only"

def styles(L):
    f, fb = L["font"]; sz = L["size"]
    return dict(
        n=ParagraphStyle("n", fontName=f, fontSize=sz, leading=sz*1.25),
        s=ParagraphStyle("s", fontName=f, fontSize=sz-1, leading=(sz-1)*1.25, textColor=colors.HexColor("#333333")),
        b=ParagraphStyle("b", fontName=fb, fontSize=sz, leading=sz*1.25),
        h=ParagraphStyle("h", fontName=fb, fontSize=sz+7, leading=(sz+7)*1.2, textColor=L["accent"]),
        t=ParagraphStyle("t", fontName=fb, fontSize=sz+5, leading=(sz+5)*1.2, textColor=L["accent"], alignment=1),
        r=ParagraphStyle("r", fontName=f, fontSize=sz, leading=sz*1.25, alignment=2),
        rb=ParagraphStyle("rb", fontName=fb, fontSize=sz, leading=sz*1.25, alignment=2),
        small=ParagraphStyle("small", fontName=f, fontSize=sz-1.5, leading=(sz-1.5)*1.2, textColor=colors.HexColor("#555555")),
    )

def logo_cell(v, L, S):
    initials = "".join(w[0] for w in v["name"].split()[:2]).upper()
    t = Table([[Paragraph(f"<font color='white'><b>{initials}</b></font>", S["h"])]], colWidths=[16*mm], rowHeights=[12*mm])
    t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),L["accent"]),("ALIGN",(0,0),(-1,-1),"CENTER"),("VALIGN",(0,0),(-1,-1),"MIDDLE")]))
    return t

def vendor_block(v, sub, L, S, show_gstin=True):
    lines = [f"<b>{v['name']}</b>", f"{v['address']}, {v['city']} {v['pin']}, {v['state_name']}"]
    if show_gstin and sub["vendor_gstin"]: lines.append(f"GSTIN: {sub['vendor_gstin']}")
    if show_gstin: lines.append(f"PAN: {v['pan']}")
    lines.append(f"{v['email']} | {v['phone']}")
    return Paragraph("<br/>".join(lines), S["n"])

def header(v, sub, L, S, title, show_gstin=True):
    vb = vendor_block(v, sub, L, S, show_gstin); lg = logo_cell(v, L, S); tt = Paragraph(title, S["t"])
    W = L["page"][0] - 30*mm
    if L["header"]=="left": data = [[lg, vb, tt]]; cw = [18*mm, W*0.52, W-18*mm-W*0.52]
    elif L["header"]=="right": data = [[tt, vb, lg]]; cw = [W*0.3, W*0.55, 18*mm]
    elif L["header"]=="center": data = [[vb, tt, lg]]; cw = [W*0.45, W*0.35, 18*mm]
    else:
        band = Table([[lg, Paragraph(f"<font color='white'><b>{v['name']}</b></font>", S["h"])]], colWidths=[18*mm, W-18*mm])
        band.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),L["accent"]),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("LEFTPADDING",(0,0),(-1,-1),6)]))
        data = [[band],[Table([[vb, tt]], colWidths=[W*0.6, W*0.4])]]; cw = [W]
    t = Table(data, colWidths=cw); t.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP")])); return t

def meta_table(sub, v, L, S, extra_rows=None):
    rows = [["Invoice No.", sub["invoice_number"], "Invoice Date", fmt_date(sub["invoice_date"], L)],
            ["Due Date", fmt_date(sub["due_date"], L), "Payment Terms", sub["payment_terms"]]]
    if sub["po_number"]: rows.append(["PO Reference", sub["po_number"], "Place of Supply", sub["place_of_supply"]])
    else: rows.append(["Place of Supply", sub["place_of_supply"], "Supply Type", "Intra-state" if sub["supply_type"]=="INTRA" else "Inter-state"])
    if sub["work_authorization_id"] or v["kind"]=="service":
        rows.append(["Work Authorization", sub["work_authorization_id"] or "—", "Project", sub.get("project") or "—"])
        if sub.get("service_period"): a, b = sub["service_period"].split(" to "); rows.append(["Service Period", f"{fmt_date(a,L)} to {fmt_date(b,L)}", "Reverse Charge", "No"])
    else: rows.append(["Reverse Charge", "No", "Vehicle / LR No.", f"GJ{g.rng.randint(10,25)}-{g.rng.choice('ABCT')}{g.rng.randint(1000,9999)}" if v["company_id"]=="B" else "—"])
    for r in (extra_rows or []): rows.append(r)
    t = Table([[Paragraph(f"<b>{a}</b>", S["s"]), Paragraph(str(b), S["n"]), Paragraph(f"<b>{c}</b>", S["s"]), Paragraph(str(d), S["n"])] for a,b,c,d in rows], colWidths=[28*mm, 50*mm, 30*mm, 52*mm])
    t.setStyle(TableStyle([("GRID" if L["grid"] else "LINEBELOW",(0,0),(-1,-1),0.4,colors.HexColor("#999999")),("VALIGN",(0,0),(-1,-1),"TOP"),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#F2F2F2")),("BACKGROUND",(2,0),(2,-1),colors.HexColor("#F2F2F2"))]))
    return t

def party_table(sub, comp_addr, L, S):
    bill = Paragraph(f"<b>Bill To</b><br/>{sub['customer_name']}<br/>{sub['customer_address']}<br/>GSTIN: {sub['customer_gstin']}", S["n"])
    ship = Paragraph(f"<b>Ship To / Service Location</b><br/>{sub['customer_name']}<br/>{sub['customer_address']}", S["n"])
    t = Table([[bill, ship]], colWidths=[80*mm, 80*mm])
    t.setStyle(TableStyle([("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#888888")),("LINEAFTER",(0,0),(0,-1),0.5,colors.HexColor("#888888")),("VALIGN",(0,0),(-1,-1),"TOP"),("PADDING",(0,0),(-1,-1),5)]))
    return t

def lines_table(sub, L, S, show_tax=True, show_hsn=True):
    intra = sub["supply_type"]=="INTRA"
    hdr = ["#", "Description", "HSN/SAC", "Qty", "Unit", "Rate", "Disc.", "Taxable Amt"]
    if L["per_line_tax"] and show_tax: hdr += ["GST %"] + (["CGST","SGST"] if intra else ["IGST"])
    elif show_tax: hdr += ["GST %"]
    if not show_hsn: hdr.remove("HSN/SAC")
    rows = [hdr]
    for i, l in enumerate(sub["lines"], 1):
        qty = f"{l['quantity']:g}" if isinstance(l["quantity"], float) else str(l["quantity"])
        r = [str(i), Paragraph(l["description"], S["n"]), l["hsn_sac"], qty, l["unit"], fmt_amt(l["unit_price"], L).replace(L["cur"],"").strip(), (f"{l['discount_pct']}%" if l["discount_pct"] else "-"), fmt_amt(l["line_total"], L).replace(L["cur"],"").strip()]
        if show_tax:
            r.append(f"{l['gst_rate']}%")
            if L["per_line_tax"]:
                tax = D(D(l["line_total"]) * l["gst_rate"] / 100)
                r += ([fmt_amt(tax/2, L).replace(L["cur"],"").strip(), fmt_amt(tax - D(tax/2), L).replace(L["cur"],"").strip()] if intra else [fmt_amt(tax, L).replace(L["cur"],"").strip()])
        if not show_hsn: r.pop(2)
        rows.append(r)
    for o in sub["other_charge_lines"]:
        r = ["", Paragraph(o["description"], S["n"]), "9965" if "Freight" in o["description"] else "9985", "1", "lot", fmt_amt(o["amount"], L).replace(L["cur"],"").strip(), "-", fmt_amt(o["amount"], L).replace(L["cur"],"").strip()]
        if show_tax:
            r.append(f"{o['gst_rate']}%")
            if L["per_line_tax"]:
                tax = D(D(o["amount"]) * o["gst_rate"] / 100)
                r += ([fmt_amt(tax/2, L).replace(L["cur"],"").strip(), fmt_amt(tax - D(tax/2), L).replace(L["cur"],"").strip()] if intra else [fmt_amt(tax, L).replace(L["cur"],"").strip()])
        if not show_hsn: r.pop(2)
        rows.append(r)
    W = L["page"][0] - 30*mm
    ncol = len(hdr); fixed = {"#":8*mm, "HSN/SAC":16*mm, "Qty":13*mm, "Unit":12*mm, "Rate":20*mm, "Disc.":12*mm, "Taxable Amt":24*mm, "GST %":13*mm, "CGST":20*mm, "SGST":20*mm, "IGST":22*mm}
    cw = [fixed.get(h, 0) for h in hdr]; cw[hdr.index("Description")] = W - sum(cw)
    t = Table(rows, colWidths=cw, repeatRows=1)
    st = [("FONT",(0,0),(-1,0),L["font"][1],L["size"]),("FONT",(0,1),(-1,-1),L["font"][0],L["size"]-0.5),("BACKGROUND",(0,0),(-1,0),L["accent"]),("TEXTCOLOR",(0,0),(-1,0),colors.white),
          ("ALIGN",(3,1),(-1,-1),"RIGHT"),("VALIGN",(0,0),(-1,-1),"TOP")]
    st += [("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#777777"))] if L["grid"] else [("LINEBELOW",(0,0),(-1,-1),0.3,colors.HexColor("#BBBBBB"))]
    if not L["grid"]:
        for i in range(1, len(rows)):
            if i % 2 == 0: st.append(("BACKGROUND",(0,i),(-1,i),colors.HexColor("#F7F7F7")))
    t.setStyle(TableStyle(st)); return t

def summary_table(sub, L, S, show_tax=True):
    intra = sub["supply_type"]=="INTRA"
    rows = [["Sub Total", fmt_amt(sub["subtotal"], L)]]
    if D(sub["discount"]) != 0: rows += [["Less: Discount", "- " + fmt_amt(sub["discount"], L)], ["Taxable Value", fmt_amt(sub["taxable_amount"], L)]]
    if D(sub["other_charges"]) != 0: rows.append(["Other Charges", fmt_amt(sub["other_charges"], L)])
    if show_tax:
        if intra: rows += [["CGST", fmt_amt(sub["cgst"], L)], ["SGST", fmt_amt(sub["sgst"], L)]]
        else: rows += [["IGST", fmt_amt(sub["igst"], L)]]
        if len(sub.get("tax_by_rate", {})) > 1 or L["per_line_tax"] is False: rows.append(["Total Tax", fmt_amt(sub["tax_amount"], L)])
    if D(sub["round_off"]) != 0: rows.append(["Round Off", fmt_amt(sub["round_off"], L)])
    rows.append(["GRAND TOTAL", fmt_amt(sub["grand_total"], L)])
    t = Table([[Paragraph(a, S["r"] if a!="GRAND TOTAL" else S["rb"]), Paragraph(b, S["r"] if a!="GRAND TOTAL" else S["rb"])] for a, b in rows], colWidths=[45*mm, 40*mm], hAlign="RIGHT")
    st = [("LINEABOVE",(0,-1),(-1,-1),0.8,L["accent"]),("BACKGROUND",(0,-1),(-1,-1),colors.HexColor("#EFEFEF"))]
    if L["grid"]: st.append(("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#AAAAAA")))
    t.setStyle(TableStyle(st)); return t

def footer_block(v, sub, L, S, show_tax=True):
    bank = Paragraph(f"<b>Bank Details</b><br/>Beneficiary: {v['name']}<br/>Bank: {v['bank']}<br/>A/c No.: {v['account']}<br/>IFSC: {v['ifsc']}<br/>Branch: {v['city']} Main", S["s"])
    decl = ("We declare that this invoice shows the actual price of the goods/services described and that all particulars are true and correct." if show_tax else "This document is issued without GST. Please refer to the underlying tax invoice for input tax credit.") + f"<br/><br/><b>For {v['name']}</b><br/><br/><br/>Authorised Signatory"
    t = Table([[bank, Paragraph(decl, S["s"])]], colWidths=[75*mm, 85*mm])
    t.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("LINEABOVE",(0,0),(-1,0),0.4,colors.HexColor("#999999"))]))
    return t

TERMS = ["Payment is due within the credit period stated above. Interest @ 18% p.a. will be charged on overdue amounts.",
         "Any discrepancy in this invoice must be notified in writing within 7 days of receipt.",
         "Goods once sold will not be taken back. Services are deemed accepted unless disputed within the notification period.",
         "TDS, if applicable, may be deducted as per the Income-tax Act; kindly share Form 16A within the statutory time.",
         "Subject to the jurisdiction of courts at the supplier's registered office.",
         "This is a computer-generated document. E. & O.E."]

def annexure(sub, v, L, S):
    hdr = ["#", "Description", "Details"]
    rows = [hdr]
    for i, l in enumerate(sub["lines"], 1):
        det = (f"Resource timesheet approved by client SPOC; {l['quantity']} hours logged across {g.rng.randint(2,5)} weeks" if v["kind"]=="service" else f"Batch {g.rng.randint(1000,9999)}; delivered via {g.rng.choice(['road','courier','own vehicle'])}; inspected {g.rng.choice(['OK','OK','minor scratches noted'])}")
        rows.append([str(i), Paragraph(l["description"], S["n"]), Paragraph(det, S["s"])])
    t = Table(rows, colWidths=[8*mm, 80*mm, 72*mm], repeatRows=1)
    t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#888888")),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#DDDDDD")),("FONT",(0,0),(-1,0),L["font"][1],L["size"])]))
    return t

def render_invoice_pdf(rec, path, layout_id=None, seed_variation=0):
    """Render the *submitted* invoice content of a record (or synthetic extra) to PDF."""
    sub = rec["sub"]; v = rec["vendor"]; comp = COMPANIES[rec["company_id"]]
    L = LAYOUTS[layout_id or sub.get("layout") or v["layout"]]; S = styles(L)
    tc = sub.get("tax_compliance_variant"); it = sub.get("itemization_variant")
    show_tax = tc not in ("NO_TAX_BREAKUP","BILL_OF_SUPPLY","TITLE_INVOICE_NO_TAX_FIELDS")
    show_gstin = tc not in ("MISSING_GSTIN","TITLE_INVOICE_NO_TAX_FIELDS")
    title = sub.get("invoice_title", L["title"]) if tc else L["title"]
    if sub.get("document_type") == "TAX_INVOICE" and not tc: title = L["title"]
    doc = SimpleDocTemplate(path, pagesize=L["page"], leftMargin=15*mm, rightMargin=15*mm, topMargin=12*mm, bottomMargin=12*mm, title=f"{title} {sub['invoice_number']}", author=v["name"])
    story = [header(v, sub, L, S, title, show_gstin), Spacer(1, 4*mm)]
    extra = []
    if rec["difficulty"]=="ADVERSARIAL":
        extra.append(["Quotation Ref.", f"Q-{g.rng.randint(1000,9999)} dated {fmt_date((dt.date.fromisoformat(sub['invoice_date'])-dt.timedelta(days=g.rng.randint(15,60))).isoformat(), L)}", "Delivery/Completion Date", fmt_date((dt.date.fromisoformat(sub['invoice_date'])-dt.timedelta(days=g.rng.randint(1,6))).isoformat(), L)])
        if v["kind"]=="goods": extra.append(["E-Way Bill No.", f"{g.rng.randint(10**11, 10**12-1)}", "Order Ref. (yours)", f"{comp['code']}-REQ-{g.rng.randint(100,999)}"])
    story += [meta_table(sub, v, L, S, extra), Spacer(1, 3*mm), party_table(sub, sub["customer_address"], L, S), Spacer(1, 4*mm)]
    if it == "NO_TABLE":
        story.append(Paragraph("Charges for the period as per engagement letter / attached statement.", S["n"])); story.append(Spacer(1, 3*mm))
        story.append(summary_table(sub, L, S, show_tax))
    elif it == "TOTAL_ONLY":
        story.append(Spacer(1, 10*mm)); story.append(Paragraph(f"<b>Amount payable: {fmt_amt(sub['grand_total'], L)}</b>", S["h"]))
        story.append(Paragraph("Inclusive of all applicable taxes.", S["s"]))
    else:
        story.append(lines_table(sub, L, S, show_tax=show_tax, show_hsn=show_gstin and show_tax)); story.append(Spacer(1, 3*mm)); story.append(summary_table(sub, L, S, show_tax))
    if L["words"] and it != "TOTAL_ONLY": story.append(Paragraph(f"<b>Amount in words:</b> {words_inr(sub['grand_total'])}", S["n"]))
    if rec["difficulty"]=="ADVERSARIAL" and show_tax and it is None:
        story.append(Paragraph(f"Total before tax: {fmt_amt(D(sub['taxable_amount'])+D(sub['other_charges']), L)} &nbsp;&nbsp; Total tax: {fmt_amt(sub['tax_amount'], L)} &nbsp;&nbsp; Net payable: {fmt_amt(sub['grand_total'], L)}", S["s"]))
    story += [Spacer(1, 5*mm), footer_block(v, sub, L, S, show_tax)]
    tp = rec.get("target_pages", 1)
    if tp >= 2:
        story.append(PageBreak()); story.append(Paragraph("<b>Terms &amp; Conditions</b>", S["b"])); story.append(Spacer(1, 2*mm))
        for i, t in enumerate(TERMS, 1): story.append(Paragraph(f"{i}. {t}", S["n"]))
        story.append(Spacer(1, 4*mm)); story.append(Paragraph(f"Invoice {sub['invoice_number']} — page 2", S["small"]))
    if tp >= 3 and it is None:
        story.append(PageBreak()); story.append(Paragraph(f"<b>Annexure A — Line item details for invoice {sub['invoice_number']}</b>", S["b"])); story.append(Spacer(1, 2*mm)); story.append(annexure(sub, v, L, S))
    doc.build(story)
    return path

# ---------------------------------------------------------------- non-invoice documents
def render_nondoc(rec, path):
    t = rec["nondoc_type"]; sub = rec["sub"]; v = rec["vendor"]; comp = COMPANIES[rec["company_id"]]
    L = LAYOUTS[v["layout"]]; S = styles(L)
    if t == "UNREADABLE":
        render_invoice_pdf(rec, path); return path
    titles = {"RECEIPT":"PAYMENT RECEIPT","QUOTATION":"QUOTATION","PROFORMA_INVOICE":"PROFORMA INVOICE","CREDIT_NOTE":"CREDIT NOTE","PURCHASE_ORDER":"PURCHASE ORDER","DELIVERY_CHALLAN":"DELIVERY CHALLAN"}
    doc = SimpleDocTemplate(path, pagesize=L["page"], leftMargin=15*mm, rightMargin=15*mm, topMargin=12*mm, bottomMargin=12*mm, title=titles[t])
    story = []
    if t == "PURCHASE_ORDER":
        # issued by the company to the vendor
        po = g.POBY[sub["po_number"]] if sub["po_number"] else g.rng.choice([p for p in g.PO_MASTER if p["vendor_id"]==v["vendor_id"]])
        story.append(Paragraph(f"<b>{comp['legal_name']}</b><br/>{comp['address']}, {comp['city']} {comp['pin']}<br/>GSTIN: {comp['gstin']}", S["n"])); story.append(Spacer(1,3*mm))
        story.append(Paragraph("PURCHASE ORDER", S["t"])); story.append(Spacer(1,3*mm))
        story.append(Table([[Paragraph(f"<b>PO No.</b> {po['po_number']}<br/><b>PO Date</b> {fmt_date(po['valid_from'], L)}<br/><b>Valid Till</b> {fmt_date(po['valid_to'], L)}", S["n"]), Paragraph(f"<b>Supplier</b><br/>{v['name']}<br/>{v['address']}, {v['city']}<br/>GSTIN: {v['gstin']}", S["n"])]], colWidths=[80*mm,80*mm]))
        rows = [["#","Item","HSN/SAC","Qty","Unit","Rate","Amount"],["1", Paragraph(po["item_description"], S["n"]), po["hsn_sac"], str(po["approved_quantity"]), po["unit"], fmt_amt(po["approved_unit_price"], L), fmt_amt(po["approved_value"], L)]]
        tb = Table(rows, colWidths=[8*mm,60*mm,18*mm,14*mm,14*mm,26*mm,30*mm]); tb.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.3,colors.grey),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#DDDDDD"))]))
        story += [Spacer(1,3*mm), tb, Spacer(1,3*mm), Paragraph("Terms: Prices firm for validity period. Deliver as per schedule. Invoice must quote this PO number.", S["s"]), Spacer(1,8*mm), Paragraph(f"Authorised by: {g.rng.choice(g.NAMES)}, Procurement", S["n"])]
    else:
        story.append(header(v, sub, L, S, titles[t], show_gstin=(t in ("PROFORMA_INVOICE","CREDIT_NOTE","QUOTATION")))); story.append(Spacer(1,4*mm))
        if t == "RECEIPT":
            story.append(Paragraph(f"Receipt No.: RCP-{g.rng.randint(1000,9999)} &nbsp;&nbsp; Date: {fmt_date(sub['invoice_date'], L)}", S["n"]))
            story.append(Paragraph(f"Received with thanks from <b>{sub['customer_name']}</b> the sum of <b>{fmt_amt(sub['grand_total'], L)}</b> ({words_inr(sub['grand_total'])}) by {g.rng.choice(['NEFT','RTGS','cheque no. '+str(g.rng.randint(100000,999999))])} towards settlement of invoice {sub['invoice_number']}.", S["n"]))
            story.append(Spacer(1,10*mm)); story.append(Paragraph("This receipt is not a tax invoice.", S["s"]))
        elif t == "QUOTATION":
            story.append(Paragraph(f"Quotation No.: QT-{g.rng.randint(2000,9999)} &nbsp;&nbsp; Date: {fmt_date(sub['invoice_date'], L)} &nbsp;&nbsp; Valid for 30 days", S["n"]))
            story.append(Paragraph(f"To: {sub['customer_name']}, {sub['customer_address']}", S["n"])); story.append(Spacer(1,3*mm))
            story.append(lines_table(sub, L, S, show_tax=True)); story.append(Spacer(1,3*mm)); story.append(summary_table(sub, L, S, True))
            story.append(Paragraph("Prices are indicative and subject to confirmation. GST extra as applicable. This is not a tax invoice.", S["s"]))
        elif t == "PROFORMA_INVOICE":
            story.append(meta_table(sub, v, L, S)); story.append(Spacer(1,3*mm)); story.append(party_table(sub, "", L, S)); story.append(Spacer(1,3*mm))
            story.append(lines_table(sub, L, S, show_tax=True)); story.append(Spacer(1,3*mm)); story.append(summary_table(sub, L, S, True))
            story.append(Paragraph("<b>PROFORMA — for advance payment only. Not valid for input tax credit. Tax invoice will follow on supply.</b>", S["n"]))
        elif t == "CREDIT_NOTE":
            story.append(Paragraph(f"Credit Note No.: CN-{g.rng.randint(100,999)}/2026-27 &nbsp;&nbsp; Date: {fmt_date(sub['invoice_date'], L)} &nbsp;&nbsp; Against Invoice: {sub['invoice_number']}", S["n"]))
            story.append(party_table(sub, "", L, S)); story.append(Spacer(1,3*mm))
            cn = copy.deepcopy(sub); cn["lines"] = cn["lines"][:1]
            for l in cn["lines"]: l["quantity"] = max(1, int(l["quantity"]*0.2)); g._recalc_line(l)
            cn["other_charge_lines"] = []; g._recalc_totals(cn, sub)
            story.append(lines_table(cn, L, S, show_tax=True)); story.append(Spacer(1,3*mm)); story.append(summary_table(cn, L, S, True))
            story.append(Paragraph("Reason: short supply / rate difference. Amount credited to your account.", S["s"]))
        elif t == "DELIVERY_CHALLAN":
            story.append(Paragraph(f"Challan No.: DC-{g.rng.randint(1000,9999)} &nbsp;&nbsp; Date: {fmt_date(sub['invoice_date'], L)} &nbsp;&nbsp; Vehicle: {g.rng.choice(['GJ','KA','MH'])}{g.rng.randint(10,50)}-{g.rng.choice('ABCT')}{g.rng.randint(1000,9999)}", S["n"]))
            story.append(Paragraph(f"Consignee: {sub['customer_name']}, {sub['customer_address']}", S["n"])); story.append(Spacer(1,3*mm))
            rows = [["#","Description","HSN","Qty","Unit","Packages"]] + [[str(i), Paragraph(l["description"], S["n"]), l["hsn_sac"], str(l["quantity"]), l["unit"], str(g.rng.randint(1,12))] for i, l in enumerate(sub["lines"],1)]
            tb = Table(rows, colWidths=[8*mm,80*mm,18*mm,16*mm,16*mm,22*mm]); tb.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.3,colors.grey),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#DDDDDD"))])); story.append(tb)
            story.append(Spacer(1,4*mm)); story.append(Paragraph("Goods dispatched for delivery. Not for sale — value not indicated. Received in good condition: ______________", S["s"]))
    doc.build(story); return path

def render_grn(rec, path):
    """The buyer's goods receipt note against a PO: rec = company_id, vendor_id, grn_number,
    received_on (ISO), po_number, received_by, lines [{description, hsn_sac, ordered, received, unit}].
    Scan it with degrade(); adversarial=True adds the RECEIVED stamp."""
    v = VBY[rec["vendor_id"]]; comp = COMPANIES[rec["company_id"]]; L = LAYOUTS[v["layout"]]; S = styles(L)
    doc = SimpleDocTemplate(path, pagesize=L["page"], leftMargin=15*mm, rightMargin=15*mm, topMargin=12*mm, bottomMargin=12*mm, title="GOODS RECEIPT NOTE")
    story = [Paragraph(f"<b>{comp['legal_name']}</b><br/>{comp['address']}, {comp['city']} {comp['pin']}<br/>GSTIN: {comp['gstin']}", S["n"]), Spacer(1,3*mm),
             Paragraph("GOODS RECEIPT NOTE", S["t"]), Spacer(1,3*mm),
             Table([[Paragraph(f"<b>GRN No.</b> {rec['grn_number']}<br/><b>Date</b> {fmt_date(rec['received_on'], L)}<br/><b>PO No.</b> {rec['po_number']}", S["n"]), Paragraph(f"<b>Supplier</b><br/>{v['name']}<br/>{v['address']}, {v['city']}<br/>GSTIN: {v['gstin']}", S["n"])]], colWidths=[80*mm,80*mm])]
    rows = [["#","Item","HSN/SAC","Ordered","Received","Unit"]] + [[str(i), Paragraph(l["description"], S["n"]), l["hsn_sac"], str(l["ordered"]), str(l["received"]), l["unit"]] for i, l in enumerate(rec["lines"], 1)]
    tb = Table(rows, colWidths=[8*mm,80*mm,20*mm,18*mm,18*mm,16*mm]); tb.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.3,colors.grey),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#DDDDDD"))]))
    story += [Spacer(1,3*mm), tb, Spacer(1,3*mm), Paragraph("Goods received subject to inspection. Shortages noted against the PO line.", S["s"]), Spacer(1,8*mm), Paragraph(f"Received by: {rec['received_by']}, Stores", S["n"])]
    doc.build(story); return path

# ---------------------------------------------------------------- degradation
def degrade(path, level, adversarial=False, unreadable=False, dpi=150):
    pages = convert_from_path(path, dpi=dpi)
    out = []
    for pg in pages:
        im = pg.convert("L")
        if unreadable:
            im = im.resize((im.width//6, im.height//6)).resize((im.width, im.height)).filter(ImageFilter.GaussianBlur(6))
            arr = np.array(im).astype(np.int16); arr += np.random.default_rng(1).integers(-70, 70, arr.shape); arr = np.clip(arr, 0, 255).astype(np.uint8); im = Image.fromarray(arr)
            im = ImageEnhance.Contrast(im).enhance(0.35)
        else:
            angle = g.rng.uniform(-1.4, 1.4) if level=="HARD" else g.rng.uniform(-2.2, 2.2)
            im = im.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
            im = im.filter(ImageFilter.GaussianBlur(0.6 if level=="HARD" else 0.9))
            arr = np.array(im).astype(np.int16); noise = np.random.default_rng(g.rng.randint(0, 10**6)).integers(-18 if level=="HARD" else -28, 18 if level=="HARD" else 28, arr.shape)
            arr = np.clip(arr + noise, 0, 255).astype(np.uint8); im = Image.fromarray(arr)
            im = ImageEnhance.Contrast(im).enhance(0.9 if level=="HARD" else 0.8); im = ImageEnhance.Brightness(im).enhance(1.03)
            if adversarial:
                dr = ImageDraw.Draw(im)
                try: f = ImageFont.truetype(FONTDIR+"DejaVuSans-Bold.ttf", 40); f2 = ImageFont.truetype(FONTDIR+"DejaVuSans.ttf", 22)
                except Exception: f = f2 = None
                x, y = im.width - 520, 120
                dr.rectangle([x, y, x+440, y+110], outline=90, width=4)
                dr.text((x+15, y+10), "RECEIVED", fill=70, font=f); dr.text((x+15, y+62), f"{g.rng.choice(['04/09/2026','05-09-2026','02.09.2026'])}  Accounts Dept.", fill=70, font=f2)
                if g.rng.random() < 0.5: dr.text((60, im.height-140), g.rng.choice(["Approved - pls process", "Chk PO qty", "Dup? see file", "Pay by 15th"]), fill=60, font=f2)
                if g.rng.random() < 0.6:
                    wm = Image.new("L", im.size, 255); wd = ImageDraw.Draw(wm); wd.text((im.width//2-200, im.height//2-40), g.rng.choice(["COPY","ORIGINAL FOR RECIPIENT","DUPLICATE FOR TRANSPORTER"]), fill=200, font=f)
                    im = Image.fromarray(np.minimum(np.array(im), np.array(wm.rotate(25, fillcolor=255))))
        out.append(im.convert("RGB"))
    out[0].save(path, save_all=True, append_images=out[1:], quality=55 if not unreadable else 30, resolution=dpi)

def merge_pdfs(paths, out):
    w = PdfWriter()
    for p in paths:
        for pg in PdfReader(p).pages: w.add_page(pg)
    with open(out, "wb") as fh: w.write(fh)

def set_metadata(path, meta):
    r = PdfReader(path); w = PdfWriter()
    for pg in r.pages: w.add_page(pg)
    w.add_metadata(meta)
    with open(path, "wb") as fh: w.write(fh)

def page_count(path): return len(PdfReader(path).pages)
