"""Builds the 300-submission plan: originals + faults, duplicates, non-invoice documents; derives expected results."""
import copy, datetime as dt
from decimal import Decimal
import gen_core as g
from gen_core import D, rng, REF_DATE, COMPANIES, VENDORS, VBY, POBY, WABY, CHECKS, CHECK_NAMES, REJECTION

NONDOC_TYPES = ["RECEIPT"]*8 + ["QUOTATION"]*8 + ["PROFORMA_INVOICE"]*8 + ["CREDIT_NOTE"]*7 + ["PURCHASE_ORDER"]*6 + ["DELIVERY_CHALLAN"]*5 + ["UNREADABLE"]*8
PLAN = {"A": dict(nondoc=17, orig=66, dup=17), "B": dict(nondoc=17, orig=66, dup=17), "C": dict(nondoc=16, orig=67, dup=17)}
TARGET = {"1.2":40,"1.4":34,"1.5":40,"2.1":40,"2.2":40,"3.2":40,"3.3":40,"4.1":36,"4.2":40,"4.3":42,"4.4":40,"1.3":8}
EXCLUSIVE = [("4.3","3.3"), ("2.1","4.4"), ("2.1","2.2"), ("4.2","4.4"), ("1.3","4.4"), ("2.1","4.2"), ("2.1","4.3"), ("1.4","4.3"), ("1.3","2.1"), ("3.2","1.3"), ("4.1","2.1")]

def compatible(check, chosen, sub_inv, v):
    for a, b in EXCLUSIVE:
        if (check==a and b in chosen) or (check==b and a in chosen): return False
    if check in ("1.4","4.3") and v["kind"]!="service": return False
    if check=="2.2" and not (sub_inv["po_number"] or v["kind"]=="goods"): return False
    return True

# ------------------------------------------------------------ build originals
records = []   # each: dict(kind='INVOICE'|'DUPLICATE'|'NONDOC', ...)
seq_by_company = {"A":0,"B":0,"C":0}
def next_id(cid):
    seq_by_company[cid] += 1; return seq_by_company[cid]

for cid, plan in PLAN.items():
    vendors = [v for v in VENDORS if v["company_id"]==cid]
    diffs = copy.copy(g.DIFF_PLAN); rng.shuffle(diffs)
    n_faulty = 0
    for i in range(plan["orig"]):
        v = vendors[i % len(vendors)] if i < len(vendors)*2 else rng.choice(vendors)
        inv = g.new_invoice(cid, v, next_id(cid), diffs[i])
        records.append(dict(kind="INVOICE", inv=inv, sub=copy.deepcopy(inv), faults=[], fault_checks=[], vendor=v, company_id=cid, difficulty=diffs[i]))
originals = [r for r in records if r["kind"]=="INVOICE"]

# ------------------------------------------------------------ assign faults (balanced)
need = dict(TARGET)
n_clean = 45
faulty = originals[:]; rng.shuffle(faulty)
clean_set = set(id(r) for r in faulty[:n_clean]); faulty = faulty[n_clean:]
weights = [1,2,2,3,3,3,4,4]
# pre-allocate the most constrained check (4.3) on service invoices
svc = [r for r in faulty if r["vendor"]["kind"]=="service"]; rng.shuffle(svc)
for r in svc[:TARGET["4.3"]]:
    r["fault_checks"].append("4.3"); need["4.3"] -= 1
for r in faulty:
    k = rng.choice(weights)
    for _ in range(k):
        cands = [c for c, n in need.items() if n > 0 and c not in r["fault_checks"] and compatible(c, r["fault_checks"], r["sub"], r["vendor"])]
        if k == 1 and not r["fault_checks"]:
            late = [c for c in cands if c in ("2.2","3.2","3.3","4.1","4.2","4.4")]
            cands = late or cands
        if not cands: break
        # prefer checks with most remaining need
        cands.sort(key=lambda c: -need[c]); pick = rng.choice(cands[:4])
        if pick=="2.1" and r["vendor"]["kind"]=="service" and rng.random()<0.8: continue
        r["fault_checks"].append(pick); need[pick] -= 1
# fill leftovers
for c, n in list(need.items()):
    tries = 0
    while need[c] > 0 and tries < 5000:
        tries += 1; r = rng.choice(faulty)
        if c in r["fault_checks"] or len(r["fault_checks"])>=6: continue
        if compatible(c, r["fault_checks"], r["sub"], r["vendor"]): r["fault_checks"].append(c); need[c]-=1
assert all(n==0 for n in need.values()), need

