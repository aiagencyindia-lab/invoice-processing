#!/usr/bin/env python3
"""
General Invoice Processing Multi-Agent System

Domain-independent invoice processing pipeline with configurable validation rules.
Each agent produces intermediate artifacts stored in output/{case_id}/

Version: 1.1.1
Reference: reconstructed_rules_book.md

Usage:
    python general_invoice_agent.py --base-dir path/to/invoices
    python general_invoice_agent.py --case path/to/single/case
    python general_invoice_agent.py --base-dir path/to/invoices --config config.json

Output:
    All outputs are saved in the SCRIPT DIRECTORY (not current working directory):
    general-invoice-processing-agent-gym/
    └── output/
        └── {case_id}/
            ├── 01_classification.json
            ├── 02_extraction.json
            ├── ...
            └── Postprocessing_Data.json

Note:
    - Outputs go to script's folder regardless of where you run it from
    - Config files are resolved relative to script directory if not absolute
    - .env file is loaded from script directory first
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar

# Third-party imports
try:
    import pdfplumber
    from dotenv import load_dotenv
    from pydantic import BaseModel
except ImportError:
    print("Error: Missing required package. Install with:")
    print("pip install google-genai pydantic pdfplumber python-dotenv")
    sys.exit(1)

logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

try:
    from ... import (
        bank_check,
        confidence,
        duplicates,
        extraction_cleanup,
        gemini,
        po_book,
        po_matching,
        tax_ids,
        three_way,
        vendor_master,
        wa_matching,
    )
    from ...llm_guard import CallBudget, LlmUnavailable, guarded_generate
    from ...paths import (
        AGENT_ROOT,
        ITEM_CODE_CACHE_PATH,
        PURCHASE_ORDER_FILE,
    )
    from ...paths import (
        AGENTIC_FLOW_OUT as OUTPUT_BASE_DIR,
    )
    from ...paths import (
        ENV_PATH as ENV_FILE,
    )
except ImportError:  # executed as a script: no parent package context
    import sys as _sys
    from pathlib import Path as _Path

    _repo_root = next(
        p
        for p in _Path(__file__).resolve().parents
        if (p / "invoice_processing").is_dir()
    )
    _sys.path.insert(0, str(_repo_root))
    from invoice_processing import (
        bank_check,
        confidence,
        duplicates,
        extraction_cleanup,
        gemini,
        po_book,
        po_matching,
        tax_ids,
        three_way,
        vendor_master,
        wa_matching,
    )
    from invoice_processing.llm_guard import (
        CallBudget,
        LlmUnavailable,
        guarded_generate,
    )
    from invoice_processing.paths import (
        AGENT_ROOT,
        ITEM_CODE_CACHE_PATH,
        PURCHASE_ORDER_FILE,
    )
    from invoice_processing.paths import (
        AGENTIC_FLOW_OUT as OUTPUT_BASE_DIR,
    )
    from invoice_processing.paths import (
        ENV_PATH as ENV_FILE,
    )


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AGENT_ROOT  # legacy alias

if ENV_FILE.exists():
    load_dotenv(ENV_FILE)
else:
    load_dotenv()  # Fallback to default behavior

# Magic-value constants
_MIN_PDF_CONTENT_LENGTH = 50
_ABN_EXPECTED_LENGTH = 11

# Deterministic generation.
#
# temperature=0 alone does NOT make Gemini reproducible: classifying the same
# line item five times returned CALLOUT twice and LABOUR_AH three times. That
# non-determinism swings eval cases pass/fail between runs, which makes an
# accuracy gate in CI unusable and adds noise to touchless-rate measurement.
# A fixed seed makes identical inputs produce identical output.
_GENERATION_SEED = int(os.getenv("GENERATION_SEED", "42"))


# How long one Vertex call may take before the caller stops waiting. The
# guard enforces it, and gemini.Model also hands it to the SDK as the request
# timeout, so a stalled socket is cut at both layers. Without it a stalled
# request holds the worker until its queue lease expires, gets redelivered,
# stalls again, and
# after three attempts dead-letters an invoice nobody ever rejected. 180s is
# roughly three times the slowest healthy extraction observed.
VERTEX_TIMEOUT_SECONDS = float(os.getenv("VERTEX_TIMEOUT_SECONDS", "180"))


def _generate(model, contents):
    """Every Vertex call in this pipeline. See invoice_processing/llm_guard.py.

    A per-call budget rather than a per-case one: this pipeline makes about
    four calls per invoice and its ceiling is the phase structure, not a
    counter. What it needs is that no single call can hang.
    """
    return guarded_generate(
        model,
        contents,
        CallBudget(
            timeout_seconds=VERTEX_TIMEOUT_SECONDS,
            deadline_seconds=VERTEX_TIMEOUT_SECONDS * 3,
        ),
    )


# Work types, and the words that mean them. Matched as whole words: "pm" as a
# bare substring made "Development" preventative maintenance, and a work type
# on the exempt list is a work type that needs no authorization -- so a
# software invoice exempted itself from the check by spelling.
_WORK_TYPE_KEYWORDS = (
    (
        "PREVENTATIVE",
        ("preventative", "preventive", "pm", "scheduled", "maintenance"),
    ),
    ("CLEANING", ("cleaning", "clean", "janitorial")),
    (
        "EMERGENCY",
        ("emergency", "urgent", "after hours", "callout", "call-out"),
    ),
)


def determine_work_type(invoice: dict) -> str:
    """What kind of work this invoice bills for, from its line descriptions."""
    words = set()
    phrases = []
    for line in invoice.get("line_items") or []:
        text = (line.get("description") or "").lower()
        phrases.append(text)
        words.update(
            "".join(ch if ch.isalnum() else " " for ch in text).split()
        )
    joined = " ".join(phrases)
    for work_type, keywords in _WORK_TYPE_KEYWORDS:
        for keyword in keywords:
            hit = keyword in joined if " " in keyword else keyword in words
            if hit:
                return work_type
    return "REPAIRS"


# One definition, shared with the confidence module's line arithmetic: two
# copies would disagree the way the confidence and 4.2 checks once did.
_line_discount = confidence.line_discount


# One definition, shared with the confidence module's line-sum check: it
# compared lines with the taxable total and flagged every clean invoice that
# prints a charge, while 4.2 netted the charge out.
_charges_and_lines = confidence.charges_and_lines


# Units that price a whole job rather than a counted thing. "1 lot" of
# "Professional services" is a total with a line drawn round it.
_LUMP_SUM_UNITS = {"lot", "lots", "ls", "l/s", "lump sum", "lumpsum", "job"}


def _itemized(invoice: dict) -> tuple[bool, str]:
    """Whether the charges are broken down, and what the reader is shown.

    Counting lines was not enough. A lump sum arrives as one line -- "1 lot,
    Professional services, 2,81,160" (MV-018) -- and a document with no table
    arrives as one line the extractor wrote from the prose ("Charges for the
    period as per engagement letter"), with no quantity and no price. Both
    passed. A breakdown needs at least one line that states what was bought and
    at what rate, and a single line of one lot is not one.
    """
    lines, _ = _charges_and_lines(invoice)
    priced = [
        line
        for line in lines
        if line.get("quantity") is not None
        and line.get("unit_price") is not None
    ]
    if not priced:
        return False, (
            f"Found {len(lines)} line item(s), none with a quantity and a rate"
        )
    if len(lines) == 1:
        line = lines[0]
        unit = str(line.get("unit") or "").strip().lower().rstrip(".")
        if unit in _LUMP_SUM_UNITS and float(line["quantity"]) == 1:
            return False, (
                f"A single lump-sum line: 1 {unit}, "
                f"{line.get('description') or 'undescribed'}"
            )
    return True, f"Found {len(lines)} line item(s)"


def _stated_tax_total(invoice: dict) -> float:
    """The tax the invoice charges, however it chooses to present it.

    A single total where there is one; the sum of the components where the
    jurisdiction splits them and the document states no total. Reading only
    `tax_amount` made every component-split invoice look as though it charged
    no tax at all.
    """
    total = invoice.get("tax_amount")
    if total is not None:
        return total
    components = [
        invoice.get(key) or 0
        for key in ("cgst_amount", "sgst_amount", "igst_amount")
    ]
    return sum(components)


def _statutory_rate(line: dict, config: dict, currency: str) -> float:
    """The rate this line should carry.

    Its classification code first (the tenant's own table), then the rate the
    invoice itself states, then the country default. A tenant with no table
    keeps exactly the behaviour they have: one rate for the whole invoice.
    """
    by_code = config.get("tax_rates_by_hsn") or {}
    code = str(line.get("hsn_sac") or "").strip()
    while code and by_code:
        if code in by_code:
            return float(by_code[code])
        code = code[:-1]  # a code table may be keyed by prefix
    stated = line.get("tax_rate")
    if stated is not None:
        return float(stated)
    return (config.get("tax_rates_by_currency") or {}).get(
        currency, config.get("default_tax_rate", 0.10)
    )


def _expected_tax(invoice: dict, config: dict) -> tuple[float, str] | None:
    """Tax due on this invoice, line by line. None when nothing says how.

    Returns the amount and a description of how it was arrived at, because a
    tax rejection an AP clerk cannot check by hand is a rejection they have to
    take on trust.
    """
    lines, charges = _charges_and_lines(invoice)
    priced = [
        line
        for line in lines
        if line.get("amount_ex_tax") is not None
        and (line.get("hsn_sac") or line.get("tax_rate") is not None)
    ]
    if not priced:
        return None

    currency = str(invoice.get("currency") or "").upper()

    # An invoice-level discount reduces what is taxable, and it is not
    # attributed to any one line -- so each line is taxed on its share of the
    # taxable amount rather than on its printed value. Taxing the printed
    # values instead overstates the tax by the whole discount, which reads as
    # the supplier undercharging when they did nothing of the kind.
    # What the invoice says is taxable, preferring the figures it prints over
    # one derived from them. `invoice_total_ex_tax` is where an extractor's
    # arithmetic errors land -- on one invoice it came back holding the packing
    # charge as well, so the charge was taxed twice and the expected tax
    # overshot by 455.94, exactly 2,533.00 at 18%. Subtotal minus discount is
    # printed on the document and cannot drift that way.
    subtotal = invoice.get("subtotal")
    taxable = invoice.get("invoice_total_ex_tax")
    if subtotal is not None:
        taxable = subtotal - (invoice.get("discount") or 0)
    share = 1.0
    if subtotal and taxable is not None and subtotal > 0:
        share = taxable / subtotal

    total = 0.0
    rates = set()
    for line in lines:
        base = line.get("amount_ex_tax")
        if base is None:
            continue
        rate = _statutory_rate(line, config, currency)
        rates.add(rate)
        total += base * share * rate
    for charge in charges:
        amount = charge.get("amount")
        if amount is None:
            continue
        rate = charge.get("tax_rate")
        if rate is None:
            rate = max(rates) if rates else 0.0
        total += amount * float(rate)
    how = ", ".join(f"{r:.0%}" for r in sorted(rates)) or "no rate"
    return round(total, 2), how


def _components_consistent(invoice: dict) -> str:
    """Are the tax components consistent with where the supply happened?

    Within a state the tax splits in two equal halves; across a state line it
    is charged as one. Getting this wrong is a real filing error that a single
    tax total hides completely. Returns an empty string when consistent, or
    what is wrong.
    """
    cgst = invoice.get("cgst_amount")
    sgst = invoice.get("sgst_amount")
    igst = invoice.get("igst_amount")
    if cgst is None and sgst is None and igst is None:
        return ""

    place = str(invoice.get("place_of_supply") or "")
    supplier = tax_ids.normalize(invoice.get("vendor_tax_id"))
    place_code = "".join(ch for ch in place if ch.isdigit())[:2]
    supplier_code = supplier[:2] if supplier[:2].isdigit() else ""
    if not place_code or not supplier_code:
        return ""

    intra_state = place_code == supplier_code
    cgst, sgst, igst = cgst or 0, sgst or 0, igst or 0
    if intra_state:
        if igst:
            return (
                f"IGST charged on a supply within state {place_code}, where "
                "CGST and SGST apply"
            )
        if abs(cgst - sgst) > 0.02:
            return f"CGST {cgst:,.2f} and SGST {sgst:,.2f} differ"
    elif cgst or sgst:
        return (
            f"CGST/SGST charged on a supply from state {supplier_code} to "
            f"{place_code}, where IGST applies"
        )
    return ""


@lru_cache(maxsize=32)
def _model_for(model_name: str, temperature: float) -> "gemini.Model":
    """One model object per (name, temperature).

    The object is stateless and the configuration is not, so the pair is the
    key. Three call sites built a fresh one per LLM call and two other modules
    kept their own class-level singleton apiece; caching here is the same
    saving without a mutable global to reason about, and it is the one place
    that decides how a model is constructed.

    `lru_cache` rather than a dict: the eviction is the point, so a process
    that sees many model names cannot grow a cache forever.
    """
    return gemini.Model(
        model_name, temperature=temperature, seed=_GENERATION_SEED
    )


@dataclass
class _GCPConfig:
    """Mutable container for GCP configuration (lazy-initialized)."""

    PROJECT_ID: str | None = None
    LOCATION: str = "us-central1"
    GEMINI_FLASH_MODEL: str = "gemini-2.5-flash"
    GEMINI_PRO_MODEL: str = "gemini-2.5-pro"
    API_CALL_DELAY_SECONDS: float = 1.0
    initialized: bool = False


_gcp_config = _GCPConfig(
    LOCATION=os.getenv("LOCATION", "us-central1"),
    GEMINI_FLASH_MODEL=os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash"),
    GEMINI_PRO_MODEL=os.getenv("GEMINI_PRO_MODEL", "gemini-2.5-pro"),
    API_CALL_DELAY_SECONDS=float(os.getenv("API_CALL_DELAY_SECONDS", "1.0")),
)


def _ensure_gcp_initialized():
    """Resolve the GCP settings on first use (not at import time).

    Agent Engine sets env vars after module import, so we must defer. The
    model client itself (invoice_processing/gemini.py) reads the same
    variables when it is first built."""
    if _gcp_config.initialized:
        return
    _gcp_config.PROJECT_ID = (
        os.getenv("PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GOOGLE_CLOUD_PROJECT_ID")
        or os.getenv("GCP_PROJECT")
    )
    _gcp_config.LOCATION = os.getenv("LOCATION") or os.getenv(
        "GOOGLE_CLOUD_REGION", "us-central1"
    )
    _gcp_config.GEMINI_FLASH_MODEL = os.getenv(
        "GEMINI_FLASH_MODEL", "gemini-2.5-flash"
    )
    _gcp_config.GEMINI_PRO_MODEL = os.getenv(
        "GEMINI_PRO_MODEL", "gemini-2.5-pro"
    )
    _gcp_config.API_CALL_DELAY_SECONDS = float(
        os.getenv("API_CALL_DELAY_SECONDS", "1.0")
    )
    if not _gcp_config.PROJECT_ID:
        print("Warning: PROJECT_ID not found in environment")
        print("Set it in .env file or export PROJECT_ID=your-gcp-project-id")
    _gcp_config.initialized = True


# Fallback configuration, used only when master data cannot be loaded.
#
# This used to BE the configuration -- a verbatim second copy of
# `config_defaults` in invoice_master_data.yaml that nothing kept in sync. The
# YAML is now the source of truth (see _load_config_defaults below), which is
# what makes the pipeline genuinely configurable rather than only documented as
# such. This dict remains so the agent still runs if the YAML is missing.
_FALLBACK_CONFIG = {
    # Organization names for customer verification
    "organization_names": ["ACME Corp", "ACME Corporation", "ACME Ltd"],
    # Tax settings
    "default_tax_rate": 0.10,  # 10%
    "tax_rates_by_currency": {
        "AUD": 0.10,
        "NZD": 0.15,
        "USD": 0.00,
        "EUR": 0.20,
    },
    # Validation settings
    "require_work_authorization": False,
    "waf_exempt_work_types": ["PREVENTATIVE", "CLEANING"],
    # PO validation
    "require_purchase_order": False,
    # 2% or $50, whichever is larger. A pure percentage is unhelpful on small
    # POs, where rounding and freight routinely exceed it; a pure absolute is
    # noise on large ones.
    "po_tolerance_pct": 0.02,
    "po_tolerance_abs": 50.0,
    "three_way_qty_tolerance": 0,
    # Duplicate detection
    # On by default. It was shipped off, which meant a pilot ran without any
    # duplicate check at all -- and a duplicate invoice is a duplicate payment,
    # the most expensive routine error in accounts payable. Exact and
    # normalised matches block; a fuzzy match only warns, so legitimate repeat
    # billing (retainers, subscriptions) is not rejected.
    "duplicate_check_enabled": True,
    "duplicate_check_type": "none",  # "none", "file", "database", "api"
    # Statutory rate per classification code (HSN/SAC and its equivalents),
    # for jurisdictions where the rate is a property of what was sold rather
    # than of the invoice. Empty means one rate per currency, as before.
    "tax_rates_by_hsn": {},
    # Tolerances
    "balance_tolerance": 0.02,
    "bank_change_recent_days": 30,
    "line_sum_tolerance": 1.00,
    # Tax reconciliation used to borrow `balance_tolerance`, which is two
    # cents: a per-line rounding rule that is normal in some jurisdictions
    # exceeded it on almost every invoice.
    "tax_rate_tolerance": 1.00,
    # And the same allowance as a share of the tax due, for invoices where a
    # fixed amount is meaningless.
    "tax_rate_tolerance_pct": 0.002,
    "hours_tolerance": 0.5,
    # Tax ID validation
    "validate_tax_id_checksum": True,
    "tax_id_format": "ABN",  # "ABN", "GSTIN", "NONE"
    # Our own tax registrations, checked against the invoice's bill-to when it
    # states one. Empty means the name check stands alone, as before.
    "organization_tax_ids": [],
    # Off: an invoice with no tax on it is not automatically wrong. Where input
    # tax can only be claimed against a proper tax invoice, turning this on
    # makes step 1.3 say so.
    "require_tax_breakup": False,
}


def _load_config_defaults() -> dict[str, Any]:
    """Read config defaults from master data, falling back to the hardcoded set.

    Values from the YAML are layered over the fallback, so a master data file
    that omits a key still yields a complete config.
    """
    try:
        from ..master_data_loader import load_master_data  # noqa: PLC0415

        defaults = load_master_data().get_config_defaults()
        if defaults:
            return {**_FALLBACK_CONFIG, **defaults}
    except ImportError as exc:
        # Run as a script there is no parent package for the relative
        # import; the built-in defaults are the intended fallback there.
        logger.debug("Master data not importable here: %s", exc)
    except Exception as exc:
        logger.warning(
            "Master data unavailable, using built-in defaults: %s", exc
        )
    return dict(_FALLBACK_CONFIG)


DEFAULT_CONFIG = _load_config_defaults()

# Module-level mutable containers for config and metrics
_config_store: dict[str, Any] = {"CONFIG": DEFAULT_CONFIG.copy()}

_metrics_store: dict[str, Any] = {
    "METRICS": {
        "llm_calls": 0,
        "total_tokens": {"prompt": 0, "completion": 0},
        "total_cost_usd": 0.0,
        "agent_breakdown": [],
    }
}


def _get_config() -> dict:
    """Return the current CONFIG dict."""
    return _config_store["CONFIG"]


def _get_metrics() -> dict:
    """Return the current METRICS dict."""
    return _metrics_store["METRICS"]


# Pricing per 1M tokens
MODEL_PRICING = {
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
}


# ============================================================================
# PYDANTIC SCHEMAS
# ============================================================================


# What a submission can turn out to be. The classifier used to answer one
# question -- "does this look like an invoice?" -- so a receipt, a quotation, a
# proforma or a credit note with an invoice-shaped header was extracted and
# validated as an invoice, and step 1.1 reported success on a document that is
# not a payable at all. Naming the kinds is what lets 1.1 and 1.3 disagree with
# each other: a Bill of Supply *is* a real document from a real supplier, it is
# simply not a tax invoice.
TAX_INVOICE = "tax_invoice"
NON_TAX_INVOICE = "invoice"  # an invoice with no tax stated: bill of supply etc
NON_PAYABLE_TYPES = frozenset(
    {
        "receipt",
        "quotation",
        "proforma_invoice",
        "credit_note",
        "purchase_order",
        "goods_receipt",
        "delivery_challan",
        "statement",
        "unreadable",
    }
)
GOODS_RECEIPT = "goods_receipt"
# Not payables, but evidence for the three-way match: when a file holds one of
# these and no invoice, it goes to a person to confirm rather than through
# invoice validation.
SUPPORTING_TYPES = frozenset({"purchase_order", GOODS_RECEIPT})
DOCUMENT_TYPES = (
    TAX_INVOICE,
    NON_TAX_INVOICE,
    *sorted(NON_PAYABLE_TYPES),
    "other",
)


class DocumentContent(BaseModel):
    """Identifies document types present in a file"""

    has_invoice: bool = False
    has_work_authorization: bool = False
    invoice_count: int = 0
    document_type: str = ""
    reasoning: str = ""


class InvoiceLineItem(BaseModel):
    """Single line item from invoice"""

    description: str
    quantity: float | None = None
    unit: str | None = None
    unit_price: float | None = None
    amount_ex_tax: float | None = None
    tax_code: str | None = "TAX"
    tax_amount: float | None = None
    amount_inc_tax: float | None = None
    # The classification code that decides the statutory rate for this line
    # (HSN/SAC in India, and the same idea elsewhere). Without it a multi-rate
    # invoice can only be checked against one blended rate, which is not a
    # check at all.
    hsn_sac: str | None = None
    tax_rate: float | None = None  # 0.18 for 18%
    # Invoices print the discount column as a percentage more often than as an
    # amount, so both are captured rather than one being inferred from the
    # other -- reading "10%" as ten rupees rejected five real invoices.
    line_discount: float | None = None  # a sum of money
    line_discount_pct: float | None = None  # 0.10 for 10%
    has_discount: bool | None = None


class OtherCharge(BaseModel):
    """Freight, insurance, a delivery fee: taxable, and outside the line table.

    They belong to the total but not to the line items, so an invoice carrying
    them reconciles only if they are read separately."""

    description: str | None = None
    amount: float | None = None
    tax_rate: float | None = None


class TaxBreakdownLine(BaseModel):
    """One rate band of an invoice's tax. Multi-rate invoices are common
    outside a single-GST jurisdiction, and a single tax_amount cannot express
    them."""

    rate: float | None = None  # e.g. 0.10 for 10%
    taxable_base: float | None = None
    tax_amount: float | None = None
    tax_code: str | None = None


class InvoiceExtraction(BaseModel):
    """Extracted invoice data.

    Fields below the original set were added for Phase 1: PO number and
    goods-receipt matching, due dates for payment scheduling and the MSME/Payment
    Times clocks, and bank details for the vendor bank-change fraud control that
    the AFP survey makes a buying criterion. Adding them later would mean
    re-extracting the whole backlog, so they are captured now even where nothing
    consumes them yet.
    """

    invoice_number: str | None = "UNKNOWN"
    invoice_date: str | None = ""
    invoice_total_inc_tax: float | None = 0.0
    invoice_total_ex_tax: float | None = 0.0
    tax_amount: float | None = 0.0
    vendor_tax_id: str | None = None
    vendor_name: str | None = "UNKNOWN"
    customer_name: str | None = None
    currency: str | None = "AUD"
    line_items: list[InvoiceLineItem] | None = []

    # --- Added in Phase 1 -------------------------------------------------
    po_number: str | None = None
    due_date: str | None = None
    payment_terms: str | None = None
    vendor_address: str | None = None
    # Bank details as *displayed on the invoice*. Never treated as the vendor's
    # true account: a mismatch against the vendor master is the primary signal
    # for business email compromise.
    bank_account_name: str | None = None
    bank_account_number: str | None = None
    bank_code: str | None = None  # BSB / sort code / routing number
    bank_iban: str | None = None
    tax_breakdown: list[TaxBreakdownLine] | None = []

    # --- Added for the India GST pack -------------------------------------
    #
    # Every one of these is optional and defaults to None, so an invoice from a
    # jurisdiction that does not print them reaches the checks exactly as it
    # did before. What they buy: a reconciliation that accounts for discounts,
    # freight and rounding rather than assuming they are absent; a tax check
    # that can compare against statutory rates per line instead of one blended
    # rate; and a work-authorization check that can look up the reference the
    # invoice quotes rather than requiring the form to be attached.
    document_type: str | None = None
    customer_tax_id: str | None = None
    work_authorization_reference: str | None = None
    project: str | None = None
    service_period_start: str | None = None
    service_period_end: str | None = None
    place_of_supply: str | None = None
    subtotal: float | None = None  # before discount, as printed
    discount: float | None = None
    other_charges: list[OtherCharge] | None = []
    round_off: float | None = None
    # Tax components where the jurisdiction splits them (CGST/SGST within a
    # state, IGST across states). Their consistency with the place of supply is
    # a check of its own: charging IGST on a local supply is a real error that
    # a single tax total hides completely.
    cgst_amount: float | None = None
    sgst_amount: float | None = None
    igst_amount: float | None = None


class WorkAuthorizationExtraction(BaseModel):
    """Extracted work authorization data"""

    reference_number: str | None = None
    site_name: str | None = None
    authorized_hours: float | None = None
    work_description: str | None = None
    technician_name: str | None = None
    date: str | None = None


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================


def load_config(config_path: str | None = None) -> dict:
    """Load configuration from file or use defaults.

    Config path is resolved relative to SCRIPT_DIR if not absolute.
    """
    if config_path:
        config_file = Path(config_path)
        # Resolve relative paths relative to script directory
        if not config_file.is_absolute():
            config_file = SCRIPT_DIR / config_file

        if config_file.exists():
            with open(config_file) as f:
                user_config = json.load(f)
                _config_store["CONFIG"] = {**DEFAULT_CONFIG, **user_config}
                print(f"Loaded config from {config_file}")
        else:
            print(f"Warning: Config file not found: {config_file}")
            _config_store["CONFIG"] = DEFAULT_CONFIG.copy()
    else:
        _config_store["CONFIG"] = DEFAULT_CONFIG.copy()

    return _config_store["CONFIG"]


def get_output_folder(case_id: str) -> Path:
    """Get output folder for a case, create if not exists"""
    output_folder = OUTPUT_BASE_DIR / case_id
    output_folder.mkdir(parents=True, exist_ok=True)
    return output_folder


def extract_pdf_to_markdown(pdf_path: Path) -> str:
    """Extract text from PDF using pdfplumber"""
    markdown_lines = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                markdown_lines.append(f"## Page {page_num}\n")
                text = page.extract_text()
                if text:
                    markdown_lines.append(text)
                    markdown_lines.append("\n---\n")
        return "\n".join(markdown_lines)
    except Exception as e:
        print(f"    Error extracting PDF {pdf_path}: {e}")
        return f"[PDF extraction failed: {e}]"


def extract_pdf_with_gemini(pdf_path: Path) -> str:
    """Extract text from PDF using Gemini multimodal"""
    model = _model_for(_gcp_config.GEMINI_FLASH_MODEL, 0)

    with open(pdf_path, "rb") as f:
        pdf_data = f.read()

    pdf_part = gemini.pdf_part(pdf_data)

    prompt = """Extract ALL text content from this PDF document.
Return the text exactly as it appears, preserving numbers, dates, amounts, and structure.
Do not summarize - extract complete text content."""

    response = _generate(model, [pdf_part, prompt])

    if _gcp_config.API_CALL_DELAY_SECONDS > 0:
        time.sleep(_gcp_config.API_CALL_DELAY_SECONDS)

    return response.text


def extract_pdf_with_fallback(pdf_path: Path) -> tuple[str, str]:
    """Extract PDF with Gemini as default, pdfplumber as fallback"""
    _ensure_gcp_initialized()
    if _gcp_config.PROJECT_ID:
        try:
            content = extract_pdf_with_gemini(pdf_path)
            if content and len(content.strip()) > _MIN_PDF_CONTENT_LENGTH:
                return content, "gemini"
        except LlmUnavailable:
            raise  # the service, not the document: see llm_guard
        except Exception as e:
            print(f"    Gemini extraction failed: {e}, using pdfplumber...")

    content = extract_pdf_to_markdown(pdf_path)
    return content, "pdfplumber"


def _strip_markdown_code_block(text: str) -> str:
    """Strip markdown code-block fences from text, returning the inner content."""
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    json_lines: list[str] = []
    in_block = False
    for line in lines:
        if line.strip().startswith("```"):
            if in_block:
                break
            in_block = True
            continue
        elif in_block:
            json_lines.append(line)
    if json_lines:
        return "\n".join(json_lines).strip()
    return text


def _extract_json_object(text: str) -> str:
    """Find and return the first top-level JSON object in *text*."""
    start_idx = text.find("{")
    if start_idx == -1:
        raise ValueError("No JSON object found")

    brace_count = 0
    end_idx = -1
    for i in range(start_idx, len(text)):
        if text[i] == "{":
            brace_count += 1
        elif text[i] == "}":
            brace_count -= 1
            if brace_count == 0:
                end_idx = i + 1
                break

    if end_idx == -1:
        raise ValueError("Unclosed JSON object")

    return text[start_idx:end_idx]


def clean_json_response(response_text: str) -> str:
    """Clean LLM JSON response - remove markdown, fix trailing commas"""
    text = _strip_markdown_code_block(response_text.strip())
    json_str = _extract_json_object(text)
    # Remove trailing commas
    json_str = re.sub(r",(\s*[}\]])", r"\1", json_str)
    return json_str


def normalize_tax_id(tax_id: str) -> str:
    """Remove all non-numeric characters from tax ID"""
    return re.sub(r"[^0-9]", "", tax_id or "")


def validate_abn_checksum(abn: str) -> tuple[bool, str]:
    """Validate an Australian Business Number checksum.

    Kept as a named function because callers and tests use it; the algorithm
    itself lives in the tax_ids registry so there is one implementation.
    """
    return tax_ids.validate_abn(abn)


def validate_tax_id(tax_id: str, format_type: str = "ABN") -> tuple[bool, str]:
    """Validate a tax ID against the configured format.

    Formats are registered in invoice_processing.tax_ids, so supporting a new
    country means registering a validator rather than editing this pipeline.
    The format comes from `config_defaults.tax_id_format` in the master data.
    """
    return tax_ids.validate(tax_id, format_type)


def clean_extraction(invoice_data: dict, pages: list[str]) -> dict:
    """Mechanical repairs, then the tax-ID checksum on what they produced.

    One step, because the checksum verdict must describe the value check 3.2
    compares: a verdict kept from before cleanup read a GSTIN back from 0 to
    O and still called it invalid. The benchmark replay calls this too, so a
    recorded verdict cannot outlive the value or the setting it was made for.
    """
    invoice_data = extraction_cleanup.clean(
        invoice_data, charges_and_lines=_charges_and_lines, pages=pages
    )
    invoice_data.pop("_tax_id_validation", None)
    if _get_config().get("validate_tax_id_checksum") and invoice_data.get(
        "vendor_tax_id"
    ):
        tax_valid, tax_reason = validate_tax_id(
            invoice_data["vendor_tax_id"],
            _get_config().get("tax_id_format", "ABN"),
        )
        invoice_data["_tax_id_validation"] = {
            "valid": tax_valid,
            "reason": tax_reason,
        }
    return invoice_data


def call_gemini(
    prompt: str,
    model_name: str | None = None,
    response_schema: type[BaseModel] | None = None,
) -> tuple[Any, float]:
    """Call Gemini API with optional structured output"""
    metrics = _get_metrics()
    model_name = model_name or _gcp_config.GEMINI_FLASH_MODEL
    model = _model_for(model_name, 0)

    start_time = time.time()
    response = _generate(model, prompt)
    latency_ms = (time.time() - start_time) * 1000

    # Update metrics
    prompt_tokens, completion_tokens = gemini.token_counts(response)
    metrics["llm_calls"] += 1
    metrics["total_tokens"]["prompt"] += prompt_tokens
    metrics["total_tokens"]["completion"] += completion_tokens

    pricing = MODEL_PRICING.get(
        model_name.split("/")[-1], MODEL_PRICING["gemini-2.5-flash"]
    )
    cost = (prompt_tokens / 1_000_000) * pricing["input"]
    cost += (completion_tokens / 1_000_000) * pricing["output"]
    metrics["total_cost_usd"] += cost

    if _gcp_config.API_CALL_DELAY_SECONDS > 0:
        time.sleep(_gcp_config.API_CALL_DELAY_SECONDS)

    if response_schema:
        json_str = clean_json_response(response.text)
        return response_schema.model_validate_json(json_str), latency_ms

    return response.text, latency_ms


def call_gemini_with_pdf(
    pdf_path: Path,
    prompt: str,
    model_name: str | None = None,
    response_schema: type[BaseModel] | None = None,
) -> tuple[Any, float]:
    """Call Gemini with PDF as input"""
    metrics = _get_metrics()
    model_name = model_name or _gcp_config.GEMINI_PRO_MODEL
    model = _model_for(model_name, 0)

    with open(pdf_path, "rb") as f:
        pdf_data = f.read()

    pdf_part = gemini.pdf_part(pdf_data)

    start_time = time.time()
    response = _generate(model, [pdf_part, prompt])
    latency_ms = (time.time() - start_time) * 1000

    # Update metrics
    prompt_tokens, completion_tokens = gemini.token_counts(response)
    metrics["llm_calls"] += 1
    metrics["total_tokens"]["prompt"] += prompt_tokens
    metrics["total_tokens"]["completion"] += completion_tokens

    pricing = MODEL_PRICING.get(
        model_name.split("/")[-1], MODEL_PRICING["gemini-2.5-flash"]
    )
    cost = (prompt_tokens / 1_000_000) * pricing["input"]
    cost += (completion_tokens / 1_000_000) * pricing["output"]
    metrics["total_cost_usd"] += cost

    if _gcp_config.API_CALL_DELAY_SECONDS > 0:
        time.sleep(_gcp_config.API_CALL_DELAY_SECONDS)

    if response_schema:
        json_str = clean_json_response(response.text)
        return response_schema.model_validate_json(json_str), latency_ms

    return response.text, latency_ms


# The formats invoices actually print. Every one here was found on a real or
# synthetic supplier document; an unparseable date is not a small problem,
# because step 3.3 then emits no verdict at all and a future-dated invoice
# passes unremarked because nobody looked.
_DATE_FORMATS = (
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%d-%b-%Y",  # 09-Aug-2026
    "%d.%m.%Y",  # 09.08.2026
    "%d %B %Y",  # 09 August 2026
    "%d %b %Y",  # 09 Aug 2026
    "%b %d, %Y",  # Aug 09, 2026
)


def _reference_date() -> date:
    """The day "is this invoice dated in the future?" is asked against.

    Normally today. `INVOICE_REFERENCE_DATE` pins it, so a corpus with a stated
    reference date -- an evaluation set, a regression fixture, a replay of last
    month's queue -- gives the same answer whenever it is run.

    Deliberately an environment variable and not a tenant setting: a customer
    who could move the clock could accept invoices dated into next year, which
    is the fraud this check exists to stop. An unreadable value falls back to
    today rather than refusing to process: a typo must not stop the queue, and
    must not quietly accept future dates either.
    """
    raw = os.getenv("INVOICE_REFERENCE_DATE", "").strip()
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            print(
                f"  Warning: INVOICE_REFERENCE_DATE={raw!r} is not YYYY-MM-DD;"
                " using today."
            )
    return date.today()


def parse_date(date_str: str | None) -> date | None:
    """Parse date string in various formats"""
    if not date_str:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


# ============================================================================
# BASE AGENT CLASS
# ============================================================================


class BaseAgent(ABC):
    """Base class for all agents"""

    def __init__(self, name: str, version: str = "1.0"):
        self.name = name
        self.version = version

    def create_output(
        self, data: dict, input_refs: list[str] | None = None
    ) -> dict:
        """Wrap agent output with metadata"""
        return {
            "agent": self.name,
            "version": self.version,
            "timestamp": datetime.now().isoformat(),
            "input_refs": input_refs or [],
            **data,
        }

    def save_artifact(
        self, output_folder: Path, filename: str, data: dict
    ) -> Path:
        """Save artifact to output folder"""
        output_file = output_folder / filename
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  => Saved: {filename}")
        return output_file

    @abstractmethod
    def run(self, *args, **kwargs) -> dict:
        """Execute the agent logic"""
        pass


# ============================================================================
# AGENT 1: CLASSIFIER
# ============================================================================


class ClassifierAgent(BaseAgent):
    """Document Classification Agent"""

    def __init__(self):
        super().__init__("classifier")

    def run(self, source_folder: Path, output_folder: Path) -> dict:
        print(" [1/9] Classifier Agent: Starting...")

        files_info = {}
        summary = {"invoice_count": 0, "waf_count": 0, "document_type": ""}
        # Whether the model or the keyword fallback decided. Only the model
        # may divert a document from the invoice path: the fallback reads the
        # whole page, so an invoice quoting its GRN or PO reads as one.
        self.fell_back = False
        invoice_sources = []
        waf_sources = []

        for file_path in source_folder.iterdir():
            if not file_path.is_file():
                continue

            if file_path.suffix.lower() == ".pdf":
                content, method = extract_pdf_with_fallback(file_path)

                # Analyze content
                doc_info = self._analyze_document(content, file_path.name)

                if doc_info.has_invoice:
                    invoice_sources.append(
                        {
                            "path": file_path.name,
                            "full_path": str(file_path),
                            "content": content,
                        }
                    )
                    summary["invoice_count"] += doc_info.invoice_count

                # The submission's kind is the kind of the document being
                # billed. A work authorization alongside an invoice does not
                # change what is being paid.
                if not summary["document_type"] or doc_info.has_invoice:
                    summary["document_type"] = (
                        doc_info.document_type or summary["document_type"]
                    )

                if doc_info.has_work_authorization:
                    waf_sources.append(
                        {
                            "path": file_path.name,
                            "full_path": str(file_path),
                            "content": content,
                        }
                    )
                    summary["waf_count"] += 1

                files_info[file_path.name] = {
                    "type": "pdf",
                    "extraction_method": method,
                    "has_invoice": doc_info.has_invoice,
                    "has_waf": doc_info.has_work_authorization,
                    "document_type": doc_info.document_type,
                }

            elif file_path.suffix.lower() == ".json":
                files_info[file_path.name] = {"type": "json"}

        files_info["invoice_sources"] = invoice_sources
        files_info["waf_sources"] = waf_sources

        summary["classified_by"] = "keyword" if self.fell_back else "model"
        output = self.create_output(
            {
                "source_folder": source_folder.name,
                "files": files_info,
                "summary": summary,
            }
        )

        self.save_artifact(output_folder, "01_classification.json", output)
        print(
            f"   Found {summary['invoice_count']} invoice(s), {summary['waf_count']} WAF(s)"
        )

        return output

    def _analyze_document(self, content: str, filename: str) -> DocumentContent:
        """Analyze document content to identify type"""
        prompt = f"""Analyze this document and identify what types are present.

DOCUMENT CONTENT:
{content[:8000]}

INVOICE INDICATORS:
- Invoice number field
- Line items table with amounts
- Total amounts (subtotal, tax, total)
- Vendor business details and tax ID

WORK AUTHORIZATION INDICATORS:
- Work authorization/WAF header
- Site attendance records
- Authorized hours
- Technician signatures

DOCUMENT TYPE -- what the document says it is, not what it resembles. Its own
title is the strongest evidence, and a disclaimer such as "this is not a tax
invoice" settles it:
- "tax_invoice": a tax invoice -- titled as one, with a supplier tax
  registration and tax stated or itemised.
- "invoice": billed as an invoice but with no tax registration or no tax
  charged, including a Bill of Supply or a composition-scheme invoice.
- "receipt": records a payment already made.
- "quotation": an offer or estimate, often "valid for N days".
- "proforma_invoice": titled proforma; a request for payment in advance, not a
  tax invoice.
- "credit_note": reverses an earlier invoice.
- "purchase_order": the buyer's own order document.
- "goods_receipt": a goods receipt note (GRN) or material receipt note (MRN)
  recording what the buyer's stores received, usually quoting the purchase
  order. Not a delivery challan, which is the supplier's dispatch note.
- "delivery_challan": a delivery or dispatch note, usually without prices.
- "statement": a statement of account listing several invoices.
- "unreadable": nothing legible could be recovered from the page.
- "other": none of the above.

invoice_count: how many DISTINCT invoices this file contains -- count distinct
invoice numbers, not pages. Several pages of one invoice is 1.

Return ONLY this JSON:
{{
  "has_invoice": true/false,
  "has_work_authorization": true/false,
  "invoice_count": 0-N,
  "document_type": "one of the values above",
  "reasoning": "brief explanation"
}}"""

        try:
            result, _ = call_gemini(
                prompt, _gcp_config.GEMINI_FLASH_MODEL, DocumentContent
            )
            return result
        except LlmUnavailable:
            raise  # the service, not the document: see llm_guard
        except Exception as e:
            print(f"    Warning: Document analysis failed: {e}")
            self.fell_back = True
            return self._classify_by_keyword(content)

    # Document-title phrases and the type each names.
    _TITLE_PHRASES: ClassVar[dict[str, str]] = {
        "proforma invoice": "proforma_invoice",
        "pro forma invoice": "proforma_invoice",
        "pro-forma invoice": "proforma_invoice",
        "proforma tax invoice": "proforma_invoice",
        "pro forma tax invoice": "proforma_invoice",
        "tax invoice": TAX_INVOICE,
        "invoice": NON_TAX_INVOICE,
        "bill of supply": NON_TAX_INVOICE,
        "credit note": "credit_note",
        "debit note": "other",  # no debit-note type; never a payable
        "purchase order": "purchase_order",
        "work order": "other",
        "goods receipt note": GOODS_RECEIPT,
        "goods received note": GOODS_RECEIPT,
        "goods receipt": GOODS_RECEIPT,
        "material receipt note": GOODS_RECEIPT,
        "material receipt": GOODS_RECEIPT,
        "grn": GOODS_RECEIPT,
        "mrn": GOODS_RECEIPT,
        "delivery challan": "delivery_challan",
        "delivery note": "delivery_challan",
        "challan": "delivery_challan",
        "quotation": "quotation",
        "receipt": "receipt",
        "statement of account": "statement",
        "statement": "statement",
    }
    _INVOICE_KINDS: ClassVar[frozenset[str]] = frozenset(
        {TAX_INVOICE, NON_TAX_INVOICE}
    )
    # The documents an invoice is billed "against": on a heading that also
    # names an invoice, the invoice wins over these (and only these).
    _SUPPORTING_KINDS: ClassVar[frozenset[str]] = frozenset(
        {"purchase_order", GOODS_RECEIPT, "delivery_challan"}
    )
    # Longest first, so "goods receipt note" is not read as "goods receipt".
    _PHRASE: ClassVar[str] = "|".join(
        sorted(map(re.escape, _TITLE_PHRASES), key=len, reverse=True)
    )
    _NON_INVOICE_PHRASE: ClassVar[str] = "|".join(
        sorted(
            (
                re.escape(p)
                for p, kind in _TITLE_PHRASES.items()
                if kind not in (TAX_INVOICE, NON_TAX_INVOICE)
            ),
            key=len,
            reverse=True,
        )
    )
    # (a) A clean heading: title phrases alone, joined by / - & "cum" "and"
    # "against". Checked only on a line with no colon and no digit.
    _HEADING: ClassVar[re.Pattern] = re.compile(
        rf"(?:{_PHRASE})(?:(?:\s*[/&-]\s*|\s+(?:cum|and|against)\s+)"
        rf"(?:{_PHRASE}))*"
    )
    _PHRASES_IN: ClassVar[re.Pattern] = re.compile(rf"\b(?:{_PHRASE})\b")
    # (b) A non-payable title: the phrase first, then only a number, a
    # "No."/"#" label, a parenthetical or copy words ("PURCHASE ORDER No.
    # 4471", "GOODS RECEIPT NOTE (GRN)", "CREDIT NOTE No: CN-4").
    _NON_PAYABLE_TITLE: ClassVar[re.Pattern] = re.compile(
        rf"(?P<phrase>{_NON_INVOICE_PHRASE})\b(?:\s*(?:[-/,]|\([a-z ]+\)"
        r"|(?:original|duplicate|triplicate|copy|amendment|amended|revised)\b"
        r"|(?:no\b\.?|number\b|#)\s*[:.]?"
        r"|[a-z]{0,4}[-/]?\d[\w/-]*))*"
    )
    _TITLE_SCAN_LINES: ClassVar[int] = 8
    # A copy marking printed after a heading: "TAX INVOICE (ORIGINAL FOR
    # RECIPIENT)" is still a clean heading once it is dropped.
    _COPY_MARK: ClassVar[re.Pattern] = re.compile(
        r"\s*\((?:original|duplicate|triplicate|copy|for|recipient|supplier"
        r"|transporter|buyer|seller|office|extra|\s)+\)$"
    )

    def _title_kind(self, line: str) -> str | None:
        """The type a title line names, or None for any other line."""
        line = re.sub(r"\s+", " ", line).strip(" \t.,;*[]|_=~'\"")
        if line.startswith("(") and line.endswith(")"):
            line = line[1:-1].strip()
        if not line:
            return None
        heading = self._COPY_MARK.sub("", line)
        if not re.search(r"[:\d]", heading) and self._HEADING.fullmatch(
            heading
        ):
            kinds = [
                self._TITLE_PHRASES[m]
                for m in self._PHRASES_IN.findall(heading)
            ]
            # A non-payable (credit note, proforma...) beats an invoice,
            # which beats the PO/GRN/challan it is billed against.
            return min(
                kinds,
                key=lambda k: (
                    2
                    if k in self._SUPPORTING_KINDS
                    else 1
                    if k in self._INVOICE_KINDS
                    else 0
                ),
            )
        title = self._NON_PAYABLE_TITLE.fullmatch(line)
        if title is None:
            return None
        # A bare "GRN No: 7" is a field label; the abbreviation titles a page
        # only on its own ("GRN", "GRN (ORIGINAL)").
        if title["phrase"] in ("grn", "mrn") and re.search(
            r"[\d#]|\bno\b|\bnumber\b", line
        ):
            return None
        return self._TITLE_PHRASES[title["phrase"]]

    def _classify_by_keyword(self, content: str) -> DocumentContent:
        """What can be told from the text when the model call fails.

        It used to answer "invoice" for anything containing the word, which
        made a quotation a payable. It fails closed: the first title line
        decides, and an untitled page is "other" -- body text cannot tell an
        invoice from an untitled PO, GRN or note quoting "Invoice No". An
        invoice typed "other" is a visible rejection; a PO typed as an invoice
        would be paid.
        """
        lowered = content.lower()
        lines = [line for line in lowered.splitlines() if line.strip()]
        document_type = next(
            filter(
                None, map(self._title_kind, lines[: self._TITLE_SCAN_LINES])
            ),
            None,
        )
        if document_type is None:
            document_type = "unreadable" if not lowered.strip() else "other"
        if document_type == NON_TAX_INVOICE and "gstin" in lowered:
            document_type = TAX_INVOICE
        return DocumentContent(
            has_invoice=document_type in (TAX_INVOICE, NON_TAX_INVOICE),
            has_work_authorization="work authorization" in lowered
            or "waf" in lowered,
            invoice_count=1
            if document_type in (TAX_INVOICE, NON_TAX_INVOICE)
            else 0,
            document_type=document_type,
            reasoning="Keyword fallback",
        )


# ============================================================================
# AGENT 2: EXTRACTOR
# ============================================================================


class ExtractorAgent(BaseAgent):
    """Invoice/WAF Extraction Agent"""

    def __init__(self):
        super().__init__("extractor")

    def run(
        self, source_folder: Path, output_folder: Path, classification: dict
    ) -> dict:
        print(" [2/9] Extractor Agent: Starting...")

        files_info = classification.get("files", {})
        invoice_sources = files_info.get("invoice_sources", [])
        waf_sources = files_info.get("waf_sources", [])

        # Extract invoice
        invoice_data = None
        extraction_failed = False
        extraction_error = None

        if invoice_sources:
            source = invoice_sources[0]
            try:
                invoice_data = self._extract_invoice(Path(source["full_path"]))
                # Mechanical repairs before anything reads the extraction:
                # look-alike Greek letters in identifiers, and charges the
                # model repeated as line items (see extraction_cleanup.py);
                # then the tax-ID checksum on the repaired value.
                invoice_data = clean_extraction(
                    invoice_data,
                    pages=confidence.extract_page_text(
                        Path(source["full_path"])
                    ),
                )

                # Per-field confidence: ground each extracted value in the
                # document's own text layer and cross-check the arithmetic.
                # Deterministic and free -- no extra model call.
                invoice_data["_evidence"] = confidence.build_evidence(
                    invoice_data, Path(source["full_path"])
                )
                routing = confidence.routing_decision(invoice_data["_evidence"])
                invoice_data["_routing"] = routing

                needs_review = invoice_data["_evidence"]["_summary"][
                    "fields_needing_review"
                ]
                print(
                    f"   Extracted invoice: {invoice_data.get('invoice_number', 'N/A')}"
                    f" [{routing}]"
                    + (
                        f" -- review: {', '.join(needs_review)}"
                        if needs_review
                        else ""
                    )
                )
            except LlmUnavailable:
                raise  # the service, not the document: see llm_guard
            except Exception as e:
                extraction_failed = True
                extraction_error = str(e)
                invoice_data = self._empty_invoice()
        else:
            extraction_failed = True
            extraction_error = "No invoice found"
            invoice_data = self._empty_invoice()

        # Extract WAF if present
        waf_data = None
        if waf_sources:
            try:
                waf_data = self._extract_waf(Path(waf_sources[0]["full_path"]))
                print(
                    f"   Extracted WAF: {waf_data.get('authorized_hours', 0)} hours"
                )
            except LlmUnavailable:
                raise  # the service, not the document: see llm_guard
            except Exception as e:
                print(f"   WAF extraction failed: {e}")

        output = self.create_output(
            {
                "invoice": invoice_data,
                "work_authorization": waf_data,
                "extraction_failed": extraction_failed,
                "extraction_error": extraction_error,
                "invoice_count": classification.get("summary", {}).get(
                    "invoice_count", 0
                ),
                "waf_count": classification.get("summary", {}).get(
                    "waf_count", 0
                ),
                # What the classifier decided this submission is. Checks 1.1
                # and 1.3 read it: whether a document is payable at all, and
                # whether it is a tax invoice, are different questions and a
                # single "looks like an invoice" boolean answered neither.
                "document_type": classification.get("summary", {}).get(
                    "document_type", ""
                ),
            },
            input_refs=["01_classification.json"],
        )

        self.save_artifact(output_folder, "02_extraction.json", output)
        return output

    def _extract_invoice(self, pdf_path: Path) -> dict:
        """Extract structured invoice data from PDF"""
        prompt = """Extract structured data from this invoice PDF.

ONE INVOICE ONLY. This file may contain more than one invoice -- several
invoices scanned or merged into one PDF, each with its own invoice number,
bill-to block and line table. Extract the FIRST invoice only: the one that
begins on the first page. Every field, including every line item and every
total, must come from that one invoice. Never combine a header from one
invoice with lines or totals from another, and never add a later invoice's
line items to the first invoice's.

Return ONLY valid JSON with these fields:
{
  "invoice_number": "string",
  "invoice_date": "YYYY-MM-DD",
  "invoice_total_inc_tax": decimal,
  "invoice_total_ex_tax": decimal (the total BEFORE tax: after any invoice-level discount, and INCLUDING other charges such as freight or packing),
  "tax_amount": decimal,
  "vendor_tax_id": "string (tax ID/ABN)",
  "vendor_name": "string",
  "customer_name": "string (who invoice is addressed to)",
  "currency": "AUD/USD/EUR/etc",
  "po_number": "string or null (the buyer's PURCHASE ORDER number only -- see PO below)",
  "due_date": "YYYY-MM-DD or null",
  "payment_terms": "string or null (e.g. 'Net 30 days')",
  "vendor_address": "string or null",
  "bank_account_name": "string or null",
  "bank_account_number": "string or null",
  "bank_code": "string or null (BSB, sort code or routing number)",
  "bank_iban": "string or null",
  "document_type": "string or null (what the document calls itself, e.g. 'Tax Invoice', 'Bill of Supply')",
  "customer_tax_id": "string or null (the tax registration of the party billed)",
  "work_authorization_reference": "string or null (work authorization / work order / WA reference quoted on the invoice)",
  "project": "string or null",
  "service_period_start": "YYYY-MM-DD or null",
  "service_period_end": "YYYY-MM-DD or null",
  "place_of_supply": "string or null (as printed, e.g. '29-Karnataka')",
  "subtotal": decimal or null (the stated sub total, before any discount),
  "discount": decimal or null (an invoice-level discount, as a positive number),
  "round_off": decimal or null (a rounding adjustment; negative if it reduces the total),
  "cgst_amount": decimal or null,
  "sgst_amount": decimal or null,
  "igst_amount": decimal or null,
  "other_charges": [
    {
      "description": "string or null (freight, insurance, packing)",
      "amount": decimal or null,
      "tax_rate": decimal or null (0.18 for 18%)
    }
  ],
  "tax_breakdown": [
    {
      "rate": decimal or null (0.10 for 10%),
      "taxable_base": decimal or null,
      "tax_amount": decimal or null,
      "tax_code": "string or null"
    }
  ],
  "line_items": [
    {
      "description": "string",
      "quantity": decimal or null,
      "unit": "string or null (hour, nos, box, kg)",
      "unit_price": decimal or null,
      "amount_ex_tax": decimal,
      "tax_code": "TAX/GST/NA",
      "tax_amount": decimal or null,
      "hsn_sac": "string or null (HSN or SAC classification code)",
      "tax_rate": decimal or null (0.18 for an 18% line),
      "line_discount": decimal or null (a discount shown as an AMOUNT of money, positive),
      "line_discount_pct": decimal or null (a discount shown as a PERCENTAGE, as a fraction: 0.10 for 10%),
      "has_discount": true/false (does this line show any discount at all, in either form?)
    }
  ]
}

Extract ALL line items. Use null for missing optional fields.

Transcribe values exactly as printed; do not compute, correct or infer them. If
the document's own arithmetic is wrong, report what it says -- a downstream
check compares the stated figures against the expected ones, and silently
correcting them hides the error.

PO: only a reference the invoice labels as a purchase order -- "PO", "P.O.
No.", "Purchase Order", "Order No.". A quotation, quote, estimate or proposal
reference ("Quotation Ref. Q-4179") is the supplier's offer, not the buyer's
order, and a work authorization or work order reference belongs in
work_authorization_reference. If no purchase order is printed, po_number is
null -- never fill it from another reference.

Bank details: report only what appears on this invoice. They are compared
against the vendor's records to detect payment-redirection fraud, so a value
inferred rather than read defeats the control.

Discounts: the discount column is a percentage on most layouts and an amount
on some. Put a percentage in line_discount_pct and an amount in line_discount,
never the same number in both, and set has_discount whenever the line shows one
-- a discount we can see but cannot quantify must not read as no discount.

other_charges: freight, packing, insurance and similar, as printed outside the
line-item table. Do not also repeat them as line items: the same charge in both
places is counted twice.

tax_breakdown: one entry per distinct tax rate shown. Leave empty if the
invoice states a single tax amount with no rate breakdown.

Tax components: where the invoice splits its tax into separate components
(CGST and SGST within a state, IGST across states, or an equivalent split
elsewhere), report each. Leave them null where the invoice states one tax
amount. tax_amount stays the total of the tax charged either way.

Percentages as decimals: an 18% line is 0.18.

Null means "the invoice does not state it". Do not compute a field from the
others -- a subtotal you calculated hides the discrepancy a later check exists
to find."""

        result, _ = call_gemini_with_pdf(
            pdf_path, prompt, _gcp_config.GEMINI_PRO_MODEL, InvoiceExtraction
        )
        return result.model_dump()

    def _extract_waf(self, pdf_path: Path) -> dict:
        """Extract work authorization data from PDF"""
        prompt = """Extract work authorization data from this PDF.

Return ONLY valid JSON:
{
  "reference_number": "string or null",
  "site_name": "string or null",
  "authorized_hours": decimal or null,
  "work_description": "string or null",
  "technician_name": "string or null",
  "date": "YYYY-MM-DD or null"
}"""

        result, _ = call_gemini_with_pdf(
            pdf_path,
            prompt,
            _gcp_config.GEMINI_PRO_MODEL,
            WorkAuthorizationExtraction,
        )
        return result.model_dump()

    def _empty_invoice(self) -> dict:
        """Return empty invoice structure"""
        return {
            "invoice_number": "EXTRACTION_FAILED",
            "invoice_date": "",
            "invoice_total_inc_tax": 0.0,
            "invoice_total_ex_tax": 0.0,
            "tax_amount": 0.0,
            "vendor_tax_id": "",
            "vendor_name": "UNKNOWN",
            "customer_name": None,
            "currency": "AUD",
            "line_items": [],
        }


# ============================================================================
# AGENT 3: PHASE 1 VALIDATOR
# ============================================================================


def _fold_name(value) -> str:
    """A company name reduced to what identifies it.

    Suppliers write the same organisation as "Enew Bharat", "Enewbharat",
    "ENEW  BHARAT" and "E-New Bharat", and step 1.2 is a substring test -- so
    a single space between the invoice and the configured name rejected an
    invoice that was correctly addressed. Comparing letters and digits only
    means an administrator lists the company once instead of guessing every
    spelling a supplier might use.

    It does not make the match looser in any way that matters: it still has to
    contain the whole configured name, so "Acme" does not match "Beacme Ltd"
    any less than it did before.
    """
    return "".join(c for c in str(value or "").lower() if c.isalnum())


def _settle(validations: list[dict]) -> list[dict]:
    """Mark the steps this tenant has switched off, and return the failures.

    A skipped step is recorded as skipped, never as passed: a check nobody ran
    has no verdict, and writing one down would make the decision claim
    something that was never established -- which is the opposite of what an
    auditable system is for.

    This also collapses four copies of the failure expression that had already
    drifted: only one of them excluded informational steps, so an
    informational check could reject an invoice in three of the four phases.
    """
    from ...checks import disabled_from  # noqa: PLC0415

    disabled = disabled_from(_get_config())
    for entry in validations:
        if entry.get("step") in disabled:
            entry["skipped"] = True
            entry["passed"] = None
            entry["rejection_template"] = None
            entry["evidence"] = (
                "Skipped: switched off in settings. "
                f"{entry.get('evidence') or ''}"
            ).strip()

    return [
        v
        for v in validations
        if v.get("passed") is False and not v.get("informational")
    ]


class Phase1ValidatorAgent(BaseAgent):
    """Phase 1: Initial Intake Validation"""

    def __init__(self):
        super().__init__("phase1_validator")

    def run(self, output_folder: Path, extraction: dict) -> dict:
        print(" [3/9] Phase 1 Validator: Starting...")

        invoice = extraction.get("invoice", {})
        validations = []

        # Step 1.1: Extraction Success.
        #
        # "Could this be read?" and "is this a payable document?" were one
        # question, answered by whether the extractor returned anything. A
        # receipt, a quotation or a delivery note with an invoice-shaped header
        # extracted perfectly and passed -- and then went on to be validated,
        # and possibly paid, as an invoice.
        document_type = str(extraction.get("document_type") or "").lower()
        if document_type in NON_PAYABLE_TYPES:
            validations.append(
                {
                    "step": "1.1",
                    "rule": "Invoice extraction must succeed",
                    "passed": False,
                    "evidence": (
                        f"Document is a {document_type.replace('_', ' ')}, "
                        "not an invoice"
                    ),
                    "outcome": document_type,
                    "rejection_template": "Document is not a valid Tax Invoice",
                }
            )
        elif extraction.get("extraction_failed"):
            validations.append(
                {
                    "step": "1.1",
                    "rule": "Invoice extraction must succeed",
                    "passed": False,
                    "evidence": extraction.get(
                        "extraction_error", "Extraction failed"
                    ),
                    "rejection_template": "Document is not a valid Tax Invoice",
                }
            )
        else:
            validations.append(
                {
                    "step": "1.1",
                    "rule": "Invoice extraction must succeed",
                    "passed": True,
                    "evidence": f"Extracted invoice {invoice.get('invoice_number')}",
                }
            )

        # Step 1.2: Customer Verification
        customer_name = _fold_name(invoice.get("customer_name"))
        org_names = [
            _fold_name(n)
            for n in _get_config().get("organization_names", [])
            if _fold_name(n)
        ]
        customer_match = (
            any(org in customer_name for org in org_names)
            if org_names
            else True
        )

        # A name can be typed; a tax registration is issued. Where the customer
        # has told us theirs and the invoice states one, they must agree --
        # otherwise an invoice addressed to a similarly-named company passes on
        # the strength of a substring.
        our_tax_ids = {
            tax_ids.normalize(t)
            for t in (_get_config().get("organization_tax_ids") or [])
            if tax_ids.normalize(t)
        }
        stated_customer_id = tax_ids.normalize(invoice.get("customer_tax_id"))
        evidence = f"Customer: {invoice.get('customer_name')}"
        if our_tax_ids and stated_customer_id:
            if stated_customer_id in our_tax_ids:
                evidence += f", tax ID {invoice.get('customer_tax_id')} matches"
            else:
                # Only when the identifier we read is well-formed. On a scanned
                # invoice the extractor read 24AAECF9034MIZE where the document
                # says 24AAECF9034M1ZE -- a 1 read as an I -- and an exact
                # comparison then rejected the invoice for being addressed to
                # someone else. That is a rejection caused by our own reading,
                # and the checksum is what tells the two apart: a misread
                # identifier fails it, and one that passes and still differs
                # really is another company's.
                readable, reason = tax_ids.validate(
                    stated_customer_id, _get_config().get("tax_id_format")
                )
                if readable:
                    customer_match = False
                    evidence += (
                        f", tax ID {invoice.get('customer_tax_id')} is not one "
                        "of this organisation's"
                    )
                else:
                    evidence += (
                        f", tax ID {invoice.get('customer_tax_id')} could not "
                        f"be read ({reason}); the name alone was checked"
                    )

        validations.append(
            {
                "step": "1.2",
                "rule": "Invoice addressed to organization",
                "passed": customer_match,
                "evidence": evidence,
                "rejection_template": None
                if customer_match
                else "Invoice addressed to different company",
            }
        )

        # Step 1.3: Tax Compliance
        has_tax_id = bool(invoice.get("vendor_tax_id"))
        has_date = bool(invoice.get("invoice_date"))
        has_vendor = bool(
            invoice.get("vendor_name")
            and invoice.get("vendor_name") != "UNKNOWN"
        )
        tax_compliant = has_tax_id and has_date and has_vendor
        evidence = (
            f"Tax ID: {has_tax_id}, Date: {has_date}, Vendor: {has_vendor}"
        )

        # A Bill of Supply is a real document from a real supplier with a real
        # date -- and it is not a tax invoice, so no input tax can be claimed
        # against it. The presence of three fields cannot tell the difference;
        # the document's own kind and whether it states any tax can. Gated,
        # because plenty of jurisdictions and plenty of suppliers legitimately
        # invoice with no tax at all.
        if (
            _get_config().get("require_tax_breakup")
            and document_type == NON_TAX_INVOICE
        ):
            tax_compliant = False
            evidence += ", billed without a tax registration"

        validations.append(
            {
                "step": "1.3",
                "rule": "Tax compliance (tax ID, date, vendor)",
                "passed": tax_compliant,
                "evidence": evidence,
                "rejection_template": None
                if tax_compliant
                else "Document is not a Tax Invoice",
            }
        )

        # Step 1.4: Work Authorization Check.
        #
        # Two ways an authorisation can be established, because customers work
        # both ways. Where the tenant has loaded what they authorised, the
        # invoice's quoted reference is looked up against it -- which is what
        # most invoices carry, and what the form-counting path could never see.
        # Where they have not, the check is what it was: is a form attached?
        if _get_config().get("require_work_authorization"):
            authorizations = _load_work_authorizations()
            match = wa_matching.match_invoice_to_wa(
                invoice, authorizations, vendors=_load_vendors()
            )
            has_waf = (extraction.get("waf_count") or 0) > 0

            if match.outcome == wa_matching.NOT_APPLICABLE:
                # Nothing to check, and nothing to record: a verdict on a
                # question that does not arise is noise in the audit trail.
                pass
            elif match.outcome == wa_matching.NOT_CONFIGURED:
                work_type = self._determine_work_type(invoice)
                exempt_types = [
                    t.upper()
                    for t in _get_config().get("waf_exempt_work_types", [])
                ]
                if work_type not in exempt_types:
                    validations.append(
                        {
                            "step": "1.4",
                            "rule": "Work authorization required",
                            "passed": has_waf,
                            "evidence": f"WAF required for {work_type}, WAF present: {has_waf}",
                            "rejection_template": None
                            if has_waf
                            else "Missing Work Authorization Form",
                        }
                    )
            else:
                # An attached form still satisfies the check: a tenant who
                # sends the paperwork should not be told it is missing because
                # they have also started loading a master.
                passed = match.ok or has_waf
                validations.append(
                    {
                        "step": "1.4",
                        "rule": "Work authorization valid for this supplier",
                        "passed": passed,
                        "evidence": (
                            match.evidence
                            + (
                                " (work authorization form attached)"
                                if has_waf and not match.ok
                                else ""
                            )
                        ),
                        "outcome": match.outcome,
                        "rejection_template": None
                        if passed
                        else "Missing Work Authorization Form",
                    }
                )

        # Step 1.5: Single Invoice Check
        invoice_count = extraction.get("invoice_count", 1)
        validations.append(
            {
                "step": "1.5",
                "rule": "Single invoice per submission",
                "passed": invoice_count <= 1,
                "evidence": f"Found {invoice_count} invoice(s)",
                "rejection_template": None
                if invoice_count <= 1
                else "Multiple invoices in submission",
            }
        )

        # Determine decision
        failed = _settle(validations)
        decision = "REJECT" if failed else "CONTINUE"

        output = self.create_output(
            {
                "phase": 1,
                "validations": validations,
                "decision": decision,
                "rejection_template": failed[0].get("rejection_template")
                if failed
                else None,
                "rejection_reason": failed[0].get("evidence")
                if failed
                else None,
            },
            input_refs=["02_extraction.json"],
        )

        self.save_artifact(output_folder, "03_phase1_validation.json", output)
        print(f"   Phase 1: {decision}")
        return output

    def _determine_work_type(self, invoice: dict) -> str:
        """Determine work type from invoice content"""
        return determine_work_type(invoice)


# ============================================================================
# AGENT 4: PHASE 2 VALIDATOR
# ============================================================================


class Phase2ValidatorAgent(BaseAgent):
    """Phase 2: Content Validation"""

    def __init__(self):
        super().__init__("phase2_validator")

    def run(self, output_folder: Path, extraction: dict, phase1: dict) -> dict:
        print(" [4/9] Phase 2 Validator: Starting...")

        invoice = extraction.get("invoice", {})
        validations = []

        # Step 2.1: Line Items Present
        itemized, evidence = _itemized(invoice)
        validations.append(
            {
                "step": "2.1",
                "rule": "Invoice charges are itemized",
                "passed": itemized,
                "evidence": evidence,
                "rejection_template": None
                if itemized
                else "Invoice charges not itemized",
            }
        )

        # Step 2.2: two-way match against the purchase order.
        #
        # Runs whenever the invoice quotes a PO, not only when POs are
        # mandatory: an invoice that names a purchase order is asserting a
        # commitment, and checking that assertion is worthwhile even where a PO
        # is optional. `require_purchase_order` decides whether a *missing* one
        # is a failure.
        config = _get_config()
        if config.get("require_purchase_order") or invoice.get("po_number"):
            match = po_matching.match_invoice_to_po(
                invoice,
                _load_purchase_orders(),
                required=bool(config.get("require_purchase_order")),
                tolerance_pct=config.get(
                    "po_tolerance_pct", po_matching.DEFAULT_TOLERANCE_PCT
                ),
                tolerance_abs=config.get(
                    "po_tolerance_abs", po_matching.DEFAULT_TOLERANCE_ABS
                ),
                # So the match knows whether this supplier delivers goods, and
                # therefore whether the order's quantity and price are the
                # controls or the work authorization's are.
                vendors=_load_vendors(),
            )
            entry = po_matching.to_validation(match)
            if not entry["passed"]:
                print(f"   PO match: {entry['evidence']}")
            validations.append(entry)

            # Step 2.3: line-level three-way match, only once 2.2 has found
            # the PO and passed it. Holds, never rejects -- see three_way.
            if entry["passed"] and invoice.get("po_number"):
                step = _three_way_step(invoice, config)
                if not step["passed"]:
                    # Same reopening of the extraction-time routing as step
                    # 3.4, re-saved for every reader that takes routing from
                    # the artifact.
                    step["routing_before"] = invoice.get("_routing")
                    invoice["_routing"] = "REVIEW"
                    summary = invoice.setdefault("_evidence", {}).setdefault(
                        "_summary", {}
                    )
                    needing = summary.setdefault("fields_needing_review", [])
                    needing.extend(
                        f"receipt: {s}"
                        for s in step["signals"]
                        if f"receipt: {s}" not in needing
                    )
                    self.save_artifact(
                        output_folder, "02_extraction.json", extraction
                    )
                validations.append(step)

        # Determine decision
        failed = _settle(validations)
        decision = "REJECT" if failed else "CONTINUE"

        output = self.create_output(
            {
                "phase": 2,
                "validations": validations,
                "decision": decision,
                "rejection_template": failed[0].get("rejection_template")
                if failed
                else None,
            },
            input_refs=["02_extraction.json", "03_phase1_validation.json"],
        )

        self.save_artifact(output_folder, "04_phase2_validation.json", output)
        print(f"   Phase 2: {decision}")
        return output


# ============================================================================
# AGENT 5: PHASE 3 VALIDATOR (External)
# ============================================================================


# The file book lives in po_book with the database one; the old name stays for
# the callers and tests that construct it directly.
_PurchaseOrderFile = po_book.FileBook


def _load_purchase_orders() -> "po_book.PurchaseOrderBook":
    """This tenant's purchase orders: the database book the worker injected,
    else the tenant's file (`purchase_order_file`, which the benchmark replay
    sets directly), else the shared legacy file for a direct CLI run."""
    config = _get_config()
    return config.get("purchase_order_book") or po_book.FileBook(
        config.get("purchase_order_file") or PURCHASE_ORDER_FILE
    )


def _three_way_base() -> dict:
    return {
        "step": "2.3",
        "rule": "Invoice lines match PO lines and goods received",
        "passed": True,
        "informational": True,
        "evidence": "",
        "signals": [],
        "lines": [],
        "rejection_template": None,
    }


def three_way_entry(invoice: dict, orders, config: dict, vendors) -> dict:
    """The step 2.3 entry for an invoice whose PO step 2.2 has passed.

    One implementation for the pipeline and the re-match job (rematch.py).
    Raises when the book or the vendors cannot be read: the pipeline holds
    that as "unchecked", the re-match job retries it.
    """
    from ...checks import disabled_from  # noqa: PLC0415

    step = _three_way_base()
    if "2.3" in disabled_from(config):
        # Recorded so _settle can mark it skipped; nothing is read or held.
        return step
    book = orders.book(invoice["po_number"])
    if book is None or not book.get("lines"):
        # The matcher would call every line unpaired against no lines.
        return {**step, "evidence": "No PO lines on file; header match only."}
    vendor = vendors.find(invoice.get("vendor_name"))
    result = three_way.match(
        invoice.get("line_items") or [],
        book["lines"],
        book["received"],
        book["billed"],
        # Unknown kind is treated as receipt-required by the matcher.
        vendor_kind=(vendor.kind or None) if vendor else None,
        qty_tol=float(config.get("three_way_qty_tolerance") or 0),
        price_tol_pct=config.get(
            "po_tolerance_pct", po_matching.DEFAULT_TOLERANCE_PCT
        ),
        price_tol_abs=config.get(
            "po_tolerance_abs", po_matching.DEFAULT_TOLERANCE_ABS
        ),
        grn_numbers=book.get("grn_numbers"),
    )
    return {
        **step,
        "passed": result.passed,
        "evidence": "; ".join(result.evidence)
        or "Every line is paired, received and priced within the order.",
        "signals": list(result.signals),
        "lines": [line.to_dict() for line in result.lines],
    }


def _three_way_step(invoice: dict, config: dict) -> dict:
    """Step 2.3 in the pipeline, where a book that cannot be read holds."""
    try:
        return three_way_entry(
            invoice, _load_purchase_orders(), config, _load_vendors()
        )
    except Exception as exc:
        # A book that cannot be read holds; it never passes silently.
        return {
            **_three_way_base(),
            "passed": False,
            "evidence": f"Purchase-order lines could not be checked: {exc}",
            "signals": [three_way.UNCHECKED],
        }


def _load_vendors() -> "vendor_master.VendorMaster":
    """This tenant's supplier list, injected through config by the worker.

    Records come from the database where there is one (the API and the worker
    are different containers, so a file written by one is invisible to the
    other); the file path is the single-process fallback the benchmark and the
    local stack still use.

    File records that carry bank fields are trusted as verified, with no
    approval queue behind them. That path exists for the benchmark harness and
    single-process dev; a database deployment always loads from the table.
    """
    config = _get_config()
    records = config.get("vendor_master_records")
    if records:
        return vendor_master.VendorMaster(records=list(records))
    return vendor_master.VendorMaster(config.get("vendor_master_file"))


def _load_work_authorizations() -> "wa_matching.WorkAuthorizations":
    """What this tenant has authorised, injected through config by the worker."""
    return wa_matching.WorkAuthorizations(
        _get_config().get("work_authorization_file")
    )


def _labour_keywords() -> tuple[str, ...]:
    """Words that mean an hour of somebody's time, from master data."""
    try:
        from ..master_data_loader import load_master_data  # noqa: PLC0415

        keywords = (
            load_master_data().data.get("labour_detection", {}).get("keywords")
        )
    except Exception:  # pragma: no cover - master data is optional
        keywords = None
    return tuple(keywords or ("labour", "labor", "technician", "hours"))


def _load_seen_invoices(exclude_case_id: str = "") -> list:
    """Previously processed invoices, read from the case output directory.

    A filesystem scan is the local stand-in for a query. Phase 1 replaces this
    with an indexed lookup against the `invoice` table -- scanning every prior
    case does not survive real volume, and the CaseRepository port already
    defines the batch read it will use.
    """
    seen = []
    base = OUTPUT_BASE_DIR
    if not base.exists():
        return seen
    for case_dir in sorted(base.iterdir()):
        if not case_dir.is_dir() or case_dir.name == exclude_case_id:
            continue
        extraction = case_dir / "02_extraction.json"
        if not extraction.exists():
            continue
        try:
            data = json.loads(extraction.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        invoice = data.get("invoice") or {}
        if invoice:
            seen.append(
                duplicates.InvoiceKey.from_invoice(case_dir.name, invoice)
            )
    return seen


class Phase3ValidatorAgent(BaseAgent):
    """Phase 3: External Validation (duplicates, vendor matching)"""

    def __init__(self, seen_invoices=None):
        """`seen_invoices` supplies the corpus to compare against.

        It is injected because the default -- scanning the output directory --
        is neither tenant-scoped nor durable: on a deployed worker that
        directory is local, ephemeral disk shared by every tenant the worker
        serves, so one tenant's invoice could be reported as a duplicate of
        another tenant's case, by case id. The worker passes a reader bound to
        one tenant's repository instead.
        """
        super().__init__("phase3_validator")
        self._seen_invoices = seen_invoices or _load_seen_invoices

    def run(self, output_folder: Path, extraction: dict, phase2: dict) -> dict:
        print(" [5/9] Phase 3 Validator: Starting...")

        invoice = extraction.get("invoice", {})
        validations = []

        # Step 3.1: Duplicate Detection
        if _get_config().get("duplicate_check_enabled"):
            case_id = (
                output_folder.name if output_folder is not None else "current"
            )
            candidate = duplicates.InvoiceKey.from_invoice(case_id, invoice)
            matches = duplicates.find_duplicates(
                candidate, self._seen_invoices(exclude_case_id=case_id)
            )
            entry = duplicates.to_validation(matches)
            if matches:
                print(f"   Duplicate check: {entry['evidence']}")
            validations.append(entry)

        # Step 3.2: Tax ID Validation.
        #
        # Two questions, and the checksum only answers the first. "Is this
        # identifier well-formed?" is arithmetic; "is it *this supplier's*?"
        # needs a vendor master, and it is the question that catches an invoice
        # carrying somebody else's registration -- structurally perfect, and
        # not theirs. Where no master is loaded the check is the checksum
        # alone, exactly as before: a tenant who has not listed their suppliers
        # must not have every invoice rejected as coming from an unknown one.
        tax_validation = invoice.get("_tax_id_validation", {})
        match = vendor_master.check_vendor_tax_id(invoice, _load_vendors())
        if tax_validation or not match.matched:
            checksum_ok = (
                tax_validation.get("valid", True) if tax_validation else True
            )
            passed = checksum_ok and match.matched
            evidence = tax_validation.get("reason", "Checksum not run")
            if match.outcome != vendor_master.NOT_CONFIGURED:
                evidence = f"{evidence}. {match.evidence}"
            validations.append(
                {
                    "step": "3.2",
                    "rule": "Tax ID valid and registered to this supplier",
                    "passed": passed,
                    "evidence": evidence,
                    "outcome": match.outcome,
                    "rejection_template": None
                    if passed
                    else "Vendor tax ID invalid",
                }
            )

        # Step 3.3: Future Date Check
        invoice_date = parse_date(invoice.get("invoice_date"))
        today = _reference_date()

        if invoice_date:
            is_future = invoice_date > today
            validations.append(
                {
                    "step": "3.3",
                    "rule": "Invoice not future dated",
                    "passed": not is_future,
                    "evidence": f"Invoice date: {invoice.get('invoice_date')}",
                    "rejection_template": None
                    if not is_future
                    else "Invoice is future dated",
                }
            )

        # Step 3.4: Bank details against the vendor's verified account. The
        # payment-redirection control. Holds, never rejects -- see bank_check.
        if "3.4" not in (_get_config().get("disabled_checks") or []):
            # match.vendor is None for an unconfigured master and for an
            # unknown vendor: there is no verified account to compare with.
            result = bank_check.compare(
                invoice,
                match.vendor,
                sender=_get_config().get("document_sender"),
                reference_date=today,
                # Default only when absent: 0 is a legitimate "never treat a
                # change as recent", not a request for 30.
                recent_days=int(
                    _get_config().get("bank_change_recent_days", 30)
                ),
            )
            if result.compared:
                validations.append(
                    {
                        "step": "3.4",
                        "rule": "Bank details match the vendor's verified account",
                        "passed": result.passed,
                        "informational": True,
                        "evidence": "; ".join(result.evidence),
                        "signals": result.signals,
                        "rejection_template": None,
                    }
                )
                if not result.passed:
                    # The routing decision was made at extraction time from
                    # grounding alone. A bank signal is the strongest cheap
                    # reason there is to put a person in front of an invoice,
                    # so it reopens that decision and the artifact is re-saved
                    # for every reader that takes routing from it.
                    invoice["_routing"] = "REVIEW"
                    summary = invoice.setdefault("_evidence", {}).setdefault(
                        "_summary", {}
                    )
                    needing = summary.setdefault("fields_needing_review", [])
                    needing.extend(
                        f"bank: {s}"
                        for s in result.signals
                        if f"bank: {s}" not in needing
                    )
                    self.save_artifact(
                        output_folder, "02_extraction.json", extraction
                    )

        # Determine decision
        failed = _settle(validations)
        decision = "REJECT" if failed else "CONTINUE"

        output = self.create_output(
            {
                "phase": 3,
                "validations": validations,
                "decision": decision,
                "rejection_template": failed[0].get("rejection_template")
                if failed
                else None,
            },
            input_refs=["02_extraction.json", "04_phase2_validation.json"],
        )

        self.save_artifact(output_folder, "05_phase3_validation.json", output)
        print(f"   Phase 3: {decision}")
        return output


# ============================================================================
# AGENT 6: PHASE 4 VALIDATOR (Calculations)
# ============================================================================


class Phase4ValidatorAgent(BaseAgent):
    """Phase 4: Calculation Validation"""

    def __init__(self):
        super().__init__("phase4_validator")

    def run(self, output_folder: Path, extraction: dict, phase3: dict) -> dict:
        print(" [6/9] Phase 4 Validator: Starting...")

        invoice = extraction.get("invoice", {})
        waf = extraction.get("work_authorization") or {}
        validations = []

        # Step 4.1: Total Verification
        # `or 0`, not `.get(key, 0)`: a field the extractor looked for and did
        # not find is present and *null*, so the default never applies and the
        # arithmetic below dies on None. That crash dead-lettered a real
        # invoice whose only sin was stating no tax -- three retries, no
        # decision, no explanation. An invoice with no tax amount is ex + 0,
        # which is exactly what the document says.
        stated_tax = invoice.get("tax_amount")
        total_inc = invoice.get("invoice_total_inc_tax") or 0
        total_ex = invoice.get("invoice_total_ex_tax") or 0
        tax = _stated_tax_total(invoice)

        # Subtotal minus discount, plus tax, plus anything charged outside the
        # line table, plus the rounding the invoice applies to itself. Each
        # term is absent on most invoices and defaults to zero, so this is
        # `ex + tax` for a document that states none of them -- and correct for
        # one that does. Reading only `ex + tax` meant a discount, a freight
        # charge or a round-off line each looked like an arithmetic error.
        subtotal = invoice.get("subtotal")
        base = subtotal if subtotal is not None else total_ex
        _, charges = _charges_and_lines(invoice)
        other_charges = sum((charge.get("amount") or 0) for charge in charges)
        expected_total = (
            base
            - (invoice.get("discount") or 0)
            + tax
            + other_charges
            + (invoice.get("round_off") or 0)
        )
        balance = abs(total_inc - expected_total)
        tolerance = _get_config().get("balance_tolerance", 0.02)

        # Informational only, per invoice_master_data.yaml (`informational_only:
        # true`) and rules book Step 4.1. Previously a failure here rejected the
        # invoice with `rejection_template = None`, producing a reason-less
        # REJECT. Reconciliation failures are rejected by Step 4.2, which has a
        # template; this step records evidence for audit.
        validations.append(
            {
                "step": "4.1",
                "rule": "Total = Subtotal + Tax",
                "passed": balance <= tolerance,
                "informational": True,
                # The reader is told when the tax was assumed rather than
                # read: a check that quietly supplies a number it never saw
                # is the kind of thing this system exists not to do.
                "evidence": f"Total: {total_inc}, Expected: {expected_total:.2f}, Diff: {balance:.2f}"
                + (
                    ""
                    if stated_tax is not None or tax
                    else " (no tax amount stated)"
                ),
            }
        )

        # Step 4.2: Line Sum Validation.
        #
        # Two arithmetic claims, not one. The lines must add up to the
        # subtotal, and each line must be the product it says it is: a
        # quantity changed without its total, or a line discount applied
        # wrongly, leaves the subtotal untouched and is invisible to a sum.
        # A charge the extractor also returned as a line is not a second
        # charge: counting both makes the lines overshoot a subtotal that never
        # included it.
        lines, _ = _charges_and_lines(invoice)
        line_total = sum((item.get("amount_ex_tax") or 0) for item in lines)
        stated_subtotal = (
            invoice.get("subtotal")
            if invoice.get("subtotal") is not None
            else total_ex
        )
        line_diff = abs(line_total - (stated_subtotal or 0))
        line_tolerance = _get_config().get("line_sum_tolerance", 1.00)
        evidence = (
            f"Line sum: {line_total:.2f}, Subtotal: {stated_subtotal or 0:.2f}, "
            f"Diff: {line_diff:.2f}"
        )
        reconciles = line_diff <= line_tolerance

        if reconciles:
            for index, item in enumerate(lines, 1):
                quantity = item.get("quantity")
                unit_price = item.get("unit_price")
                stated = item.get("amount_ex_tax")
                if quantity is None or unit_price is None or stated is None:
                    continue
                discount, quantifiable = _line_discount(
                    item, quantity, unit_price
                )
                if not quantifiable:
                    # The line shows a discount and we could not read how much.
                    # Rejecting here rejects the supplier for our own reading,
                    # which is what the tax-ID comparison already learned.
                    continue
                computed = quantity * unit_price - discount
                if abs(computed - stated) > line_tolerance:
                    reconciles = False
                    evidence = (
                        f"Line {index} ({item.get('description') or 'unnamed'}): "
                        f"{quantity:g} x {unit_price:,.2f} = {computed:,.2f}, "
                        f"but the invoice states {stated:,.2f}"
                    )
                    break

        validations.append(
            {
                "step": "4.2",
                "rule": "Sum of lines = Subtotal, and each line adds up",
                "passed": reconciles,
                "evidence": evidence,
                "rejection_template": None
                if reconciles
                else "Invoice amounts do not reconcile",
            }
        )

        # Step 4.3: is the invoice inside what was authorised?
        #
        # Gated on `require_work_authorization`, as this step's
        # `enabled_when_config` in invoice_master_data.yaml declares -- the
        # gate was previously specified and not honoured, so an organization
        # that does not require work authorization still had invoices rejected
        # for lacking one.
        #
        # Where the tenant has loaded what they authorised, "inside it" means
        # all four of hours, rate, project and period. Billing 200 hours
        # against 40, and billing 40 hours at twice the agreed rate, cost the
        # same and are different conversations with the supplier, so the
        # evidence names which one happened.
        if _get_config().get("require_work_authorization"):
            match = wa_matching.match_invoice_to_wa(
                invoice, _load_work_authorizations(), vendors=_load_vendors()
            )
            if match.outcome == wa_matching.MATCHED:
                result = wa_matching.hours_check(
                    invoice,
                    match.authorization,
                    hours_tolerance=_get_config().get("hours_tolerance", 0.5),
                    labour_keywords=_labour_keywords(),
                )
                validations.append(
                    {
                        "step": "4.3",
                        "rule": "Invoice within its work authorization",
                        "passed": result.ok,
                        "evidence": result.evidence,
                        "outcome": result.outcome,
                        "details": result.details,
                        "rejection_template": None
                        if result.ok
                        else "Invoice does not match work authorization",
                    }
                )
            elif match.outcome == wa_matching.NOT_CONFIGURED:
                self._legacy_hours_check(invoice, waf, validations)
            # Any other outcome means step 1.4 has already rejected this
            # invoice for the missing or wrong authorisation, and repeating
            # itself in the findings helps nobody.

        # Step 4.4: Tax Rate Verification.
        #
        # Per line where the invoice classifies its lines, because one blended
        # rate cannot check a multi-rate invoice: a 12% line taxed at 18% and
        # an 18% line taxed at 12% cancel out perfectly in the total. Where the
        # lines carry no classification this is the single-rate comparison it
        # always was.
        currency = str(invoice.get("currency") or "").upper()
        config = _get_config()
        # Absolute, or a share of the tax due, whichever is larger. A fixed
        # rupee or two is the right allowance on a small invoice and an
        # impossible one on a large: per-line rounding and the apportionment of
        # an invoice-level discount across twelve lines put a two-million-rupee
        # tax figure several hundred out, which is 0.07% and not a discrepancy
        # anyone would investigate. The faults this check exists to catch --
        # the wrong rate, the wrong base, tax on an exempt line -- are whole
        # percentage points wide.
        per_line = _expected_tax(invoice, config)

        if per_line is not None:
            expected_tax, how = per_line
            evidence_rate = f"per line at {how}"
        else:
            tax_rate = (config.get("tax_rates_by_currency") or {}).get(
                currency, config.get("default_tax_rate", 0.10)
            )
            expected_tax = round(total_ex * tax_rate, 2)
            evidence_rate = f"{tax_rate:.0%} of {total_ex:.2f}"

        # Absolute, or a share of the tax due, whichever is larger. A fixed
        # rupee or two is the right allowance on a small invoice and an
        # impossible one on a large: per-line rounding and the apportionment of
        # an invoice-level discount across twelve lines put a two-million-rupee
        # tax figure several hundred out, which is 0.07% and not a discrepancy
        # anyone would investigate. The faults this check exists to catch --
        # the wrong rate, the wrong base, tax on an exempt line -- are whole
        # percentage points wide.
        tax_tolerance = max(
            config.get("tax_rate_tolerance", tolerance),
            abs(expected_tax) * config.get("tax_rate_tolerance_pct", 0.002),
        )
        tax_diff = abs(tax - expected_tax)
        tax_valid = tax_diff <= tax_tolerance
        evidence = (
            f"Tax: {tax:.2f}, Expected: {expected_tax:.2f} "
            f"({evidence_rate}), Diff: {tax_diff:.2f}"
        )

        # The components have to agree with where the supply happened, even
        # when the total is right: charging the wrong kind of tax is a filing
        # error the customer carries, not the supplier.
        inconsistent = _components_consistent(invoice)
        if tax_valid and inconsistent:
            tax_valid = False
            evidence = f"{evidence}. {inconsistent}"

        validations.append(
            {
                "step": "4.4",
                "rule": "Tax amount matches the statutory rate",
                "passed": tax_valid,
                "evidence": evidence,
                "rejection_template": None
                if tax_valid
                else "Invoice tax amount incorrect",
            }
        )

        return self._finish_phase4(output_folder, validations)

    def _finish_phase4(self, output_folder: Path, validations: list) -> dict:
        """Settle the phase and write it. Informational steps record evidence
        but never reject -- see Step 4.1."""
        failed = _settle(validations)
        decision = "REJECT" if failed else "ACCEPT"

        output = self.create_output(
            {
                "phase": 4,
                "validations": validations,
                "decision": decision,
                "rejection_template": failed[0].get("rejection_template")
                if failed
                else None,
            },
            input_refs=["02_extraction.json", "05_phase3_validation.json"],
        )

        self.save_artifact(output_folder, "06_phase4_validation.json", output)
        print(f"   Phase 4: {decision}")
        return output

    def _legacy_hours_check(self, invoice: dict, waf: dict, validations: list):
        """The form-based path, for a tenant with no authorization master.

        Unchanged behaviour: hours read off an attached Work Authorization
        Form, with the work-type exemption deciding whether one was needed.
        """
        work_type = self._determine_work_type(invoice)
        exempt_types = [
            t.upper() for t in _get_config().get("waf_exempt_work_types", [])
        ]
        waf_required = work_type not in exempt_types

        invoice_hours = self._calculate_labour_hours(invoice)
        waf_hours = waf.get("authorized_hours", 0) if waf else 0
        hours_tolerance = _get_config().get("hours_tolerance", 0.5)

        if waf and waf_hours and waf_hours > 0:
            # WAF present with authorized hours — check limits
            hours_valid = invoice_hours <= waf_hours + hours_tolerance
            validations.append(
                {
                    "step": "4.3",
                    "rule": "Labour hours within authorization",
                    "passed": hours_valid,
                    "evidence": f"Invoice: {invoice_hours}h, Authorized: {waf_hours}h, Work type: {work_type}",
                    "rejection_template": None
                    if hours_valid
                    else "Invoice does not match work authorization",
                }
            )
        elif waf_required and invoice_hours > 0 and (not waf or not waf_hours):
            # Non-exempt work type has labour hours but no WAF authorization
            validations.append(
                {
                    "step": "4.3",
                    "rule": "Labour hours within authorization",
                    "passed": False,
                    "evidence": (
                        f"Invoice: {invoice_hours}h, Work type: {work_type} (non-exempt), "
                        f"WAF authorized hours: {waf_hours} — no authorization for billed labour"
                    ),
                    "rejection_template": "Invoice does not match work authorization",
                }
            )

    def _determine_work_type(self, invoice: dict) -> str:
        """Determine work type from invoice line item descriptions."""
        return determine_work_type(invoice)

    def _calculate_labour_hours(self, invoice: dict) -> float:
        """Calculate total labour hours from invoice"""
        total = 0.0
        labour_keywords = ["labour", "labor", "technician", "hours"]

        for line in invoice.get("line_items") or []:
            desc = (line.get("description") or "").lower()
            if any(kw in desc for kw in labour_keywords):
                qty = line.get("quantity") or 0
                if qty > 0:
                    total += qty
        return total


# ============================================================================
# AGENT 7: TRANSFORMER
# ============================================================================


class TransformerAgent(BaseAgent):
    """Line Item Transformation Agent"""

    VALID_ITEM_CODES: ClassVar[list[str]] = [
        "LABOUR",
        "LABOUR_AH",
        "PARTS",
        "FREIGHT",
        "TRAVEL",
        "CALLOUT",
        "HIRE",
        "CLEANING",
        "OTHER",
    ]

    # Tax code normalization map — extracted values are mapped to
    # the canonical codes expected by downstream systems and eval.
    TAX_CODE_MAP: ClassVar[dict[str, str]] = {
        "GST": "TAX",
        "gst": "TAX",
        "Gst": "TAX",
        "VAT": "TAX",
        "vat": "TAX",
    }

    def __init__(self):
        super().__init__("transformer")

    def run(self, output_folder: Path, extraction: dict, phase4: dict) -> dict:
        print(" [7/9] Transformer Agent: Starting...")

        invoice = extraction.get("invoice", {})
        currency = invoice.get("currency", "AUD")
        tax_rate = (
            _get_config().get("tax_rates_by_currency", {}).get(currency, 0.10)
        )

        line_items = invoice.get("line_items") or []

        # Classify all line items in a single LLM call for efficiency
        descriptions = [line.get("description", "") for line in line_items]
        item_codes = self._classify_items(descriptions)

        mapped_items = []
        for idx, line in enumerate(line_items):
            item_code = item_codes[idx] if idx < len(item_codes) else "OTHER"
            amount = line.get("amount_ex_tax") or 0
            tax = line.get("tax_amount") or (amount * tax_rate)

            # Normalize tax code (e.g. GST -> TAX)
            raw_tax_code = line.get("tax_code", "TAX")
            tax_code = self.TAX_CODE_MAP.get(raw_tax_code, raw_tax_code)

            mapped_items.append(
                {
                    "line_number": idx + 1,
                    "item_code": item_code,
                    "description": line.get("description", ""),
                    "quantity": f"{line.get('quantity') or 1:.2f}",
                    "unit_cost": f"{line.get('unit_price') or amount:,.2f}",
                    "line_cost": f"{amount:,.2f}",
                    "tax": f"{tax:.2f}",
                    "tax_code": tax_code,
                }
            )

        # Calculate totals
        totals = {
            "line_cost_total": sum(
                float(i["line_cost"].replace(",", "")) for i in mapped_items
            ),
            "tax_total": sum(float(i["tax"]) for i in mapped_items),
        }
        totals["grand_total"] = totals["line_cost_total"] + totals["tax_total"]

        output = self.create_output(
            {
                "currency": currency,
                "tax_rate": tax_rate,
                "line_items_mapped": mapped_items,
                "totals": totals,
            },
            input_refs=["02_extraction.json", "06_phase4_validation.json"],
        )

        self.save_artifact(output_folder, "07_transformation.json", output)
        print(f"   Mapped {len(mapped_items)} line items")
        return output

    def _classify_items(self, descriptions: list[str]) -> list[str]:
        """Classify line items deterministically.

        Order matters, and it is chosen for reproducibility:

        1. Keyword match -- pure logic, identical every run, and free. The
           keyword map is ordered most-specific-first, so "Emergency call-out -
           overtime rate" resolves to LABOUR_AH rather than CALLOUT.
        2. Memoized LLM result -- a description classified before keeps its
           answer, so repeat invoices from a vendor cost nothing and never
           reclassify differently.
        3. LLM -- only for descriptions no keyword covers and that have not been
           seen before. The result is cached.

        This exists because temperature=0 does not make Gemini reproducible:
        the same description returned CALLOUT twice and LABOUR_AH three times
        across five runs, and a fixed seed only reduced the disagreement. That
        variance swings eval cases pass/fail between identical runs, which makes
        an accuracy gate in CI meaningless.
        """
        if not descriptions:
            return []

        codes: list[str | None] = list(
            self._classify_items_keyword_fallback(descriptions)
        )
        # The keyword pass returns OTHER for "no match"; treat that as unknown
        # so a real classifier gets a chance at it.
        unresolved = [i for i, code in enumerate(codes) if code == "OTHER"]
        if not unresolved:
            return codes

        cache = self._load_item_code_cache()
        still_unknown = []
        for index in unresolved:
            cached = cache.get(self._cache_key(descriptions[index]))
            if cached in self.VALID_ITEM_CODES:
                codes[index] = cached
            else:
                still_unknown.append(index)

        if still_unknown:
            resolved = self._classify_items_llm(
                [descriptions[i] for i in still_unknown]
            )
            for index, code in zip(still_unknown, resolved, strict=False):
                codes[index] = code
                cache[self._cache_key(descriptions[index])] = code
            self._save_item_code_cache(cache)

        return [code or "OTHER" for code in codes]

    @staticmethod
    def _cache_key(description: str) -> str:
        return " ".join(str(description).strip().lower().split())

    @staticmethod
    def _load_item_code_cache() -> dict:
        try:
            if ITEM_CODE_CACHE_PATH.exists():
                with open(ITEM_CODE_CACHE_PATH, encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            pass  # A broken cache must never block processing.
        return {}

    @staticmethod
    def _save_item_code_cache(cache: dict) -> None:
        try:
            ITEM_CODE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(ITEM_CODE_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2, sort_keys=True)
        except OSError as exc:
            print(f"    Warning: could not save item code cache: {exc}")

    def _classify_items_llm(self, descriptions: list[str]) -> list[str]:
        """Classify all line item descriptions using a single LLM call.

        Uses Gemini to semantically classify each line item description into
        one of the valid item codes, rather than relying on brittle keyword matching.
        Falls back to 'OTHER' on failure.
        """
        if not descriptions:
            return []

        items_block = "\n".join(
            f'  {i + 1}. "{desc}"' for i, desc in enumerate(descriptions)
        )

        prompt = f"""Classify each invoice line item description into exactly one item code.

VALID ITEM CODES:
- LABOUR: Work performed by technicians, engineers, tradespeople. Includes service calls,
  inspections, diagnostics, calibration, testing, system checks, standard-hours work.
- LABOUR_AH: After-hours or overtime labour only.
- PARTS: Physical materials, components, equipment, replacement parts, filters, coils,
  fittings, valves, units, supplies, consumables — any tangible item purchased/installed.
- FREIGHT: Delivery, shipping, freight charges.
- TRAVEL: Travel time, mileage, kilometre charges, travel allowances.
- CALLOUT: Call-out fees, attendance fees, minimum charges for site visits.
- HIRE: Equipment hire or rental charges.
- CLEANING: Cleaning services, janitorial work.
- OTHER: Only if the description truly does not fit any of the above categories.

LINE ITEMS TO CLASSIFY:
{items_block}

IMPORTANT RULES:
- Physical items (filters, coils, fittings, parts, materials, equipment, units) are PARTS.
- Service work (calibration, diagnostics, testing, inspection, repair labour) is LABOUR.
- Prefer a specific code over OTHER. Only use OTHER as a last resort.

Return ONLY a JSON object in this exact format:
{{"classifications": [{{"item_number": 1, "item_code": "CODE", "reasoning": "brief reason"}}, ...]}}"""

        try:
            result, _ = call_gemini(prompt, _gcp_config.GEMINI_FLASH_MODEL)
            json_str = clean_json_response(result)
            parsed = json.loads(json_str)
            classifications = parsed.get("classifications", [])

            # Build result list, validating each code
            codes = []
            for i, _desc in enumerate(descriptions):
                item_num = i + 1
                match = next(
                    (
                        c
                        for c in classifications
                        if c.get("item_number") == item_num
                    ),
                    None,
                )
                if match and match.get("item_code") in self.VALID_ITEM_CODES:
                    codes.append(match["item_code"])
                else:
                    codes.append("OTHER")
            return codes

        except LlmUnavailable:
            raise  # the service, not the document: see llm_guard
        except Exception as e:
            print(
                f"    Warning: LLM item classification failed ({e}), using fallback"
            )
            return self._classify_items_keyword_fallback(descriptions)

    def _classify_items_keyword_fallback(
        self, descriptions: list[str]
    ) -> list[str]:
        """Keyword-based fallback for item classification when LLM is unavailable."""
        KEYWORD_MAP = {
            "LABOUR_AH": ["after hours", "overtime", "a/h", "after-hours"],
            "LABOUR": [
                "labour",
                "labor",
                "technician",
                "installation",
                "service",
                "calibration",
                "diagnostics",
                "inspection",
                "repair",
            ],
            "PARTS": [
                "parts",
                "material",
                "component",
                "supply",
                "filter",
                "coil",
                "fitting",
                "valve",
                "unit",
                "equipment",
                "replacement",
            ],
            "FREIGHT": ["freight", "delivery", "shipping"],
            "TRAVEL": ["travel", "mileage", "kilometre", "kilometer"],
            "CALLOUT": ["call out", "callout", "attendance"],
            "HIRE": ["hire", "rental"],
            "CLEANING": ["cleaning", "clean"],
        }

        codes = []
        for desc in descriptions:
            desc_lower = desc.lower()
            matched = "OTHER"
            for code, keywords in KEYWORD_MAP.items():
                if any(kw in desc_lower for kw in keywords):
                    matched = code
                    break
            codes.append(matched)
        return codes


# ============================================================================
# AGENT 8: OUTPUT GENERATOR
# ============================================================================


class OutputGeneratorAgent(BaseAgent):
    """Final Output Generation Agent"""

    def __init__(self):
        super().__init__("output_generator")

    def run(
        self,
        output_folder: Path,
        extraction: dict,
        transformer: dict,
        decision: str,
        rejection_template: str | None = None,
        rejection_reason: str | None = None,
        rejection_phase: int | None = None,
    ) -> dict:
        print(" [8/9] Output Generator: Starting...")

        invoice = extraction.get("invoice", {})

        # Map decision to status
        status_map = {
            "ACCEPT": "Pending Payment",
            "REJECT": "Rejected",
            "SET_ASIDE": "To Verify",
            "ERROR": "To Verify",
        }
        invoice_status = status_map.get(decision, "To Verify")

        # Generate outcome message
        timestamp = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        if decision == "ACCEPT":
            outcome = f"Invoice accepted for payment on {timestamp}"
        elif decision == "REJECT":
            reason = (
                rejection_template or rejection_reason or "Validation failed"
            )
            outcome = f"Invoice rejected on {timestamp}: {reason}"
        else:
            outcome = f"Invoice requires review as of {timestamp}"

        # Build output
        output_data = {
            "Invoice Processing": {
                "Invoice Type": "Normal",
                "Invoice Status": invoice_status,
                "Invoice Source": "Email",
            },
            "Invoice Details": {
                "Vendor Invoice": invoice.get("invoice_number", ""),
                "Invoice Date": invoice.get("invoice_date", ""),
                "Invoice Total": _money(invoice.get("invoice_total_inc_tax")),
                "Pretax Total": _money(invoice.get("invoice_total_ex_tax")),
                "Tax Amount": _money(invoice.get("tax_amount")),
                "Currency": invoice.get("currency", "AUD"),
            },
            "Vendor Information": {
                "Vendor Name": invoice.get("vendor_name", ""),
                "Tax ID": invoice.get("vendor_tax_id", ""),
            },
            "Line Items": json.dumps(transformer.get("line_items_mapped", [])),
            "Outcome Message": {"Outcome Message": outcome},
        }

        # Save output
        output_file = output_folder / "Postprocessing_Data.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)

        print(
            f"  => Saved: Postprocessing_Data.json (Status: {invoice_status})"
        )

        # Save decision artifact
        decision_output = self.create_output(
            {
                "final_decision": decision,
                "decision_class": decision,  # ACCEPT, REJECT, SET_ASIDE — used by investigation agent
                "invoice_status": invoice_status,
                "rejection_template": rejection_template,
                "rejection_reason": rejection_reason,
                "rejection_phase": f"Phase {rejection_phase}"
                if rejection_phase
                else None,
            }
        )
        self.save_artifact(output_folder, "08_decision.json", decision_output)

        return output_data


# ============================================================================
# AGENT 9: AUDIT LOGGER
# ============================================================================


class AuditLoggerAgent(BaseAgent):
    """Audit Logging Agent"""

    def __init__(self):
        super().__init__("audit_logger")

    def run(
        self,
        output_folder: Path,
        source_folder: Path,
        decision: str,
        processing_time: float,
    ) -> dict:
        print(" [9/9] Audit Logger: Creating audit trail...")

        output = self.create_output(
            {
                "case_id": source_folder.name,
                "source_folder": str(source_folder),
                "output_folder": str(output_folder),
                "processing_summary": {
                    "decision": decision,
                    "processing_time_seconds": round(processing_time, 2),
                    "total_llm_calls": _get_metrics()["llm_calls"],
                    "total_tokens": _get_metrics()["total_tokens"]["prompt"]
                    + _get_metrics()["total_tokens"]["completion"],
                    "total_cost_usd": round(
                        _get_metrics()["total_cost_usd"], 6
                    ),
                },
                "artifacts": [
                    "01_classification.json",
                    "02_extraction.json",
                    "03_phase1_validation.json",
                    "04_phase2_validation.json",
                    "05_phase3_validation.json",
                    "06_phase4_validation.json",
                    "07_transformation.json",
                    "08_decision.json",
                    "09_audit_log.json",
                    "Postprocessing_Data.json",
                ],
            }
        )

        self.save_artifact(output_folder, "09_audit_log.json", output)
        return output


# ============================================================================
# ORCHESTRATOR
# ============================================================================


def _money(value: Any) -> str:
    """Format a money value, or empty string when it is genuinely unknown.

    `dict.get(key, 0)` returns the default only when the key is ABSENT. The
    extraction schema emits every field, using None where the model could not
    read one -- so the default never applied, and `f"{None:,.2f}"` crashed the
    output generator on the first real invoice with no tax line.

    Rendering an unknown amount as "0.00" would be worse than the crash: it
    asserts the tax is zero on a document where we simply could not read it,
    and this JSON is what feeds the ERP export. Empty is honest, and the eval
    comparator already treats a missing amount as missing on both sides.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return f"{value:,.2f}"


def _handle_phase_rejection(
    output_folder: Path,
    source_folder: Path,
    extraction: dict,
    phase_result: dict,
    phase_num: int,
    start_time: float,
) -> dict:
    """Handle rejection at a specific validation phase."""
    transformer = TransformerAgent()
    transformer_result = transformer.run(
        output_folder, extraction, phase_result
    )

    output_gen = OutputGeneratorAgent()
    output_gen.run(
        output_folder,
        extraction,
        transformer_result,
        "REJECT",
        phase_result.get("rejection_template"),
        phase_result.get("rejection_reason"),
        rejection_phase=phase_num,
    )

    audit = AuditLoggerAgent()
    audit.run(output_folder, source_folder, "REJECT", time.time() - start_time)
    return {"decision": "REJECT", "phase": phase_num}


# The nine agents in the order they run, with the wording shown while each one
# is working. Progress is reported per step because "processing" on its own is
# indistinguishable from "stuck": a Vertex call can take a minute, and someone
# watching an unchanging spinner concludes the upload failed and re-uploads.
PIPELINE_STEPS: list[tuple[str, str]] = [
    ("classification", "Classifying the document"),
    ("extraction", "Extracting invoice fields"),
    ("phase1", "Initial intake checks"),
    ("phase2", "Content validation"),
    ("phase3", "External validation"),
    ("phase4", "Calculation validation"),
    ("transformer", "Transforming for export"),
    ("decision", "Writing the decision"),
    ("audit_log", "Recording the audit log"),
]


def _step_reporter(on_step):
    """Announce the step about to run.

    A reporter that raised would turn a cosmetic progress bar into a way for
    the pipeline to die, so anything it throws is swallowed here.
    """

    def report(index: int) -> None:
        if on_step is None:
            return
        key, label = PIPELINE_STEPS[index - 1]
        try:
            on_step(
                {
                    "step": index,
                    "total": len(PIPELINE_STEPS),
                    "key": key,
                    "label": label,
                    # When, so a reader can tell work in flight from a step
                    # left behind by a worker that died mid-invoice.
                    "at": time.time(),
                }
            )
        except Exception as exc:
            print(f"   (progress not reported: {exc})")

    return report


def _run_pipeline(
    source_folder: Path,
    output_folder: Path,
    start_time: float,
    on_step=None,
    seen_invoices=None,
    force_invoice: bool = False,
) -> dict:
    """Run the main processing pipeline (agents 1-9). Returns result dict.
    `force_invoice`: a person sent this document to the invoice queue, so a
    PO/GRN classification does not divert it."""
    report = _step_reporter(on_step)

    # Agent 1: Classification
    report(1)
    classifier = ClassifierAgent()
    classification = classifier.run(source_folder, output_folder)

    # A PO or GRN on its own goes to a person to confirm; a bundle that also
    # holds an invoice is still an invoice.
    summary = classification.get("summary") or {}
    document_type = summary.get("document_type")
    if (
        not force_invoice
        and document_type in SUPPORTING_TYPES
        and not summary.get("invoice_count")
        and summary.get("classified_by") == "model"
    ):
        files = classification.get("files") or {}
        pdf = next(
            (
                name
                for name, info in files.items()
                if isinstance(info, dict) and info.get("type") == "pdf"
            ),
            "",
        )
        return {
            "decision": "SUPPORTING",
            "document_type": document_type,
            "pdf": str(source_folder / pdf) if pdf else "",
        }

    # Agent 2: Extraction
    report(2)
    extractor = ExtractorAgent()
    extraction = extractor.run(source_folder, output_folder, classification)

    # Agent 3: Phase 1 Validation
    report(3)
    phase1 = Phase1ValidatorAgent()
    phase1_result = phase1.run(output_folder, extraction)

    if phase1_result["decision"] == "REJECT":
        return _handle_phase_rejection(
            output_folder,
            source_folder,
            extraction,
            phase1_result,
            1,
            start_time,
        )

    # Agent 4: Phase 2 Validation
    report(4)
    phase2 = Phase2ValidatorAgent()
    phase2_result = phase2.run(output_folder, extraction, phase1_result)

    if phase2_result["decision"] == "REJECT":
        return _handle_phase_rejection(
            output_folder,
            source_folder,
            extraction,
            phase2_result,
            2,
            start_time,
        )

    # Agent 5: Phase 3 Validation
    report(5)
    phase3 = Phase3ValidatorAgent(seen_invoices=seen_invoices)
    phase3_result = phase3.run(output_folder, extraction, phase2_result)

    if phase3_result["decision"] == "REJECT":
        return _handle_phase_rejection(
            output_folder,
            source_folder,
            extraction,
            phase3_result,
            3,
            start_time,
        )

    # Agent 6: Phase 4 Validation
    report(6)
    phase4 = Phase4ValidatorAgent()
    phase4_result = phase4.run(output_folder, extraction, phase3_result)

    # Agent 7: Transformer
    report(7)
    transformer = TransformerAgent()
    transformer_result = transformer.run(
        output_folder, extraction, phase4_result
    )

    # Agent 8: Output Generator
    report(8)
    output_gen = OutputGeneratorAgent()
    output_gen.run(
        output_folder,
        extraction,
        transformer_result,
        phase4_result["decision"],
        phase4_result.get("rejection_template"),
        phase4_result.get("rejection_reason"),
        rejection_phase=4 if phase4_result["decision"] == "REJECT" else None,
    )

    # Agent 9: Audit Logger
    report(9)
    processing_time = time.time() - start_time
    audit = AuditLoggerAgent()
    audit.run(
        output_folder, source_folder, phase4_result["decision"], processing_time
    )

    return {
        "decision": phase4_result["decision"],
        "processing_time": processing_time,
        "output_folder": str(output_folder),
    }


def process_invoice(
    source_folder: Path, on_step=None, seen_invoices=None, force_invoice=False
) -> dict:
    """Main pipeline orchestrator.

    `on_step` is called with a progress record before each agent runs, so a
    caller -- the worker -- can publish where a document has got to.
    """
    _ensure_gcp_initialized()
    start_time = time.time()

    case_id = source_folder.name
    output_folder = get_output_folder(case_id)

    print(f"\n{'=' * 60}")
    print(f"Processing: {case_id}")
    print(f"{'=' * 60}")

    # Reset metrics
    _metrics_store["METRICS"] = {
        "llm_calls": 0,
        "total_tokens": {"prompt": 0, "completion": 0},
        "total_cost_usd": 0.0,
        "agent_breakdown": [],
    }

    try:
        return _run_pipeline(
            source_folder,
            output_folder,
            start_time,
            on_step,
            seen_invoices,
            force_invoice,
        )

    except Exception as e:
        print(f"\n   Error: {e}")
        traceback.print_exc()

        # Save error
        error_file = output_folder / "error.txt"
        error_file.write_text(f"Error: {e}\n\n{traceback.format_exc()}")

        audit = AuditLoggerAgent()
        audit.run(
            output_folder, source_folder, "ERROR", time.time() - start_time
        )

        return {"decision": "ERROR", "error": str(e)}


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="General Invoice Processing Agent"
    )
    parser.add_argument(
        "--base-dir", "-b", type=str, help="Base directory with case folders"
    )
    parser.add_argument(
        "--case", "-c", type=str, help="Single case folder path"
    )
    parser.add_argument("--config", type=str, help="Path to config JSON file")
    parser.add_argument(
        "--num-cases", "-n", type=int, help="Limit number of cases"
    )

    args = parser.parse_args()

    # Load configuration
    load_config(args.config)

    print("=" * 60)
    print("GENERAL INVOICE PROCESSING AGENT")
    print("=" * 60)
    print(f"Output Dir: {OUTPUT_BASE_DIR}")
    print()

    # Collect cases
    if args.case:
        case_folders = [Path(args.case)]
    elif args.base_dir:
        base_path = Path(args.base_dir)
        if not base_path.exists():
            print(f"Error: Directory not found: {base_path}")
            sys.exit(1)
        case_folders = sorted([d for d in base_path.iterdir() if d.is_dir()])
        if args.num_cases:
            case_folders = case_folders[: args.num_cases]
    else:
        print("Error: Specify --base-dir or --case")
        sys.exit(1)

    print(f"Processing {len(case_folders)} case(s)\n")

    # Process
    stats = {"total": len(case_folders), "accept": 0, "reject": 0, "error": 0}

    for idx, folder in enumerate(case_folders, 1):
        print(f"[{idx}/{len(case_folders)}]", end="")
        result = process_invoice(folder)

        decision = result.get("decision", "ERROR")
        if decision == "ACCEPT":
            stats["accept"] += 1
        elif decision == "REJECT":
            stats["reject"] += 1
        else:
            stats["error"] += 1

        print(f"   Result: {decision}\n")

    # Summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total:    {stats['total']}")
    print(f"Accepted: {stats['accept']}")
    print(f"Rejected: {stats['reject']}")
    print(f"Errors:   {stats['error']}")


if __name__ == "__main__":
    main()
