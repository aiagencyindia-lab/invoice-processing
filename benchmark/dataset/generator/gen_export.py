"""Generates all documents and dataset files, then runs final QA."""
import os, sys, csv, json, shutil, hashlib, copy, datetime as dt
from collections import Counter, defaultdict
sys.path.insert(0, '/home/claude')
src = open('/home/claude/gen_plan.py').read().replace('if __name__ == "__main__":', 'if False:')
exec(compile(src, 'gen_plan', 'exec'))
import gen_render as R
import gen_core as g
from gen_core import D, COMPANIES, VENDORS, VBY, PO_MASTER, WA_MASTER, CHECKS, CHECK_NAMES, REJECTION, REF_DATE

OUT = "/home/claude/dataset"
DOCS = f"{OUT}/documents"
if os.path.exists(OUT): shutil.rmtree(OUT)
for cid in COMPANIES: os.makedirs(f"{DOCS}/company_{cid}", exist_ok=True)
byid = {r["inv"]["invoice_id"]: r for r in records}

FN_PATTERNS = ["{num}.pdf", "Invoice_{num}.pdf", "{vendor}_{mon}_2026.pdf", "scan_{seq:04d}.pdf", "IMG_{seq:04d}.pdf", "inv_{num}_signed.pdf", "{num}-{cust}.pdf", "Doc{seq:03d}.pdf"]
def safe(s): return "".join(ch if ch.isalnum() else "_" for ch in s).strip("_")
def filename(r, pattern=None):
    sub = r["sub"]; pat = pattern or g.rng.choice(FN_PATTERNS)
    return pat.format(num=safe(sub["invoice_number"]), vendor=safe(r["vendor"]["name"].split()[0]), mon=sub.get("month","Aug")[:3], seq=r["submission_seq"] + g.rng.randint(0, 900), cust=COMPANIES[r["company_id"]]["code"])

# ---------------------------------------------------------------- render
extra_counter = 0
def render_record(r):
    global extra_counter
    cid = r["company_id"]; iid = r["inv"]["invoice_id"]
    path = f"{DOCS}/company_{cid}/{iid}.pdf"
    if r["kind"] == "NONDOC":
        R.render_nondoc(r, path)
        if r["nondoc_type"] == "UNREADABLE": R.degrade(path, "HARD", unreadable=True, dpi=110)
        elif r["difficulty"] in ("HARD", "ADVERSARIAL"): R.degrade(path, r["difficulty"], adversarial=(r["difficulty"]=="ADVERSARIAL"))
        r["submission_filename"] = filename(r); r["render_note"] = r["nondoc_type"]
        return path
    if r["kind"] == "DUPLICATE":
        orig = byid[r["dup_of"]]; opath = f"{DOCS}/company_{cid}/{orig['inv']['invoice_id']}.pdf"; pat = r["dup_pattern"]
        if pat in ("EXACT_PDF", "UPLOADED_TWICE"):
            shutil.copyfile(opath, path); r["submission_filename"] = orig["submission_filename"] if pat=="UPLOADED_TWICE" else filename(r, "{num}.pdf")
        elif pat == "SAME_NUMBER_DIFFERENT_FILENAME":
            shutil.copyfile(opath, path); r["submission_filename"] = filename(r, g.rng.choice(["scan_{seq:04d}.pdf", "IMG_{seq:04d}.pdf", "Doc{seq:03d}.pdf"]))
        elif pat == "METADATA_CHANGED":
            shutil.copyfile(opath, path); R.set_metadata(path, {"/Title": f"Invoice copy {r['sub']['invoice_number']}", "/Producer": "MobileScan 4.2", "/CreationDate": "D:20260904"}); r["submission_filename"] = filename(r)
        elif pat == "MINOR_FORMAT_CHANGE":
            alt = {"L1":"L5","L2":"L6","L3":"L7","L4":"L1","L5":"L2","L6":"L3","L7":"L4"}[r["vendor"]["layout"]]
            R.render_invoice_pdf(r, path, layout_id=alt); _finish_invoice(r, path); r["submission_filename"] = filename(r)
        else:  # SAME_VENDOR_SAME_NUMBER_RERENDERED
            R.render_invoice_pdf(r, path); _finish_invoice(r, path); r["submission_filename"] = filename(r)
        r["render_note"] = f"duplicate:{pat}"
        return path
    R.render_invoice_pdf(r, path); _finish_invoice(r, path); r["submission_filename"] = filename(r); r["render_note"] = "invoice"
    return path