for r in faulty:
    ordered = [c for c in g.APPLY_ORDER if c in r["fault_checks"]]
    for c in ordered:
        r["faults"].append(g.inject(r["inv"], c, r["sub"]))

# ------------------------------------------------------------ duplicates
DUP_PATTERNS = ["EXACT_PDF","SAME_NUMBER_DIFFERENT_FILENAME","MINOR_FORMAT_CHANGE","UPLOADED_TWICE","METADATA_CHANGED","SAME_VENDOR_SAME_NUMBER_RERENDERED"]
dup_records = []
for cid, plan in PLAN.items():
    pool = [r for r in originals if r["company_id"]==cid]
    rng.shuffle(pool)
    chosen = pool[:plan["dup"]]
    for j, orig in enumerate(chosen):
        pat = DUP_PATTERNS[j % len(DUP_PATTERNS)]
        d = dict(kind="DUPLICATE", inv=copy.deepcopy(orig["inv"]), sub=copy.deepcopy(orig["sub"]), faults=[], fault_checks=list(orig["fault_checks"]),
                 vendor=orig["vendor"], company_id=cid, difficulty=orig["difficulty"], dup_of=orig["inv"]["invoice_id"], dup_pattern=pat)
        d["fault_checks"].append("3.1")
        d["inv"]["invoice_id"] = d["sub"]["invoice_id"] = f"{COMPANIES[cid]['code']}-INV-{next_id(cid):03d}"
        # inherit faults (documented as inherited)
        for f in orig["faults"]:
            ff = dict(f); ff["invoice_id"]=d["inv"]["invoice_id"]; ff["fault_id"]=f"F-{d['inv']['invoice_id']}-{f['check_id'].replace('.','')}"; ff["fault_description"]="(inherited from duplicated original) "+f["fault_description"]; d["faults"].append(ff)
        d["faults"].append(dict(invoice_id=d["inv"]["invoice_id"], check_id="3.1", check_name="duplicate_detection", fault_id=f"F-{d['inv']['invoice_id']}-31", fault_category=f"DUPLICATE_{pat}",
                                fault_description=f"Resubmission of {orig['inv']['invoice_id']} ({pat.replace('_',' ').lower()})", correct_value=f"unique; original {orig['inv']['invoice_id']}", injected_value=f"duplicate of {orig['inv']['invoice_id']} / invoice no. {orig['sub']['invoice_number']}",
                                expected_system_action="REJECT", expected_rejection_reason=REJECTION["3.1"]))
        dup_records.append(d)
records.extend(dup_records)

# ------------------------------------------------------------ non-invoice documents
nd_types = copy.copy(NONDOC_TYPES); rng.shuffle(nd_types)
ni = 0
for cid, plan in PLAN.items():
    vendors = [v for v in VENDORS if v["company_id"]==cid]
    for i in range(plan["nondoc"]):
        t = nd_types[ni]; ni += 1
        v = rng.choice(vendors)
        base = g.new_invoice(cid, v, next_id(cid), rng.choice(["EASY","EASY","EASY","EASY","MEDIUM","MEDIUM","MEDIUM","MEDIUM","HARD","ADVERSARIAL"]))
        base["document_type"] = t
        rec = dict(kind="NONDOC", inv=base, sub=copy.deepcopy(base), faults=[], fault_checks=["1.1"], vendor=v, company_id=cid, difficulty=base["difficulty"], nondoc_type=t)
        rec["sub"]["document_type"]=t
        rec["faults"].append(dict(invoice_id=base["invoice_id"], check_id="1.1", check_name="extraction_success", fault_id=f"F-{base['invoice_id']}-11", fault_category=f"NONINVOICE_{t}",
                                  fault_description={"RECEIPT":"Payment receipt submitted instead of a tax invoice","QUOTATION":"Quotation submitted instead of a tax invoice","PROFORMA_INVOICE":"Proforma invoice submitted","CREDIT_NOTE":"Credit note submitted where a tax invoice is expected","PURCHASE_ORDER":"Purchase order document submitted","DELIVERY_CHALLAN":"Delivery challan submitted","UNREADABLE":"Document is unreadable (severely degraded scan)"}[t],
                                  correct_value="TAX_INVOICE", injected_value=t, expected_system_action="REJECT", expected_rejection_reason=REJECTION["1.1"]))
        if t != "UNREADABLE":
            rec["fault_checks"].append("1.3")
            rec["faults"].append(dict(invoice_id=base["invoice_id"], check_id="1.3", check_name="tax_compliance", fault_id=f"F-{base['invoice_id']}-13", fault_category=f"NONTAXDOC_{t}",
                                      fault_description=f"{t.replace('_',' ').title()} lacks tax-invoice characteristics", correct_value="Tax Invoice", injected_value=t, expected_system_action="REJECT", expected_rejection_reason=REJECTION["1.3"]))
        records.append(rec)

