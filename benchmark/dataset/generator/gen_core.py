"""Synthetic invoice validation dataset - core data synthesis (masters, invoices, faults).
Everything is fictional. Deterministic via seed."""
import random, datetime as dt, copy, string
from decimal import Decimal, ROUND_HALF_UP

SEED = 20260903
rng = random.Random(SEED)
REF_DATE = dt.date(2026, 9, 3)

def D(x): return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

# ---------------------------------------------------------------- GSTIN utils
ALNUM = string.digits + string.ascii_uppercase
def gstin_checksum(s14):
    total = 0
    for i, ch in enumerate(s14):
        v = ALNUM.index(ch); f = 1 if i % 2 == 0 else 2
        p = v * f; total += p // 36 + p % 36
    return ALNUM[(36 - total % 36) % 36]
def make_gstin(state, pan, entity="1"):
    base = f"{state}{pan}{entity}Z"; return base + gstin_checksum(base)
def gstin_valid(g):
    return len(g) == 15 and g[13] == "Z" and gstin_checksum(g[:14]) == g[14] and g[:2].isdigit()
def rand_pan(kind):
    return "".join(rng.choice(string.ascii_uppercase) for _ in range(3)) + kind + rng.choice(string.ascii_uppercase) + f"{rng.randint(1000,9999)}" + rng.choice(string.ascii_uppercase)

STATES = {"03":"Punjab","06":"Haryana","07":"Delhi","08":"Rajasthan","09":"Uttar Pradesh","21":"Odisha","24":"Gujarat","27":"Maharashtra","29":"Karnataka","33":"Tamil Nadu","36":"Telangana"}

# ---------------------------------------------------------------- Companies
COMPANIES = {
 "A": dict(company_id="A", code="TECH", legal_name="Zentrix Cloudworks Private Limited", short="Zentrix Cloudworks",
           address="4th Floor, Prism Tech Park, Outer Ring Road, Bellandur", city="Bengaluru", pin="560103", state="29",
           pan="AAFCZ4821L", email="ap@zentrixcloudworks.example", industry="Technology / Software"),
 "B": dict(company_id="B", code="MFG", legal_name="Ferrovane Industries Private Limited", short="Ferrovane Industries",
           address="Plot 112, GIDC Industrial Estate Phase II, Makarpura", city="Vadodara", pin="390010", state="24",
           pan="AAECF9034M", email="accounts.payable@ferrovane.example", industry="Manufacturing / Industrial"),
 "C": dict(company_id="C", code="SVC", legal_name="Lumora Brandcraft LLP", short="Lumora Brandcraft",
           address="2nd Floor, Orbit Corporate Centre, Andheri East", city="Mumbai", pin="400059", state="27",
           pan="AAMFL2760R", email="finance@lumorabrandcraft.example", industry="Professional Services / Marketing"),
}
for c in COMPANIES.values():
    c["gstin"] = make_gstin(c["state"], c["pan"])