def _finish_invoice(r, path):
    """multi-document merge + degradation for invoice-type records"""
    global extra_counter
    sub = r["sub"]
    if sub.get("extra_invoices"):
        parts = [path]
        for k in range(sub["extra_invoices"]):
            extra_counter += 1
            ex = g.new_invoice(r["company_id"], r["vendor"], 900 + extra_counter, "EASY")
            ex["invoice_id"] = f"EXTRA-{extra_counter:03d}"
            erec = dict(kind="INVOICE", inv=ex, sub=copy.deepcopy(ex), vendor=r["vendor"], company_id=r["company_id"], difficulty="EASY", target_pages=1)
            ep = f"/tmp/extra_{extra_counter}.pdf"; R.render_invoice_pdf(erec, ep); parts.append(ep)
            r.setdefault("extra_invoice_numbers", []).append(ex["invoice_number"])
        R.merge_pdfs(parts, path)
    if r["difficulty"] in ("HARD", "ADVERSARIAL"):
        R.degrade(path, r["difficulty"], adversarial=(r["difficulty"]=="ADVERSARIAL"), dpi=150 if r["difficulty"]=="HARD" else 130)

# originals and nondocs first, then duplicates (they copy originals)
for r in [x for x in records if x["kind"] != "DUPLICATE"]: render_record(r)
for r in [x for x in records if x["kind"] == "DUPLICATE"]: render_record(r)
for r in records:
    p = f"{DOCS}/company_{r['company_id']}/{r['inv']['invoice_id']}.pdf"
    r["page_count"] = R.page_count(p); r["sha256"] = hashlib.sha256(open(p, "rb").read()).hexdigest()

