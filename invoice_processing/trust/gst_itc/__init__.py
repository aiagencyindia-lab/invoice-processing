"""GST/ITC v1 (FR-26): vendor GST standing, identity sharing, ITC at risk.

Per invoice: the supplier's GSTIN is checked (checksum, then the `gstin`
provider); a cancelled or suspended registration and an unverifiable one are
holds. Non-filing, a new vendor's oversized invoice and two GSTINs behind one
PAN/bank/domain are advisories. `GST-ITC-RISK` prices the input tax credit
those signals put at risk: `tax_amount * max(factor)`, Decimal only.

Not possible in Wave 1: IRN/e-invoice checks (no IRN field), place of
business (no vendor address), 2A/2B reconciliation.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from ... import tax_ids
from ..engine import EngineContext
from ..findings.model import Citation, FindingSpec, register_type

logger = logging.getLogger(__name__)

ENGINE = "gst_itc"
ENGINE_VERSION = "gst_itc-1"
RULE_VERSION = "FR-26-v1"

INACTIVE = "GST-INACTIVE"
UNVERIFIED = "GST-UNVERIFIED"
NONFILER = "GST-NONFILER"
NEW_LARGE = "GST-NEW-LARGE-FIRST"
SHARED = "GST-SHARED-IDENTITY"
ITC_RISK = "GST-ITC-RISK"

TYPES = {
    INACTIVE: ("The supplier's GST registration is not active", True),
    UNVERIFIED: ("The supplier's GSTIN could not be verified", True),
    NONFILER: ("The supplier has missed GST return periods", False),
    NEW_LARGE: ("A new supplier's invoice is far above the norm", False),
    SHARED: ("Two GSTINs share a PAN, bank account or domain", False),
    ITC_RISK: ("Input tax credit at risk on this invoice", False),
}
for _code, (_text, _hold) in TYPES.items():
    register_type(_code, _text, engine=ENGINE, hold=_hold)

_STATUSES = ("active", "cancelled", "suspended")


def _spec(code, invoice, key, summary, citations, *, impact=None, **details):
    return FindingSpec(
        type=code,
        transaction_node=invoice["id"],
        dedupe_key=f"{code}:{key}",
        impact=impact,
        currency=invoice["attrs"].get("currency") or "INR",
        confidence=1.0,
        engine_version=ENGINE_VERSION,
        rule_version=RULE_VERSION,
        model_version="none",
        summary=summary,
        details=details,
        citations=tuple(Citation(n) for n in dict.fromkeys(citations)),
    )


def _gstin_of(invoice: dict, vendor: dict | None) -> str | None:
    """The vendor master's GSTIN, else the one on the invoice. Only a
    15-character value is a GSTIN; an ABN or VAT number is not ours."""
    for raw in (
        (vendor or {}).get("attrs", {}).get("gstin"),
        invoice["attrs"].get("vendor_tax_id"),
    ):
        value = tax_ids.normalize(raw)
        if len(value) == 15:
            return value
    return None


class _Settings:
    def __init__(self, settings: dict):
        self.settings = settings

    def get(self, tenant_id: str) -> dict:
        return self.settings


class _Run:
    def __init__(self, ectx: EngineContext):
        self.ectx = ectx
        self.tid = ectx.ctx.tenant_id
        self.lookups: dict[str, object] = {}
        self.no_tax = 0

    def lookup(self, gstin: str):
        if gstin not in self.lookups:
            # The provider is chosen from the settings this run already
            # has, not a second read of the tenant's settings.
            self.lookups[gstin] = self.ectx.providers.call(
                self.ectx.ctx,
                "gstin",
                gstin,
                settings_store=_Settings(self.ectx.settings),
            )
        return self.lookups[gstin]

    def standing(self, invoice, vendor, out, factors) -> None:
        gstin = _gstin_of(invoice, vendor)
        if gstin is None:
            return
        inv_id = invoice["id"]
        base = [inv_id] + ([vendor["id"]] if vendor else [])
        if not tax_ids.validate_gstin(gstin)[0]:
            out.append(
                _spec(
                    UNVERIFIED,
                    invoice,
                    inv_id,
                    "The supplier's GSTIN fails its checksum; held until "
                    "it is corrected.",
                    base,
                    reason="checksum",
                    gstin=gstin,
                )
            )
            return
        result = self.lookup(gstin)
        cites = [inv_id, result.evidence_node or inv_id]
        status = str(result.data.get("status") or "").strip().lower()
        if result.status != "ok" or status not in _STATUSES:
            reason = "unavailable" if result.status != "ok" else "unknown"
            out.append(
                _spec(
                    UNVERIFIED,
                    invoice,
                    inv_id,
                    "The GST registry could not confirm the supplier's "
                    "GSTIN; held until it can.",
                    cites,
                    reason=reason,
                    gstin=gstin,
                )
            )
            return
        if status != "active":
            out.append(
                _spec(
                    INACTIVE,
                    invoice,
                    inv_id,
                    f"The supplier's GST registration is {status}.",
                    cites,
                    gstin=gstin,
                    status=status,
                )
            )
            factors.append(("risk_inactive", cites))
        missed = int(result.data.get("missed_periods") or 0)
        if missed >= self.ectx.threshold("gst.nonfiler_missed_periods"):
            out.append(
                _spec(
                    NONFILER,
                    invoice,
                    inv_id,
                    f"The supplier has missed {missed} GST return periods.",
                    cites,
                    gstin=gstin,
                    missed_periods=missed,
                )
            )
            factors.append(("risk_nonfiler", cites))

    def itc_risk(self, invoice, factors, out) -> None:
        if not factors:
            return
        tax = invoice["attrs"].get("tax_amount")
        if tax in (None, ""):
            self.no_tax += 1
            return
        name, cites = max(
            factors, key=lambda f: Decimal(str(self._factor(f[0])))
        )
        factor = Decimal(str(self._factor(name)))
        out.append(
            _spec(
                ITC_RISK,
                invoice,
                invoice["id"],
                "Input tax credit on this invoice is at risk.",
                [invoice["id"], *cites],
                impact=Decimal(str(tax)) * factor,
                factor=str(factor),
                signals=sorted({n for n, _ in factors}),
            )
        )

    def _factor(self, name: str) -> float:
        return self.ectx.threshold(f"gst.{name}")

    def invoice(self, invoice: dict, vendor: dict | None) -> list[FindingSpec]:
        out: list[FindingSpec] = []
        factors: list[tuple[str, list[str]]] = []
        self.standing(invoice, vendor, out, factors)
        self.itc_risk(invoice, factors, out)
        return out


def _vendor_of(ectx: EngineContext, invoice_id: str) -> dict | None:
    for e in ectx.graph.edges(
        ectx.ctx.tenant_id, src=invoice_id, kind="issued_by"
    ):
        return ectx.graph.node(ectx.ctx.tenant_id, e["dst"])
    return None


def run(ectx: EngineContext) -> list[FindingSpec]:
    tid = ectx.ctx.tenant_id
    if ectx.document is not None:
        if ectx.invoice_node is None:
            return []  # a contract: nothing GST to check
        node = ectx.graph.node(tid, ectx.invoice_node)
        invoices = [node] if node else []
    else:
        invoices = ectx.graph.nodes(tid, kind="invoice")
    r = _Run(ectx)
    out: list[FindingSpec] = []
    for invoice in invoices:
        out.extend(r.invoice(invoice, _vendor_of(ectx, invoice["id"])))
    if r.no_tax:
        logger.info(
            "gst_itc_tax_amount_unknown",
            extra={"engine": ENGINE, "invoices": r.no_tax},
        )
    return out