# ------------------------------------------------------------ submission order & filenames
rng.shuffle(records)
# ensure duplicates come after their originals
order = {r["inv"]["invoice_id"]: i for i, r in enumerate(records)}
def sort_key(r):
    return (r["company_id"], order[r["inv"]["invoice_id"]])
records.sort(key=sort_key)
for cid in PLAN:
    recs = [r for r in records if r["company_id"]==cid]
    ids = {r["inv"]["invoice_id"]: r for r in recs}
    changed = True
    while changed:
        changed = False
        for i, r in enumerate(recs):
            if r["kind"]=="DUPLICATE":
                j = recs.index(ids[r["dup_of"]])
                if j > i: recs.insert(j+1, recs.pop(i)); changed=True; break
    for k, r in enumerate(recs, 1):
        r["submission_seq"] = k
        r["submission_id"] = f"SUB-{cid}-{k:03d}"
        r["submitted_at"] = (dt.datetime(2026,9,3,9,0) + dt.timedelta(minutes=k*rng.randint(3,11))).isoformat()

# ------------------------------------------------------------ derived expected results
def tax_expected(sub, v):
    """Expected tax = stated taxable amount (stated subtotal - discount) apportioned across lines by line value,
    at the statutory rate for each line's HSN/SAC (from the vendor catalog), plus tax on other charges."""
    lines = sub["lines"]; line_sum = sum((D(l["line_total"]) for l in lines), D(0))
    taxable = D(D(sub["subtotal"]) - D(sub["discount"]))
    catalog = {it[1]: it[5] for it in v["items"]}
    exp = D(0)
    for l in lines:
        base = taxable * (D(l["line_total"])/line_sum if line_sum else 1)
        exp += D(D(base) * catalog.get(l["hsn_sac"], l["gst_rate"]) / 100)
    for o in sub["other_charge_lines"]: exp += D(D(o["amount"]) * o["gst_rate"]/100)
    return exp