# ---------------------------------------------------------------- Vendors
# kind: goods | service ; items: (description, hsn_sac, unit, price_lo, price_hi, gst_rate)
VENDOR_SPECS = {
"A": [
 ("Cloudhaven Infra Services Pvt Ltd","Bengaluru","29","C","service",[("Managed cloud operations – {mon} 2026","998315","hour",1800,2600,18),("On-call SRE support – {mon} 2026","998315","hour",2200,3200,18)],"CH/{fy}/{n:04d}",1,"L1"),
 ("Nimbus Nine Software LLP","Pune","27","F","goods",[("Nimbus Suite Enterprise licence – annual, per seat","997331","seat",9500,14000,18),("Nimbus Analytics add-on – per seat","997331","seat",3200,4800,18),("Implementation & onboarding pack","998314","pack",85000,145000,18)],"NN-{yy}-{n:05d}",2,"L2"),
 ("Quantar DevOps Consulting Pvt Ltd","Hyderabad","36","C","service",[("DevOps engineering – Sprint {sp} ({mon} 2026)","998314","hour",2400,3400,18),("Kubernetes platform hardening – {mon} 2026","998314","hour",2800,3900,18),("Architecture review workshop","998314","hour",4500,6500,18)],"QDC/INV/{fy}/{n:03d}",3,"L3"),
 ("Britehall IT Staffing Solutions","Chennai","33","P","service",[("Contract engineer – {name} – {mon} 2026","998513","hour",1400,2100,18),("Contract QA analyst – {name} – {mon} 2026","998513","hour",1100,1700,18)],"BH{n:06d}",1,"L4"),
 ("Orchid Peak Datacenters Ltd","Mumbai","27","C","service",[("Colocation – Rack {rk}, 42U, {mon} 2026","998315","hour",950,1300,18),("Remote hands support – {mon} 2026","998315","hour",1500,2200,18)],"OPD/{fy}/{n:05d}",2,"L5"),
 ("Kestrel Cyber Assurance Pvt Ltd","Bengaluru","29","C","service",[("VAPT – external perimeter assessment","998319","hour",3500,5200,18),("SOC 2 readiness advisory – {mon} 2026","998319","hour",3800,5600,18)],"KCA-{yy}{mm}-{n:03d}",3,"L6"),
 ("Redwood Analytics Labs Pvt Ltd","Gurugram","06","C","service",[("Data engineering – pipeline build – {mon} 2026","998314","hour",2100,3000,18),("BI dashboard development – {mon} 2026","998314","hour",1900,2700,18)],"RAL/{n:04d}/{fy}",1,"L7"),
 ("Sapphire Systems Hardware Pvt Ltd","Bengaluru","29","C","goods",[("Rack server, 2U, dual Xeon, 256GB RAM","8471","nos",285000,410000,18),("Business laptop 14-inch, 16GB/512GB","8471","nos",68000,92000,18),("27-inch IPS monitor","8528","nos",14500,21000,18),("USB-C docking station","8471","nos",7800,11500,18),("Network switch 48-port PoE","8517","nos",92000,138000,18),("Cat6A patch cord 2m (pack of 10)","8544","pack",2400,3600,18)],"SSH/{fy}/{n:04d}",2,"L1"),
 ("Meridian SaaS Tools (India) Pvt Ltd","Noida","09","C","goods",[("Meridian Workspace Pro – monthly subscription, per user","998315","user",1250,1650,18),("Meridian Storage 1TB block – monthly","998315","block",4200,5600,18)],"MST-{yy}-{n:05d}",3,"L2"),
 ("Pixelforge UX Studio LLP","Bengaluru","29","F","service",[("Product UX design – {mon} 2026","998391","hour",1600,2400,18),("Design system components – {mon} 2026","998391","hour",1800,2600,18)],"PFX/{n:03d}/{fy}",1,"L3"),
],
"B": [
 ("Apex Industrial Components Pvt Ltd","Rajkot","24","C","goods",[("Deep groove ball bearing 6205-2RS","8482","nos",180,260,18),("Taper roller bearing 30208","8482","nos",520,760,18),("Spherical roller bearing 22212 EK","8482","nos",3900,5400,18),("Bearing housing SNL 512","8483","nos",2600,3800,18),("Lock nut KM 12","7318","nos",95,140,18)],"AIC/{fy}/{n:04d}",1,"L1"),
 ("Shakti Steel & Alloys Ltd","Ahmedabad","24","C","goods",[("HR coil IS 2062 E250, 3.0mm","7208","MT",54000,61000,18),("CR sheet 1.2mm, 1250 x 2500","7209","MT",62000,70000,18),("MS angle 50x50x6","7216","MT",52000,58000,18),("MS round bar 25mm","7214","MT",53000,59500,18)],"SSA-{yy}-{n:05d}",2,"L2"),
 ("Bharat Fasteners Corporation","Ludhiana","03","P","goods",[("Hex bolt M12x50, 8.8, zinc (box of 100)","7318","box",1450,1900,18),("Hex nut M12, 8 grade (box of 200)","7318","box",780,1050,18),("Spring washer M12 (box of 500)","7318","box",620,850,18),("Allen cap screw M8x25 (box of 200)","7318","box",1350,1750,18),("Anchor fastener M16x150","7318","nos",68,95,18)],"BFC/{n:05d}/{fy}",3,"L3"),
 ("Vortex Pneumatics Pvt Ltd","Coimbatore","33","C","goods",[("Pneumatic cylinder ISO 6431, 63x200","8412","nos",5800,8200,18),("5/2 solenoid valve 1/4 inch","8481","nos",2900,4100,18),("FRL unit 1/2 inch","8481","nos",6200,8800,18),("PU tube 8mm (100m roll)","3917","roll",1800,2500,18)],"VP-{yy}{mm}-{n:04d}",1,"L4"),
 ("Marudhar Machine Tools Pvt Ltd","Jodhpur","08","C","goods",[("CNC turning centre spare – turret index motor","8466","nos",84000,118000,18),("Ball screw assembly 32mm x 1200","8483","nos",46000,64000,18),("Spindle bearing set","8482","set",38000,52000,18),("Annual maintenance visit – machine tools","998717","visit",18000,26000,18)],"MMT/{fy}/{n:03d}",2,"L5"),
 ("Trident Industrial Maintenance Services","Vadodara","24","P","service",[("Preventive maintenance – Plant 2 – {mon} 2026","998717","hour",850,1250,18),("Breakdown support – {mon} 2026","998717","hour",1100,1600,18),("Millwright fitter deployment – {mon} 2026","998717","hour",700,980,18)],"TIMS/{n:04d}/{fy}",3,"L6"),
 ("Kalinga Polymers Ltd","Bhubaneswar","21","C","goods",[("HDPE granules injection grade (25kg bag)","3901","bag",2650,3100,18),("PP copolymer granules (25kg bag)","3902","bag",2800,3300,18),("Masterbatch black 40% (25kg)","3204","bag",4200,5100,18)],"KPL-{yy}-{n:05d}",1,"L7"),
 ("Surya Electricals & Automation Pvt Ltd","Vadodara","24","C","goods",[("MCC panel feeder 63A","8537","nos",24000,33000,18),("VFD 15kW 415V","8504","nos",38000,52000,18),("Contactor 3-pole 40A","8536","nos",1450,2100,18),("PLC I/O module 16DI","8537","nos",11500,15800,18)],"SEA/{fy}/{n:04d}",2,"L1"),
 ("Greenline Logistics Pvt Ltd","Surat","24","C","goods",[("Freight – Vadodara to Rajkot, 32ft MXL","996511","trip",14500,19500,12),("Freight – Vadodara to Ludhiana, 20ft","996511","trip",38000,46000,12),("Local material movement (per day)","996511","day",5200,6800,12)],"GL/{yy}/{n:05d}",3,"L2"),
 ("Precision Weld Fabricators","Anand","24","P","service",[("Fabrication job work – structural – {mon} 2026","998873","hour",620,900,12),("Welding services on site – {mon} 2026","998873","hour",680,950,12)],"PWF-{n:04d}",1,"L3"),
],
"C": [
 ("Hue & Cry Creative Studio LLP","Mumbai","27","F","service",[("Brand identity design – {proj}","998391","hour",1700,2500,18),("Campaign key visuals – {proj}","998391","hour",1600,2300,18),("Packaging design – {proj}","998391","hour",1500,2200,18)],"HCC/{fy}/{n:04d}",1,"L1"),
 ("Signalbox Media Buying Pvt Ltd","Mumbai","27","C","service",[("Digital media planning – {proj} – {mon} 2026","998361","hour",1400,2100,18),("Campaign optimisation – {proj} – {mon} 2026","998361","hour",1300,1900,18)],"SMB-{yy}-{n:05d}",2,"L2"),
 ("Verbatim Content Collective","Pune","27","P","service",[("Long-form content writing – {proj}","998393","hour",900,1400,18),("Social copy – {proj} – {mon} 2026","998393","hour",850,1250,18)],"VCC/{n:04d}",3,"L3"),
 ("Northstar Strategy Consultants Pvt Ltd","New Delhi","07","C","service",[("Brand strategy consulting – {proj}","998311","hour",4200,6500,18),("Market research synthesis – {proj}","998311","hour",3600,5200,18)],"NSC/{fy}/{n:03d}",1,"L4"),
 ("Frameline Film Productions Pvt Ltd","Mumbai","27","C","service",[("Video production – {proj} – shoot days","998396","hour",3200,4800,18),("Post-production & edit – {proj}","998396","hour",1900,2800,18)],"FFP-{yy}{mm}-{n:03d}",2,"L5"),
 ("Echo Influence Network Pvt Ltd","Bengaluru","29","C","service",[("Influencer campaign management – {proj}","998361","hour",1500,2300,18),("Creator coordination – {proj} – {mon} 2026","998361","hour",1100,1700,18)],"EIN/{fy}/{n:04d}",3,"L6"),
 ("Printwell Offset Works","Mumbai","27","P","goods",[("Brochure A4 tri-fold, 170gsm art, 4+4 (per 1000)","4911","lot",9800,13500,12),("Poster A2, 250gsm, 4+0 (per 100)","4911","lot",6200,8800,12),("Business cards 350gsm matte (per 500)","4911","lot",1800,2600,12),("Roll-up standee 850x2000 with print","3926","nos",3200,4600,18)],"PW/{fy}/{n:05d}",1,"L7"),
 ("Gridspace Event Management LLP","Mumbai","27","F","service",[("Event production – {proj}","998596","hour",2100,3200,18),("Venue coordination & logistics – {proj}","998596","hour",1600,2400,18)],"GEM-{n:04d}/{fy}",2,"L1"),
 ("Talkpoint PR Advisory Pvt Ltd","Gurugram","06","C","service",[("PR retainer – {mon} 2026","998311","hour",2600,3800,18),("Media outreach – {proj}","998311","hour",2200,3300,18)],"TPA/{yy}/{n:04d}",3,"L2"),
 ("Bluewick Photography Studio","Mumbai","27","P","service",[("Product photography – {proj}","998387","hour",2400,3600,18),("Retouching – {proj}","998387","hour",1200,1800,18)],"BWP-{yy}-{n:04d}",1,"L3"),
]}