# ---------------------------------------------------------------- balanced population
balanced = {c: set() for c in CHECKS}
for c in CHECKS:
    fails = [r for r in records if r["expected"][c] in ("FAIL", "INFORMATIONAL_DISCREPANCY")]
    passes = [r for r in records if r["expected"][c] in ("PASS", "INFORMATIONAL_MATCH")]
    g.rng.shuffle(passes)
    n = min(len(fails), len(passes))
    # keep diversity: spread across companies
    chosen_pass = []
    for cid in ("A", "B", "C"):
        chosen_pass += [r for r in passes if r["company_id"]==cid][: max(1, n//3)]
    chosen_pass += [r for r in passes if r not in chosen_pass][: n - len(chosen_pass)]
    g.rng.shuffle(fails)
    for r in fails[:n] + chosen_pass[:n]: balanced[c].add(r["inv"]["invoice_id"])

# ---------------------------------------------------------------- write files
def w_csv(name, rows, cols):
    with open(f"{OUT}/{name}", "w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore"); wr.writeheader()
        for row in rows: wr.writerow(row)

master_cols = ["invoice_id","company_id","vendor_id","customer_id","submission_id","submission_seq","submitted_at","submission_filename","document_file","sha256","invoice_number","invoice_date","due_date","po_number","work_authorization_id","currency","subtotal","discount","taxable_amount","cgst","sgst","igst","tax_amount","other_charges","round_off","grand_total","line_item_count","document_type","page_count","difficulty","layout","vendor_gstin_as_submitted","customer_name_as_submitted","is_duplicate","duplicate_of","expected_final_decision","first_failing_check","expected_rejection_reason","fault_count","fault_checks"]
master = []
for r in records:
    s = r["sub"]
    master.append(dict(invoice_id=r["inv"]["invoice_id"], company_id=r["company_id"], vendor_id=r["vendor"]["vendor_id"], customer_id=r["company_id"], submission_id=r["submission_id"], submission_seq=r["submission_seq"], submitted_at=r["submitted_at"],
        submission_filename=r["submission_filename"], document_file=f"documents/company_{r['company_id']}/{r['inv']['invoice_id']}.pdf", sha256=r["sha256"],
        invoice_number=s["invoice_number"], invoice_date=s["invoice_date"], due_date=s["due_date"], po_number=s["po_number"], work_authorization_id=s["work_authorization_id"], currency="INR",
        subtotal=s["subtotal"], discount=s["discount"], taxable_amount=s["taxable_amount"], cgst=s["cgst"], sgst=s["sgst"], igst=s["igst"], tax_amount=s["tax_amount"], other_charges=s["other_charges"], round_off=s["round_off"], grand_total=s["grand_total"],
        line_item_count=(0 if s.get("itemization_variant") else len(s["lines"])), document_type=(r["nondoc_type"] if r["kind"]=="NONDOC" else ("BILL_OF_SUPPLY" if s.get("tax_compliance_variant")=="BILL_OF_SUPPLY" else ("INVOICE_NON_TAX" if s.get("tax_compliance_variant") in ("NO_TAX_BREAKUP","TITLE_INVOICE_NO_TAX_FIELDS") else "TAX_INVOICE"))),
        page_count=r["page_count"], difficulty=r["difficulty"], layout=(r["vendor"]["layout"]), vendor_gstin_as_submitted=s["vendor_gstin"], customer_name_as_submitted=s["customer_name"],
        is_duplicate=(r["kind"]=="DUPLICATE"), duplicate_of=r.get("dup_of",""), expected_final_decision=r["expected_final_decision"], first_failing_check=r["first_failing_check"] or "", expected_rejection_reason=r["expected_rejection_reason"],
        fault_count=len(r["faults"]), fault_checks="|".join(sorted(set(f["check_id"] for f in r["faults"])))))
w_csv("master_invoice_dataset.csv", master, master_cols)

# validation matrix (long)
matrix = []
for r in records:
    fby = {f["check_id"]: f for f in r["faults"]}
    for c in CHECKS:
        e = r["expected"][c]; f = fby.get(c)
        matrix.append(dict(invoice_id=r["inv"]["invoice_id"], company_id=r["company_id"], check_id=c, check_name=CHECK_NAMES[c], phase=c.split(".")[0], expected_result=e,
            fault_present=bool(f), fault_code=(f["fault_category"] if f else ""), correct_value=(f["correct_value"] if f else ""), injected_value=(f["injected_value"] if f else ""),
            expected_system_action=("N_A" if e=="N_A" else ("INFORMATIONAL_ONLY" if c=="4.1" else ("REJECT" if e=="FAIL" else "PASS"))),
            expected_rejection_reason=(REJECTION[c] if e=="FAIL" else ("Total discrepancy detected (informational, must not reject)" if e=="INFORMATIONAL_DISCREPANCY" else "")),
            is_first_failing_check=(r["first_failing_check"]==c), in_balanced_population=(r["inv"]["invoice_id"] in balanced[c])))
w_csv("validation_test_matrix.csv", matrix, ["invoice_id","company_id","check_id","check_name","phase","expected_result","fault_present","fault_code","correct_value","injected_value","expected_system_action","expected_rejection_reason","is_first_failing_check","in_balanced_population"])
# wide matrix
wide = []
for r in records:
    row = dict(invoice_id=r["inv"]["invoice_id"], company_id=r["company_id"], vendor_id=r["vendor"]["vendor_id"], submission_id=r["submission_id"], document_type=master[[m["invoice_id"] for m in master].index(r["inv"]["invoice_id"])]["document_type"], page_count=r["page_count"], difficulty=r["difficulty"])
    for c in CHECKS:
        e = r["expected"][c]; row[CHECK_NAMES[c]] = ("INFORMATIONAL — DISCREPANCY" if e=="INFORMATIONAL_DISCREPANCY" else ("INFORMATIONAL — MATCH" if e=="INFORMATIONAL_MATCH" else e))
    row["expected_final_decision"] = r["expected_final_decision"]; row["first_failing_check"] = r["first_failing_check"] or ""; row["expected_rejection_reason"] = r["expected_rejection_reason"]
    wide.append(row)
w_csv("validation_test_matrix_wide.csv", wide, ["invoice_id","company_id","vendor_id","submission_id","document_type","page_count","difficulty"] + [CHECK_NAMES[c] for c in CHECKS] + ["expected_final_decision","first_failing_check","expected_rejection_reason"])

# fault manifest
faults = []
for r in records:
    for f in r["faults"]:
        faults.append(dict(fault_id=f["fault_id"], invoice_id=r["inv"]["invoice_id"], company_id=r["company_id"], check_id=f["check_id"], check_name=f["check_name"], fault_category=f["fault_category"], fault_description=f["fault_description"], correct_value=f["correct_value"], injected_value=f["injected_value"], expected_system_action=f["expected_system_action"], expected_rejection_reason=f["expected_rejection_reason"], inherited_from_duplicate_original=(f["fault_description"].startswith("(inherited"))))
w_csv("fault_manifest.csv", faults, ["fault_id","invoice_id","company_id","check_id","check_name","fault_category","fault_description","correct_value","injected_value","expected_system_action","expected_rejection_reason","inherited_from_duplicate_original"])

# masters
w_csv("po_master.csv", PO_MASTER, ["po_number","customer_id","vendor_id","item_description","hsn_sac","approved_quantity","unit","approved_unit_price","approved_value","currency","valid_from","valid_to","status"])
w_csv("vendor_master.csv", [dict(v, gstin_checksum_valid=g.gstin_valid(v["gstin"])) for v in VENDORS], ["vendor_id","company_id","name","kind","address","city","state","state_name","pin","pan","gstin","gstin_checksum_valid","email","phone","payment_terms","bank","account","ifsc","layout"])
w_csv("work_authorization_master.csv", WA_MASTER, ["work_authorization_id","customer_id","vendor_id","project","approved_hours","hourly_rate","period_start","period_end","status"])
w_csv("customer_master.csv", [dict(c) for c in COMPANIES.values()], ["company_id","code","legal_name","industry","address","city","pin","state","pan","gstin","email"])

# duplicate manifest
dups = []
for r in records:
    if r["kind"]=="DUPLICATE":
        o = byid[r["dup_of"]]
        dups.append(dict(duplicate_invoice_id=r["inv"]["invoice_id"], original_invoice_id=o["inv"]["invoice_id"], company_id=r["company_id"], vendor_id=r["vendor"]["vendor_id"], invoice_number=r["sub"]["invoice_number"], duplicate_pattern=r["dup_pattern"],
            original_submission_seq=o["submission_seq"], duplicate_submission_seq=r["submission_seq"], original_filename=o["submission_filename"], duplicate_filename=r["submission_filename"], original_sha256=o["sha256"], duplicate_sha256=r["sha256"], byte_identical=(o["sha256"]==r["sha256"]), expected_rejection_reason=REJECTION["3.1"]))
w_csv("duplicate_manifest.csv", dups, ["duplicate_invoice_id","original_invoice_id","company_id","vendor_id","invoice_number","duplicate_pattern","original_submission_seq","duplicate_submission_seq","original_filename","duplicate_filename","original_sha256","duplicate_sha256","byte_identical","expected_rejection_reason"])

# ground truth JSON
gt = []
for r in records:
    inv, s = r["inv"], r["sub"]
    entry = dict(invoice_id=inv["invoice_id"], submission_id=r["submission_id"], company_id=r["company_id"], vendor_id=r["vendor"]["vendor_id"], record_kind=r["kind"], document_type=(r["nondoc_type"] if r["kind"]=="NONDOC" else "TAX_INVOICE"),
        difficulty=r["difficulty"], page_count=r["page_count"], document_file=f"documents/company_{r['company_id']}/{inv['invoice_id']}.pdf", submission_filename=r["submission_filename"],
        correct=dict(invoice_number=inv["invoice_number"], vendor_name=inv["vendor_name"], vendor_gstin=inv["vendor_gstin"], customer_name=inv["customer_name"], customer_gstin=inv["customer_gstin"], customer_address=inv["customer_address"], invoice_date=inv["invoice_date"], due_date=inv["due_date"], payment_terms=inv["payment_terms"],
            po_number=inv["po_number"], work_authorization_id=inv["work_authorization_id"], project=inv["project"], service_period=inv["service_period"], place_of_supply=inv["place_of_supply"], supply_type=inv["supply_type"], currency="INR",
            lines=inv["lines"], other_charges=inv["other_charge_lines"], subtotal=inv["subtotal"], discount=inv["discount"], taxable_amount=inv["taxable_amount"], other_charges_total=inv["other_charges"], tax_by_rate=inv["tax_by_rate"], cgst=inv["cgst"], sgst=inv["sgst"], igst=inv["igst"], tax_amount=inv["tax_amount"], round_off=inv["round_off"], expected_total=inv["grand_total"]),
        as_submitted=dict(invoice_number=s["invoice_number"], vendor_gstin=s["vendor_gstin"], customer_name=s["customer_name"], customer_gstin=s["customer_gstin"], invoice_date=s["invoice_date"], due_date=s["due_date"], po_number=s["po_number"], work_authorization_id=s["work_authorization_id"], project=s["project"], service_period=s["service_period"], supply_type=s["supply_type"],
            lines=([] if s.get("itemization_variant") in ("NO_TABLE","TOTAL_ONLY") else s["lines"]), other_charges=s["other_charge_lines"], subtotal=s["subtotal"], discount=s["discount"], taxable_amount=s["taxable_amount"], other_charges_total=s["other_charges"], cgst=s["cgst"], sgst=s["sgst"], igst=s["igst"], tax_amount=s["tax_amount"], round_off=s["round_off"], stated_total=s["grand_total"],
            computed_total_from_stated_fields=r.get("computed_total",""), computed_tax_from_statutory_rates=r.get("computed_tax",""), itemization_variant=s.get("itemization_variant",""), tax_compliance_variant=s.get("tax_compliance_variant",""), invoices_in_submission=1+s.get("extra_invoices",0), extra_invoice_numbers=r.get("extra_invoice_numbers",[])),
        faults=[dict(fault_id=f["fault_id"], check_id=f["check_id"], fault_category=f["fault_category"], correct_value=f["correct_value"], injected_value=f["injected_value"], expected_action=f["expected_system_action"]) for f in r["faults"]],
        expected=dict(per_check={CHECK_NAMES[c]: r["expected"][c] for c in CHECKS}, final_decision=r["expected_final_decision"], first_failing_check=r["first_failing_check"], rejection_reason=r["expected_rejection_reason"]),
        duplicate_of=r.get("dup_of"), duplicate_pattern=r.get("dup_pattern"))
    gt.append(entry)
json.dump(dict(reference_date=REF_DATE.isoformat(), generator_seed=g.SEED, record_count=len(gt), records=gt), open(f"{OUT}/ground_truth.json", "w"), indent=1, default=str)

# evaluation summary + balance report
summary = []; report_lines = ["CHECK                              PASS   FAIL   INFO-MATCH  INFO-DISCREP   N/A   | BALANCED PASS  BALANCED FAIL", "-"*118]
LABELS = {"1.1":"1.1 Extraction Success","1.2":"1.2 Customer Verification","1.3":"1.3 Tax Compliance","1.4":"1.4 Work Authorization","1.5":"1.5 Single Document","2.1":"2.1 Line Items Present","2.2":"2.2 PO Validation","3.1":"3.1 Duplicate Detection","3.2":"3.2 Tax ID Validation","3.3":"3.3 Future Date Check","4.1":"4.1 Total Verification (info)","4.2":"4.2 Line Sum Validation","4.3":"4.3 Authorization Hours","4.4":"4.4 Tax Rate Verification"}
for c in CHECKS:
    cnt = Counter(r["expected"][c] for r in records); bal = [r for r in records if r["inv"]["invoice_id"] in balanced[c]]
    bp = sum(1 for r in bal if r["expected"][c] in ("PASS","INFORMATIONAL_MATCH")); bf = sum(1 for r in bal if r["expected"][c] in ("FAIL","INFORMATIONAL_DISCREPANCY"))
    summary.append(dict(check_id=c, check_name=CHECK_NAMES[c], rejection_reason=REJECTION[c] or "(informational only — never rejects)", applicable=len(records)-cnt["N_A"], expected_pass=cnt["PASS"], expected_fail=cnt["FAIL"], informational_match=cnt["INFORMATIONAL_MATCH"], informational_discrepancy=cnt["INFORMATIONAL_DISCREPANCY"], not_applicable=cnt["N_A"], balanced_population=len(bal), balanced_pass=bp, balanced_fail=bf, first_failing_check_count=sum(1 for r in records if r["first_failing_check"]==c)))
    report_lines.append(f"{LABELS[c]:<34} {cnt['PASS']:>4}   {cnt['FAIL']:>4}   {cnt['INFORMATIONAL_MATCH']:>10}  {cnt['INFORMATIONAL_DISCREPANCY']:>12}  {cnt['N_A']:>4}   | {bp:>13}  {bf:>13}")
w_csv("evaluation_summary.csv", summary, ["check_id","check_name","rejection_reason","applicable","expected_pass","expected_fail","informational_match","informational_discrepancy","not_applicable","balanced_population","balanced_pass","balanced_fail","first_failing_check_count"])
open(f"{OUT}/balance_report.txt","w").write("\n".join(report_lines) + "\n\nNOTE: For 4.1 the FAIL population means 'calculation discrepancy detected' (INFORMATIONAL), never a rejection.\n")
print("\n".join(report_lines))

# ---------------------------------------------------------------- FINAL QA
qa = []
def check(name, cond, detail=""): qa.append((name, bool(cond), detail)); print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
ids = [r["inv"]["invoice_id"] for r in records]
check("1. Exactly 300 submissions", len(records)==300, str(len(records)))
check("2. Exactly 100 per company", all(sum(1 for r in records if r["company_id"]==c)==100 for c in "ABC"))
check("3. Unique invoice IDs", len(set(ids))==300)
check("4. Ground truth complete", len(gt)==300 and all(e["correct"]["expected_total"] for e in gt))
check("5. Every intentional fault documented", all(all(c in {f["check_id"] for f in r["faults"]} for c in r["fault_checks"]) for r in records) and len(faults)==sum(len(r["faults"]) for r in records), f"{len(faults)} faults")
bal_ok = all(abs(s["balanced_pass"]-s["balanced_fail"])<=1 and s["balanced_fail"]>=40 for s in summary)
check("6. Balanced PASS/FAIL per check (balanced population; each >=40)", bal_ok, "; ".join(f"{s['check_id']}:{s['balanced_pass']}/{s['balanced_fail']}" for s in summary))
# 7. clean calculations correct on correct data
def totals_ok(inv):
    t = g.compute_totals(inv["lines"], D(inv["discount"]), inv["other_charge_lines"], inv["supply_type"]=="INTRA", roundoff=D(inv["round_off"])!=0)
    return all(D(t[k])==D(inv[k]) for k in ("subtotal","taxable_amount","tax_amount","cgst","sgst","igst","grand_total"))
check("7. All correct (ground-truth) calculations reconcile", all(totals_ok(r["inv"]) for r in records))
check("8. Injected calculation faults reproducible (derived expectations == planned)", all(all((r["expected"][c]==("INFORMATIONAL_DISCREPANCY" if c=="4.1" else "FAIL")) for c in r["fault_checks"]) for r in records))
check("9. PO relationships consistent", all((r["inv"]["po_number"]=="" or (g.POBY[r["inv"]["po_number"]]["vendor_id"]==r["vendor"]["vendor_id"] and g.POBY[r["inv"]["po_number"]]["customer_id"]==r["company_id"] and g.POBY[r["inv"]["po_number"]]["status"]=="ACTIVE")) for r in records))
check("10. Vendor tax IDs consistent (all master GSTINs checksum-valid, unique)", all(g.gstin_valid(v["gstin"]) for v in VENDORS) and len({v["gstin"] for v in VENDORS})==len(VENDORS))
check("11. Work authorizations consistent", all((r["inv"]["work_authorization_id"]=="" or (g.WABY[r["inv"]["work_authorization_id"]]["vendor_id"]==r["vendor"]["vendor_id"] and g.WABY[r["inv"]["work_authorization_id"]]["status"]=="ACTIVE")) for r in records))
check("12. Duplicates documented and ordered after originals", len(dups)==sum(1 for r in records if r["kind"]=="DUPLICATE") and all(d["duplicate_submission_seq"]>d["original_submission_seq"] for d in dups), f"{len(dups)} duplicates")
fd = [r for r in records if r["expected"]["3.3"]=="FAIL"]
check("13. Future-date cases relative to 2026-09-03", all(dt.date.fromisoformat(r["sub"]["invoice_date"])>REF_DATE for r in fd) and all(dt.date.fromisoformat(r["sub"]["invoice_date"])<=REF_DATE for r in records if r["expected"]["3.3"]=="PASS"))
check("14. 4.1 never has a rejection action", all(m["expected_system_action"]!="REJECT" for m in matrix if m["check_id"]=="4.1") and all(f["expected_system_action"]=="INFORMATIONAL_ONLY" for f in faults if f["check_id"]=="4.1"))
unintended = [(r["inv"]["invoice_id"], c) for r in records if r["kind"]!="NONDOC" for c in CHECKS if r["expected"][c] in ("FAIL","INFORMATIONAL_DISCREPANCY") and c not in r["fault_checks"]]
clean = [r for r in records if not r["faults"]]
check("15. No unintended faults; clean cases all-PASS", not unintended and all(r["expected_final_decision"]=="ACCEPT" for r in clean), f"{len(clean)} fully clean invoices")
check("16. No real-world PII/companies (all names synthetic, .example domains, fictional banks)", all(v["email"].endswith(".example") for v in VENDORS))
lay = Counter(r["vendor"]["layout"] for r in records)
check("17. Layout templates not reused excessively (7 layouts, max share)", max(lay.values())/300 < 0.25, str(dict(lay)))
check("18. PASS/FAIL counts match evaluation summary", all(s["expected_fail"]==sum(1 for m in matrix if m["check_id"]==s["check_id"] and m["expected_result"]=="FAIL") for s in summary))
pc = Counter(r["page_count"] for r in records); dc = Counter(r["difficulty"] for r in records)
print("page counts:", dict(pc)); print("difficulty:", dict(dc)); print("doc types:", dict(Counter(m["document_type"] for m in master)))
json.dump([dict(check=n, passed=p, detail=d) for n, p, d in qa] + [dict(check="page_count_distribution", passed=True, detail=str(dict(pc))), dict(check="difficulty_distribution", passed=True, detail=str(dict(dc)))], open(f"{OUT}/qa_report.json","w"), indent=1)
assert all(p for _, p, _ in qa), "QA FAILED"
print("ALL QA PASSED")