def derive(r):
    inv, sub, v = r["inv"], r["sub"], r["vendor"]; comp = COMPANIES[r["company_id"]]
    res = {}
    if r["kind"]=="NONDOC":
        res["1.1"]="FAIL"; res["1.3"] = "N_A" if r["nondoc_type"]=="UNREADABLE" else "FAIL"
        for c in CHECKS:
            res.setdefault(c, "N_A")
        return res
    res["1.1"]="PASS"
    res["1.2"]="PASS" if sub["customer_gstin"]==comp["gstin"] and sub["customer_name"]==comp["legal_name"] else "FAIL"
    res["1.3"]="FAIL" if sub.get("tax_compliance_variant") else "PASS"
    if v["kind"]=="service":
        wa = WABY.get(sub["work_authorization_id"])
        ok = bool(wa) and wa["vendor_id"]==v["vendor_id"] and wa["customer_id"]==r["company_id"] and wa["status"]=="ACTIVE"
        res["1.4"]="PASS" if ok else "FAIL"
    else: res["1.4"]="N_A"
    res["1.5"]="FAIL" if sub.get("extra_invoices") else "PASS"
    res["2.1"]="FAIL" if sub.get("itemization_variant") else "PASS"
    if sub["po_number"] or v["kind"]=="goods":
        po = POBY.get(sub["po_number"])
        ok = bool(po) and po["vendor_id"]==v["vendor_id"] and po["customer_id"]==r["company_id"] and po["status"]=="ACTIVE" and po["valid_from"]<=sub["invoice_date"]<=po["valid_to"]
        if ok and v["kind"]=="goods" and not sub.get("itemization_variant"):
            l0 = sub["lines"][0]; ok = l0["quantity"]<=po["approved_quantity"] and D(l0["unit_price"])==D(po["approved_unit_price"])
        res["2.2"]="PASS" if ok else "FAIL"
    else: res["2.2"]="N_A"
    res["3.1"]="FAIL" if r["kind"]=="DUPLICATE" else "PASS"
    gst = sub["vendor_gstin"]
    if sub.get("tax_compliance_variant") in ("MISSING_GSTIN","TITLE_INVOICE_NO_TAX_FIELDS"): res["3.2"]="N_A"
    else: res["3.2"]="PASS" if (gst and g.gstin_valid(gst) and gst==v["gstin"]) else "FAIL"
    res["3.3"]="FAIL" if dt.date.fromisoformat(sub["invoice_date"])>REF_DATE else "PASS"
    # 4.1 informational: stated subtotal - discount + tax + other + round_off vs stated total
    if sub.get("itemization_variant")=="TOTAL_ONLY": res["4.1"]="N_A"
    else:
        exp_total = D(D(sub["subtotal"])-D(sub["discount"])+D(sub["tax_amount"])+D(sub["other_charges"])+D(sub["round_off"]))
        res["4.1"]="INFORMATIONAL_DISCREPANCY" if exp_total!=D(sub["grand_total"]) else "INFORMATIONAL_MATCH"
        r["computed_total"]=str(exp_total)
    # 4.2 lines vs stated subtotal, and per-line qty x price - discount
    if sub.get("itemization_variant"): res["4.2"]="N_A"
    else:
        ok = sum((D(l["line_total"]) for l in sub["lines"]), D(0))==D(sub["subtotal"])
        for l in sub["lines"]:
            if D(D(D(l["unit_price"])*l["quantity"])-D(l["line_discount"]))!=D(l["line_total"]): ok=False
        res["4.2"]="PASS" if ok else "FAIL"
    # 4.3
    if v["kind"]=="service" and res["1.4"]=="PASS" and not sub.get("itemization_variant"):
        wa = WABY[sub["work_authorization_id"]]
        hl = [l for l in sub["lines"] if l["unit"]=="hour"]
        hours = sum(l["quantity"] for l in hl); rate_ok = all(D(l["unit_price"])==D(wa["hourly_rate"]) for l in hl)
        sp_start, sp_end = sub["service_period"].split(" to ")
        ok = hours<=wa["approved_hours"] and rate_ok and sub["project"]==wa["project"] and wa["period_start"]<=sp_start and sp_end<=wa["period_end"]
        res["4.3"]="PASS" if ok else "FAIL"
    else: res["4.3"]="N_A"
    # 4.4
    if sub.get("tax_compliance_variant") or sub.get("itemization_variant"): res["4.4"]="N_A"
    else:
        exp = tax_expected(sub, v); stated = D(sub["tax_amount"])
        intra = sub["place_of_supply"][:2]==v["state"]
        comp_ok = (D(sub["igst"])==0 and abs(D(sub["cgst"])-D(sub["sgst"]))<=D("0.01") and abs(D(sub["cgst"])+D(sub["sgst"])-stated)<=D("0.01")) if intra else (D(sub["cgst"])==0 and D(sub["sgst"])==0 and D(sub["igst"])==stated)
        res["4.4"]="PASS" if (abs(exp-stated)<=D("1.00") and comp_ok) else "FAIL"
        r["computed_tax"]=str(exp)
    return res

for r in records:
    r["expected"] = derive(r)
    # first failing blocking check in pipeline order, and final decision
    ff = next((c for c in CHECKS if c!="4.1" and r["expected"][c]=="FAIL"), None)
    r["first_failing_check"] = ff
    r["expected_final_decision"] = "REJECT" if ff else "ACCEPT"
    r["expected_rejection_reason"] = REJECTION[ff] if ff else ""
    # page count target
    n = r["sub"].get("line_item_count", 1)
    r["target_pages"] = 3 if (n>=10 or (r["difficulty"]=="ADVERSARIAL" and n>=6)) else (2 if (n>=6 or r["difficulty"] in ("HARD",) and n>=4) else 1)

# consistency check: injected faults must be reflected in derived results
for r in records:
    for c in r["fault_checks"]:
        exp = r["expected"][c]
        want = "INFORMATIONAL_DISCREPANCY" if c=="4.1" else "FAIL"
        assert exp==want, (r["inv"]["invoice_id"], c, exp, r["fault_checks"])

if __name__ == "__main__":
    from collections import Counter
    print("records", len(records), Counter(r["kind"] for r in records), Counter(r["company_id"] for r in records))
    for c in CHECKS:
        cnt = Counter(r["expected"][c] for r in records); print(c, CHECK_NAMES[c], dict(cnt))
    print("first failing:", Counter(r["first_failing_check"] for r in records))
    print("difficulty:", Counter(r["difficulty"] for r in records))
    print("faults per faulty original:", Counter(len(r["fault_checks"]) for r in records if r["kind"]=="INVOICE"))