BANKS = ["Kaveri Mercantile Bank","Saraswati Cooperative Bank Ltd","Aravalli National Bank","Deccan Trust Bank","Konkan Commerce Bank"]
NAMES = ["R. Iyer","S. Banerjee","P. Mehta","A. Khan","K. Nair","V. Deshmukh","M. Reddy","N. Kulkarni","J. Fernandes","T. Sharma"]
PROJECTS_C = ["Solstice Skincare launch","Orbit Bank rebrand","Nectar Foods summer campaign","Vireo Mobility IPO comms","Halo Home appliances festive push","Cobalt Wear D2C launch"]

VENDORS = []
for cid, specs in VENDOR_SPECS.items():
    for i, (name, city, state, pankind, kind, items, pattern, terms_i, layout) in enumerate(specs, 1):
        pan = rand_pan(pankind)
        v = dict(vendor_id=f"V{cid}{i:02d}", company_id=cid, name=name, city=city, state=state, state_name=STATES[state],
                 address=f"{rng.randint(11,899)}, {rng.choice(['Industrial Area','Tech Boulevard','Sector','Main Road','MIDC Lane','Commercial Complex'])} {rng.randint(1,42)}",
                 pin=f"{rng.randint(110000,700000)}", pan=pan, gstin=make_gstin(state, pan), kind=kind, items=items,
                 number_pattern=pattern, payment_terms=[15,30,45,60][terms_i-1] if terms_i<=3 else 30, layout=layout,
                 bank=rng.choice(BANKS), account=f"{rng.randint(10**11, 10**12-1)}", ifsc=f"{rng.choice(['KMBK','SCBL','ARNB','DTBK','KCBL'])}0{rng.randint(100000,999999)}",
                 email=f"billing@{name.split()[0].lower().replace('&','and')}.example", phone=f"+91 {rng.randint(70000,99999)} {rng.randint(10000,99999)}")
        VENDORS.append(v)
VBY = {v["vendor_id"]: v for v in VENDORS}

# ---------------------------------------------------------------- PO master
PO_MASTER = []
def add_po(v, status="ACTIVE", customer=None):
    cid = customer or v["company_id"]
    n = len(PO_MASTER) + 1
    po_no = f"PO-2026-{4000+n:04d}" if cid=="B" else (f"ZC-PO-{26}{n:04d}" if cid=="A" else f"LB/PO/{n:04d}")
    item = rng.choice(v["items"])
    qty = rng.choice([1,2,5,10,20,50,100,200]) if v["kind"]=="goods" else rng.choice([40,80,120,160,240,320])
    price = D(rng.randint(item[3], item[4]))
    start = dt.date(2026, rng.randint(1,4), rng.randint(1,28))
    po = dict(po_number=po_no, customer_id=cid, vendor_id=v["vendor_id"], item_description=item[0], hsn_sac=item[1],
              approved_quantity=qty, unit=item[2], approved_unit_price=str(price), currency="INR",
              valid_from=start.isoformat(), valid_to=(dt.date(2027,3,31) if status=="ACTIVE" else (start+dt.timedelta(days=90))).isoformat(), status=status,
              approved_value=str(D(price*qty)))
    PO_MASTER.append(po); return po
for v in VENDORS:
    k = 4 if v["kind"]=="goods" else 2
    for _ in range(k): add_po(v)
    add_po(v, status="CANCELLED")
POBY = {p["po_number"]: p for p in PO_MASTER}

# ---------------------------------------------------------------- Work Authorization master
WA_MASTER = []
def add_wa(v, status="ACTIVE", expired=False):
    n = len(WA_MASTER)+1
    wa_id = f"WA-{v['company_id']}-{2026}-{n:03d}"
    item = rng.choice(v["items"])
    hours = rng.choice([40,60,80,100,120,160,200,240,320])
    rate = D(rng.randint(item[3], item[4]))
    proj = rng.choice(PROJECTS_C) if v["company_id"]=="C" else (rng.choice(["Platform Modernisation","Data Lake Build","Security Program 2026","Cloud Cost Optimisation","Customer Portal v3"]) if v["company_id"]=="A" else rng.choice(["Plant 2 Maintenance","Line 4 Expansion","Annual Shutdown 2026"]))
    if expired:
        start = dt.date(2025, rng.randint(10,12), 1); end = dt.date(2026, rng.randint(1,3), 28)
    else:
        start = dt.date(2026, rng.randint(3,5), 1); end = dt.date(2026, rng.randint(9,12), 28)
    wa = dict(work_authorization_id=wa_id, vendor_id=v["vendor_id"], customer_id=v["company_id"], project=proj,
              approved_hours=hours, hourly_rate=str(rate), period_start=start.isoformat(), period_end=end.isoformat(),
              status="EXPIRED" if expired else status, rate_item=item[0])
    WA_MASTER.append(wa); return wa
for v in VENDORS:
    if v["kind"]=="service":
        for _ in range(3): add_wa(v)
        add_wa(v, expired=True)
WABY = {w["work_authorization_id"]: w for w in WA_MASTER}

# ---------------------------------------------------------------- Invoice content synthesis
CHECKS = ["1.1","1.2","1.3","1.4","1.5","2.1","2.2","3.1","3.2","3.3","4.1","4.2","4.3","4.4"]
CHECK_NAMES = {"1.1":"extraction_success","1.2":"customer_verification","1.3":"tax_compliance","1.4":"work_authorization","1.5":"single_document","2.1":"line_items_present","2.2":"po_validation","3.1":"duplicate_detection","3.2":"tax_id_validation","3.3":"future_date_check","4.1":"total_verification","4.2":"line_sum_validation","4.3":"authorization_hours_check","4.4":"tax_rate_verification"}
REJECTION = {"1.1":"Document is not a valid Tax Invoice","1.2":"Invoice addressed to different company","1.3":"Document is not a Tax Invoice","1.4":"Missing Work Authorization Form","1.5":"Multiple invoices in submission","2.1":"Invoice charges not itemized","2.2":"Invalid purchase order","3.1":"Invoice already processed","3.2":"Vendor tax ID invalid","3.3":"Invoice is future dated","4.1":None,"4.2":"Invoice amounts do not reconcile","4.3":"Invoice does not match work authorization","4.4":"Invoice tax amount incorrect"}

MONTHS = ["April","May","June","July","August","September"]
def fy_str(d): return "2026-27"
def fmt_number(v, pattern, seq, d):
    return pattern.format(fy="2026-27", yy="26", n=seq, mm=f"{d.month:02d}", sp=rng.randint(9,18))

invoice_seq = {}
def next_seq(vid):
    invoice_seq[vid] = invoice_seq.get(vid, rng.randint(100, 900)) + rng.randint(1, 9)
    return invoice_seq[vid]

def build_lines(v, wa=None, po=None, n_lines=None, month="August", proj=None):
    """Build correct line items. Returns list of dict."""
    items = v["items"]
    lines = []
    if v["kind"]=="service":
        n = n_lines or rng.choice([1,1,2,2,3,4])
        remaining = wa["approved_hours"] if wa else rng.choice([80,120,160,240])
        rate = D(wa["hourly_rate"]) if wa else None
        for i in range(n):
            it = items[0] if (wa and i==0) else rng.choice(items)
            hrs = max(4, int(remaining*(0.35 if n>1 else 0.85))) if i < n-1 else max(4, int(remaining*0.9))
            hrs = min(hrs, remaining); remaining -= hrs
            if hrs <= 0: break
            r = rate if wa else D(rng.randint(it[3], it[4]))
            desc = it[0].format(mon=month, sp=rng.randint(9,18), name=rng.choice(NAMES), rk=rng.randint(3,44), proj=proj or rng.choice(PROJECTS_C))
            lines.append(dict(description=desc, hsn_sac=it[1], quantity=hrs, unit="hour", unit_price=str(r), discount_pct=0, gst_rate=it[5]))
    else:
        n = n_lines or rng.choice([1,2,3,3,4,5,6,8,10,12])
        chosen = [rng.choice(items) for _ in range(n)]
        for i, it in enumerate(chosen):
            if po and i==0:
                qty = po["approved_quantity"]; price = D(po["approved_unit_price"]); desc = po["item_description"]; hsn = po["hsn_sac"]; unit = po["unit"]
            else:
                qty = rng.choice([1,2,3,4,5,6,8,10,12,15,20,25,50,100]) if it[2] in ("nos","box","bag","pack","seat","user","block","lot","roll","set") else rng.choice([1,2,3,5,8,10,12,15])
                price = D(rng.randint(it[3], it[4])); desc = it[0]; hsn = it[1]; unit = it[2]
            disc = rng.choice([0,0,0,0,2,5,10])
            rate = {x[1]: x[5] for x in items}.get(hsn, it[5])
            lines.append(dict(description=desc, hsn_sac=hsn, quantity=qty, unit=unit, unit_price=str(price), discount_pct=disc, gst_rate=rate))
    for ln in lines:
        gross = D(D(ln["unit_price"]) * ln["quantity"])
        ln["line_discount"] = str(D(gross * ln["discount_pct"] / 100))
        ln["line_total"] = str(D(gross - D(ln["line_discount"])))
    return lines

def compute_totals(lines, discount=D(0), other=None, intra=True, roundoff=True):
    other = other or []
    subtotal = sum((D(l["line_total"]) for l in lines), D(0))
    taxable = D(subtotal - discount)
    # allocate invoice discount proportionally for tax base per rate
    tax_by_rate = {}
    for l in lines:
        base = D(l["line_total"]) * (taxable/subtotal if subtotal else 1)
        tax_by_rate[l["gst_rate"]] = tax_by_rate.get(l["gst_rate"], D(0)) + D(base)
    other_total = sum((D(o["amount"]) for o in other), D(0))
    other_tax = sum((D(D(o["amount"]) * o["gst_rate"] / 100) for o in other), D(0))
    tax_amount = sum((D(b * r / 100) for r, b in tax_by_rate.items()), D(0)) + other_tax
    comps = {}
    if intra:
        comps["cgst"] = D(tax_amount/2); comps["sgst"] = D(tax_amount - comps["cgst"]); comps["igst"] = D(0)
    else:
        comps["igst"] = tax_amount; comps["cgst"] = D(0); comps["sgst"] = D(0)
    pre = D(taxable + tax_amount + other_total)
    ro = D(pre.quantize(Decimal("1"), rounding=ROUND_HALF_UP) - pre) if roundoff else D(0)
    total = D(pre + ro)
    return dict(subtotal=str(subtotal), discount=str(discount), taxable_amount=str(taxable), other_charges=str(other_total),
                tax_amount=str(tax_amount), cgst=str(comps["cgst"]), sgst=str(comps["sgst"]), igst=str(comps["igst"]),
                round_off=str(ro), grand_total=str(total), tax_by_rate={str(k): str(v) for k, v in tax_by_rate.items()})

DIFF_PLAN = ["EASY"]*30 + ["MEDIUM"]*40 + ["HARD"]*20 + ["ADVERSARIAL"]*10

def new_invoice(cid, vendor, seq_id, difficulty, date=None):
    comp = COMPANIES[cid]
    d = date or dt.date(2026, rng.randint(4, 9), rng.randint(1, 28))
    if d > REF_DATE: d = REF_DATE - dt.timedelta(days=rng.randint(0, 40))
    wa = po = None
    if vendor["kind"]=="service":
        cands = [w for w in WA_MASTER if w["vendor_id"]==vendor["vendor_id"] and w["status"]=="ACTIVE"]
        wa = rng.choice(cands)
        ps, pe = dt.date.fromisoformat(wa["period_start"]), dt.date.fromisoformat(wa["period_end"])
        if d < ps: d = ps + dt.timedelta(days=rng.randint(3, 25))
        if d > pe: d = pe - dt.timedelta(days=rng.randint(1, 20))
        if d > REF_DATE: d = REF_DATE - dt.timedelta(days=rng.randint(0, 10))
        if rng.random() < 0.5:
            po = rng.choice([p for p in PO_MASTER if p["vendor_id"]==vendor["vendor_id"] and p["status"]=="ACTIVE"])
    else:
        po = rng.choice([p for p in PO_MASTER if p["vendor_id"]==vendor["vendor_id"] and p["status"]=="ACTIVE"])
    if po:
        vf, vt = dt.date.fromisoformat(po["valid_from"]), dt.date.fromisoformat(po["valid_to"])
        if d < vf: d = vf + dt.timedelta(days=rng.randint(2, 15))
        if d > vt: d = vt - dt.timedelta(days=rng.randint(1, 15))
        if d > REF_DATE: d = REF_DATE - dt.timedelta(days=rng.randint(0, 5))
        if wa:
            ps, pe = dt.date.fromisoformat(wa["period_start"]), dt.date.fromisoformat(wa["period_end"])
            if not (ps <= d <= pe): po = None
    n_lines = None
    if difficulty in ("HARD","ADVERSARIAL") and vendor["kind"]=="goods": n_lines = rng.choice([8,10,12,14])
    month = MONTHS[min(d.month-4, 5)] if d.month >= 4 else "April"
    lines = build_lines(vendor, wa=wa, po=po, n_lines=n_lines, month=month, proj=(wa["project"] if wa and cid=="C" else None))
    other = []
    if vendor["kind"]=="goods" and rng.random()<0.45:
        other.append(dict(description=rng.choice(["Freight & forwarding","Packing charges","Transit insurance"]), amount=str(D(rng.randint(500,9000))), gst_rate=lines[0]["gst_rate"]))
    if vendor["kind"]=="service" and rng.random()<0.2:
        other.append(dict(description="Travel & out-of-pocket expenses (at actuals)", amount=str(D(rng.randint(2000,25000))), gst_rate=18))
    discount = D(0)
    if vendor["kind"]=="goods" and rng.random()<0.25:
        discount = D(sum(D(l["line_total"]) for l in lines) * rng.choice([2,3,5]) / 100)
    intra = vendor["state"] == comp["state"]
    totals = compute_totals(lines, discount, other, intra, roundoff=rng.random()<0.6)
    seq = next_seq(vendor["vendor_id"])
    inv = dict(
        invoice_id=f"{comp['code']}-INV-{seq_id:03d}", company_id=cid, vendor_id=vendor["vendor_id"], customer_id=cid,
        vendor_name=vendor["name"], vendor_gstin=vendor["gstin"], vendor_state=vendor["state"],
        customer_name=comp["legal_name"], customer_gstin=comp["gstin"], customer_state=comp["state"], customer_address=f"{comp['address']}, {comp['city']} {comp['pin']}, {STATES[comp['state']]}",
        invoice_number=fmt_number(None, vendor["number_pattern"], seq, d), invoice_date=d.isoformat(),
        due_date=(d+dt.timedelta(days=vendor["payment_terms"])).isoformat(), payment_terms=f"Net {vendor['payment_terms']}",
        po_number=po["po_number"] if po else "", work_authorization_id=wa["work_authorization_id"] if wa else "",
        project=wa["project"] if wa else "", service_period=(f"{max(d-dt.timedelta(days=rng.choice([14,21,30])), dt.date.fromisoformat(wa['period_start'])).isoformat()} to {d.isoformat()}" if wa else ""), currency="INR", supply_type="INTRA" if intra else "INTER",
        place_of_supply=f"{comp['state']}-{STATES[comp['state']]}", lines=lines, other_charge_lines=other, **totals,
        line_item_count=len(lines), document_type="TAX_INVOICE", difficulty=difficulty, layout=vendor["layout"],
        month=month, invoice_title="TAX INVOICE",
    )
    return inv

# ---------------------------------------------------------------- Fault injection
def inject(inv, check, submitted):
    """Mutate `submitted` (a deep copy of correct inv) for check; return fault record."""
    f = dict(invoice_id=inv["invoice_id"], check_id=check, check_name=CHECK_NAMES[check])
    comp = COMPANIES[inv["company_id"]]; v = VBY[inv["vendor_id"]]
    if check == "1.2":
        other = rng.choice([c for c in COMPANIES.values() if c["company_id"] != inv["company_id"]])
        f.update(fault_category="WRONG_CUSTOMER", fault_description="Bill-to party is a different fictional company",
                 correct_value=inv["customer_name"], injected_value=other["legal_name"])
        submitted["customer_name"]=other["legal_name"]; submitted["customer_gstin"]=other["gstin"]; submitted["customer_state"]=other["state"]
        submitted["customer_address"]=f"{other['address']}, {other['city']} {other['pin']}, {STATES[other['state']]}"
    elif check == "1.3":
        mode = rng.choice(["MISSING_GSTIN","NO_TAX_BREAKUP","BILL_OF_SUPPLY","TITLE_INVOICE_NO_TAX_FIELDS"])
        f.update(fault_category=mode, correct_value="TAX INVOICE with GSTIN and tax breakup", injected_value=mode)
        if mode=="MISSING_GSTIN":
            f["fault_description"]="Supplier GSTIN omitted from document"; submitted["vendor_gstin"]=""; submitted["tax_compliance_variant"]=mode
        elif mode=="NO_TAX_BREAKUP":
            f["fault_description"]="Document shows no GST breakup/HSN; only a gross amount"; submitted["tax_compliance_variant"]=mode
            submitted["invoice_title"]="INVOICE"
        elif mode=="BILL_OF_SUPPLY":
            f["fault_description"]="Document is titled 'Bill of Supply' (no tax charged)"; submitted["invoice_title"]="BILL OF SUPPLY"; submitted["tax_compliance_variant"]=mode
        else:
            f["fault_description"]="Document titled 'Invoice' lacking tax-invoice fields (no GSTIN, no HSN/SAC, no tax lines)"; submitted["invoice_title"]="INVOICE"; submitted["vendor_gstin"]=""; submitted["tax_compliance_variant"]=mode
        submitted["tax_amount"]="0.00"; submitted["cgst"]=submitted["sgst"]=submitted["igst"]="0.00"
        submitted["grand_total"]=str(D(D(submitted["taxable_amount"])+D(submitted["other_charges"])))
        submitted["round_off"]="0.00"
    elif check == "1.4":
        mode = rng.choice(["MISSING","INVALID_ID","EXPIRED","OTHER_VENDOR"])
        cur = inv["work_authorization_id"]
        if mode=="MISSING": new=""; desc="Work authorization reference omitted"
        elif mode=="INVALID_ID": new=f"WA-{inv['company_id']}-2026-{rng.randint(700,999)}"; desc="Work authorization ID does not exist in master"
        elif mode=="EXPIRED": new=rng.choice([w["work_authorization_id"] for w in WA_MASTER if w["vendor_id"]==v["vendor_id"] and w["status"]=="EXPIRED"]); desc="Referenced work authorization is expired"
        else: new=rng.choice([w["work_authorization_id"] for w in WA_MASTER if w["customer_id"]==inv["company_id"] and w["vendor_id"]!=v["vendor_id"] and w["status"]=="ACTIVE"]); desc="Work authorization belongs to another vendor"
        f.update(fault_category=f"WA_{mode}", fault_description=desc, correct_value=cur, injected_value=new or "(blank)")
        submitted["work_authorization_id"]=new
    elif check == "1.5":
        k = rng.choice([2,2,3])
        f.update(fault_category="MULTI_DOCUMENT", fault_description=f"Submission PDF contains {k} separate invoices", correct_value="1 invoice", injected_value=f"{k} invoices")
        submitted["extra_invoices"]=k-1
    elif check == "2.1":
        mode = rng.choice(["LUMPSUM","NO_TABLE","TOTAL_ONLY"])
        f.update(fault_category=f"NOT_ITEMIZED_{mode}", fault_description={"LUMPSUM":"Single unitemized line 'Professional/supply services' at gross amount","NO_TABLE":"Line-item table omitted entirely","TOTAL_ONLY":"Only grand total shown"}[mode], correct_value=f"{inv['line_item_count']} itemized lines", injected_value=mode)
        submitted["itemization_variant"]=mode
        submitted["lines"]=[dict(description=("Professional services" if v["kind"]=="service" else "Supply of goods as per order"), hsn_sac=inv["lines"][0]["hsn_sac"], quantity=1, unit="lot", unit_price=inv["subtotal"], discount_pct=0, line_discount="0.00", line_total=inv["subtotal"], gst_rate=inv["lines"][0]["gst_rate"])]
        submitted["line_item_count"]=0
    elif check == "2.2":
        modes = ["NOT_EXISTS","OTHER_VENDOR","OTHER_CUSTOMER","CANCELLED"] + (["QTY_EXCEEDS","PRICE_CONFLICT"] if v["kind"]=="goods" else [])
        mode = rng.choice(modes); cur = inv["po_number"]
        if mode=="NOT_EXISTS": new = (f"PO-2026-{rng.randint(6000,7999)}" if inv["company_id"]=="B" else (f"ZC-PO-26{rng.randint(5000,7999)}" if inv["company_id"]=="A" else f"LB/PO/{rng.randint(5000,7999)}")); desc="PO number does not exist"
        elif mode=="OTHER_VENDOR": new = rng.choice([p["po_number"] for p in PO_MASTER if p["customer_id"]==inv["company_id"] and p["vendor_id"]!=v["vendor_id"] and p["status"]=="ACTIVE"]); desc="PO belongs to another vendor"
        elif mode=="OTHER_CUSTOMER": new = rng.choice([p["po_number"] for p in PO_MASTER if p["customer_id"]!=inv["company_id"] and p["status"]=="ACTIVE"]); desc="PO belongs to another customer company"
        elif mode=="CANCELLED": new = rng.choice([p["po_number"] for p in PO_MASTER if p["vendor_id"]==v["vendor_id"] and p["status"]=="CANCELLED"]); desc="PO is cancelled"
        elif mode=="QTY_EXCEEDS":
            new = cur; po = POBY[cur]; q0 = submitted["lines"][0]["quantity"]; q1 = int(po["approved_quantity"]*rng.choice([1.5,2,3]))+1
            submitted["lines"][0]["quantity"]=q1; _recalc_line(submitted["lines"][0]); _recalc_totals(submitted, inv); desc=f"Invoiced quantity {q1} exceeds PO approved quantity {po['approved_quantity']}"
            f.update(correct_value=str(q0), injected_value=str(q1))
        else:
            new = cur; po = POBY[cur]; p0 = submitted["lines"][0]["unit_price"]; p1 = str(D(D(p0)*Decimal(rng.choice(["1.08","1.15","1.25"]))))
            submitted["lines"][0]["unit_price"]=p1; _recalc_line(submitted["lines"][0]); _recalc_totals(submitted, inv); desc=f"Unit price {p1} conflicts with PO price {po['approved_unit_price']}"
            f.update(correct_value=p0, injected_value=p1)
        if "correct_value" not in f: f.update(correct_value=cur, injected_value=new)
        f.update(fault_category=f"PO_{mode}", fault_description=desc); submitted["po_number"]=new
    elif check == "3.2":
        mode = rng.choice(["BAD_CHECKSUM","OTHER_VENDOR","NOT_EXISTS","ALTERED_DIGIT","MISSING"])
        g = inv["vendor_gstin"]
        if mode=="BAD_CHECKSUM": new = g[:14] + ALNUM[(ALNUM.index(g[14])+7)%36]; desc="GSTIN check character invalid"
        elif mode=="OTHER_VENDOR": new = rng.choice([x["gstin"] for x in VENDORS if x["vendor_id"]!=v["vendor_id"]]); desc="GSTIN belongs to a different vendor"
        elif mode=="NOT_EXISTS": new = make_gstin(v["state"], rand_pan("C")); desc="Structurally valid GSTIN not present in vendor master"
        elif mode=="ALTERED_DIGIT": i = rng.randint(2,11); new = g[:i] + ALNUM[(ALNUM.index(g[i])+3)%36] + g[i+1:]; desc="One character of GSTIN altered (checksum no longer matches)"
        else: new = ""; desc="Vendor GSTIN missing where mandatory"
        f.update(fault_category=f"TAXID_{mode}", fault_description=desc, correct_value=g, injected_value=new or "(blank)"); submitted["vendor_gstin"]=new
    elif check == "3.3":
        d = REF_DATE + dt.timedelta(days=rng.choice([1,1,2,5,7,12,20,28,45,60]))
        f.update(fault_category="FUTURE_DATE", fault_description=f"Invoice dated after reference date {REF_DATE}", correct_value=inv["invoice_date"], injected_value=d.isoformat())
        submitted["invoice_date"]=d.isoformat(); submitted["due_date"]=(d+dt.timedelta(days=v["payment_terms"])).isoformat()
    elif check == "4.1":
        t0 = D(D(submitted["subtotal"])-D(submitted["discount"])+D(submitted["tax_amount"])+D(submitted["other_charges"])+D(submitted["round_off"]))
        delta = rng.choice([D(1000), D(-500), D(100), D(2500), D(-1200), D(10000), D(0.5), D(-0.9)])
        t1 = str(D(t0+delta))
        f.update(fault_category="TOTAL_MISMATCH", fault_description="Stated grand total differs from the total computed from the stated subtotal, discount, tax and other charges", correct_value=str(t0), injected_value=t1)
        submitted["grand_total"]=t1
    elif check == "4.2":
        removable = [k for k in range(1, len(submitted["lines"])) if submitted["lines"][k]["gst_rate"]>0]
        if v["kind"]=="service":
            mode = rng.choice(["LINE_TOTAL_ALTERED","LINE_DISCOUNT_WRONG","SUBTOTAL_WRONG"]) if len(submitted["lines"])>1 else rng.choice(["LINE_TOTAL_ALTERED","SUBTOTAL_WRONG"])
        else:
            mode = rng.choice(["LINE_TOTAL_ALTERED","QTY_PRICE_MISMATCH","LINE_DISCOUNT_WRONG","SUBTOTAL_WRONG"] + (["MISSING_LINE"] if removable else [])) if len(submitted["lines"])>1 else rng.choice(["LINE_TOTAL_ALTERED","SUBTOTAL_WRONG"])
        i = (rng.choice(removable) if mode=="MISSING_LINE" else (rng.randrange(1, len(submitted["lines"])) if len(submitted["lines"])>1 else 0)); ln = submitted["lines"][i]
        if mode=="LINE_TOTAL_ALTERED":
            old = ln["line_total"]; new = str(D(D(old)+rng.choice([D(100),D(250),D(-150),D(1000),D(50)]))); ln["line_total"]=new; desc=f"Line {i+1} total altered; qty x price no longer equals line total"
        elif mode=="QTY_PRICE_MISMATCH":
            old = str(ln["quantity"]); ln["quantity"]=ln["quantity"]+rng.choice([1,2,5]); new=str(ln["quantity"]); desc=f"Line {i+1} quantity changed without changing line total"
        elif mode=="LINE_DISCOUNT_WRONG":
            old = ln["line_discount"]; new = str(D(D(old)+rng.choice([D(200),D(500),D(75)]))); ln["line_discount"]=new; desc=f"Line {i+1} discount changed; line total not updated"
        elif mode=="SUBTOTAL_WRONG":
            old = submitted["subtotal"]; new = str(D(D(old)+rng.choice([D(500),D(1000),D(-300),D(2000)]))); submitted["subtotal"]=new; desc="Stated subtotal differs from the sum of line totals (tax and total computed from the wrong subtotal)"
            delta = D(D(new)-D(old)); submitted["taxable_amount"]=str(D(D(submitted["taxable_amount"])+delta))
            ls = sum((D(l["line_total"]) for l in submitted["lines"]), D(0)); t1 = sum((D(D(submitted["taxable_amount"])*D(l["line_total"])/ls*l["gst_rate"]/100) for l in submitted["lines"]), D(0)) + sum((D(D(o["amount"])*o["gst_rate"]/100) for o in submitted["other_charge_lines"]), D(0)); t1 = D(t1); submitted["tax_amount"]=str(t1)
            if submitted["supply_type"]=="INTRA": submitted["cgst"]=str(D(t1/2)); submitted["sgst"]=str(D(t1-D(t1/2)))
            else: submitted["igst"]=str(t1)
            pre = D(D(submitted["taxable_amount"])+t1+D(submitted["other_charges"])); ro = D(pre.quantize(Decimal("1"), rounding=ROUND_HALF_UP)-pre) if D(submitted["round_off"])!=0 else D(0)
            submitted["round_off"]=str(ro); submitted["grand_total"]=str(D(pre+ro))
        else:
            old = f"{len(submitted['lines'])} lines"; removed = submitted["lines"].pop(i); new = f"{len(submitted['lines'])} lines (line '{removed['description'][:30]}' omitted)"; desc="A line item is missing from the table but its value remains in the subtotal"
            submitted["line_item_count"]=len(submitted["lines"])
        f.update(fault_category=f"LINESUM_{mode}", fault_description=desc, correct_value=old, injected_value=new)
    elif check == "4.3":
        wa = WABY[inv["work_authorization_id"]]
        mode = rng.choice(["HOURS_EXCEED","WRONG_RATE","WRONG_PROJECT","OUTSIDE_PERIOD"])
        ln = submitted["lines"][0]
        if mode=="HOURS_EXCEED":
            old = str(sum(l["quantity"] for l in submitted["lines"])); extra = wa["approved_hours"] - int(old) + rng.choice([8,16,24,40]); ln["quantity"] += extra; _recalc_line(ln); _recalc_totals(submitted, inv); new=str(sum(l["quantity"] for l in submitted["lines"])); desc=f"Invoiced hours {new} exceed approved {wa['approved_hours']}"
        elif mode=="WRONG_RATE":
            old = ln["unit_price"]; new = str(D(D(old)*Decimal(rng.choice(["1.10","1.20","0.90"])))); ln["unit_price"]=new; _recalc_line(ln); _recalc_totals(submitted, inv); desc=f"Hourly rate {new} differs from authorized rate {wa['hourly_rate']}"
        elif mode=="WRONG_PROJECT":
            old = inv["project"]; new = rng.choice([w["project"] for w in WA_MASTER if w["project"]!=old]); submitted["project"]=new; desc="Invoice references a project different from the work authorization"
        else:
            old = inv["service_period"]; e = dt.date.fromisoformat(wa["period_start"]) - dt.timedelta(days=rng.randint(3,30)); new = f"{(e-dt.timedelta(days=21)).isoformat()} to {e.isoformat()}"; submitted["service_period"]=new; desc="Service period falls outside the authorization period"
        f.update(fault_category=f"WA_{mode}", fault_description=desc, correct_value=old, injected_value=new)
    elif check == "4.4":
        modes = ["WRONG_RATE","WRONG_AMOUNT","WRONG_BASE","COMPONENT_MISMATCH","TAX_ON_EXEMPT","ROUNDING"]
        mode = rng.choice(modes)
        tax0 = D(submitted["tax_amount"]); intra = submitted["supply_type"]=="INTRA"
        if mode=="WRONG_RATE":
            r0 = submitted["lines"][0]["gst_rate"]; r1 = rng.choice([r for r in (5,12,18,28) if r!=r0])
            for l in submitted["lines"]: l["gst_rate"]=r1
            tax1 = D(D(submitted["taxable_amount"])*r1/100 + D(submitted["other_charges"])*r1/100); desc=f"GST rate {r1}% applied instead of {r0}%"; old=f"{r0}%"; new=f"{r1}%"
        elif mode=="WRONG_AMOUNT":
            tax1 = D(tax0 + rng.choice([D(500),D(-350),D(1200),D(90)])); desc="Tax amount does not equal taxable amount x rate"; old=str(tax0); new=str(tax1)
        elif mode=="WRONG_BASE":
            base_wrong = D(submitted["subtotal"]) + D(submitted["other_charges"]) + D(submitted["discount"]) + D(rng.choice([1500,3000,5000]))
            tax1 = D(base_wrong * submitted["lines"][0]["gst_rate"]/100); desc="Tax computed on an incorrect base (discount not deducted / extra amount included)"; old=str(tax0); new=str(tax1)
        elif mode=="COMPONENT_MISMATCH":
            tax1 = tax0; desc = ("IGST charged on an intra-state supply (should be CGST+SGST)" if intra else "CGST+SGST charged on an inter-state supply (should be IGST)")
            old = "CGST+SGST" if intra else "IGST"; new = "IGST" if intra else "CGST+SGST"; submitted["supply_type"] = "INTER" if intra else "INTRA"; intra = not intra
        elif mode=="TAX_ON_EXEMPT":
            ex = dict(description=rng.choice(["Exempt supply – printed books","Exempt – pure agent reimbursement (no GST)","Non-taxable: government fee reimbursed at actuals"]), hsn_sac="4901", quantity=1, unit="lot", unit_price=str(D(rng.randint(2000,9000))), discount_pct=0, line_discount="0.00", gst_rate=0)
            ex["line_total"]=ex["unit_price"]; submitted["lines"].append(ex); submitted["line_item_count"]=len(submitted["lines"])
            submitted["subtotal"]=str(D(D(submitted["subtotal"])+D(ex["line_total"]))); submitted["taxable_amount"]=str(D(D(submitted["taxable_amount"])+D(ex["line_total"])))
            r = submitted["lines"][0]["gst_rate"]; tax1 = D(tax0 + D(ex["line_total"])*r/100); desc="Tax charged on an exempt (0%) line"; old="0.00 tax on exempt line"; new=str(D(D(ex['line_total'])*r/100))
        else:
            tax1 = D(tax0 + Decimal(rng.choice(["3.40","-2.75","5.10","4.20"]))); desc="Tax rounding error beyond permitted tolerance (±1.00)"; old=str(tax0); new=str(tax1)
        submitted["tax_amount"]=str(tax1)
        if intra: submitted["cgst"]=str(D(tax1/2)); submitted["sgst"]=str(D(tax1-D(tax1/2))); submitted["igst"]="0.00"
        else: submitted["igst"]=str(tax1); submitted["cgst"]=submitted["sgst"]="0.00"
        pre = D(D(submitted["taxable_amount"])+tax1+D(submitted["other_charges"]))
        ro = D(pre.quantize(Decimal("1"), rounding=ROUND_HALF_UP)-pre) if D(submitted["round_off"])!=0 else D(0)
        submitted["round_off"]=str(ro); submitted["grand_total"]=str(D(pre+ro))
        f.update(fault_category=f"TAX_{mode}", fault_description=desc, correct_value=old, injected_value=new)
    f["expected_system_action"] = "INFORMATIONAL_ONLY" if check=="4.1" else "REJECT"
    f["expected_rejection_reason"] = REJECTION[check] or "Total discrepancy detected (informational)"
    f["fault_id"] = f"F-{inv['invoice_id']}-{check.replace('.','')}"
    return f

def _recalc_line(ln):
    gross = D(D(ln["unit_price"])*ln["quantity"]); ln["line_discount"]=str(D(gross*ln["discount_pct"]/100)); ln["line_total"]=str(D(gross-D(ln["line_discount"])))
def _recalc_totals(sub, inv):
    t = compute_totals(sub["lines"], D(sub["discount"]), sub["other_charge_lines"], sub["supply_type"]=="INTRA", roundoff=D(inv["round_off"])!=0)
    sub.update(t)

# Order in which faults must be applied so that later checks are computed on the right basis
APPLY_ORDER = ["2.2","4.3","4.4","4.2","1.3","4.1","2.1","1.2","1.4","1.5","3.2","3.3"]

def applicable(check, sub, inv, v, faults):
    """Return True if check is evaluable on this submission."""
    if check in ("1.1","1.2","1.5","2.1","3.1","3.3"): return True
    if check=="1.3": return True
    if check=="1.4": return v["kind"]=="service"
    if check=="2.2": return bool(inv["po_number"]) or v["kind"]=="goods"
    if check=="3.2": return True
    if check=="4.1": return True
    if check=="4.2": return "2.1" not in faults
    if check=="4.3": return v["kind"]=="service" and "1.4" not in faults and "2.1" not in faults
    if check=="4.4": return "1.3" not in faults
    return True
