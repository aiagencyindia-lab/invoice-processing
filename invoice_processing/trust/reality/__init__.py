"""Reality checks (FR-23): is what was invoiced corroborated by deliveries,
hours or seats on record?

Per invoice: classify it (goods, services, subscription, other), sum the
evidence the graph holds for that kind (receipts and delivered shipments on
its PO; timesheets and tickets in its service period; the latest seat count),
band it against what was invoiced, and record a `reality_check` node with
`evidences` edges. Only a contradiction holds (U3); a partial match, no
evidence and the plausibility signals (`plausibility.py`) are insights. The
providers are asked only when the graph holds nothing, and every answer is a
cited `provider_evidence` node.
"""

from __future__ import annotations

import calendar
import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from functools import cached_property

from ... import masking
from ..engine import EngineContext
from ..findings.model import Citation, FindingSpec, register_type
from ..types import TABLE
from . import bands, classify, goods, plausibility, services, subscriptions
from .classify import iso_date

logger = logging.getLogger(__name__)

ENGINE = "reality"
ENGINE_VERSION = "reality-1"
RULE_VERSION = "FR-23-v1"
SOURCE = "reality"

for _code, _t in TABLE.items():
    if _t.engine == ENGINE:
        register_type(
            _code,
            _t.description,
            engine=ENGINE,
            hold=_t.hold,
            dimension=_t.dimension,
        )

CODE = {
    bands.CONTRADICTED: "REAL-CONTRADICTED",
    bands.PARTIAL: "REAL-PARTIAL",
    bands.NO_EVIDENCE: "REAL-NO-EVIDENCE",
}
CHECKS = {
    "goods": goods.check,
    "services": services.check,
    "subscription": subscriptions.check,
}
TOLERANCE = {
    "goods": "reality.qty_tolerance_pct",
    "services": "reality.hours_tolerance_pct",
    "subscription": "reality.seats_tolerance_pct",
}
CONFIDENCE = {
    bands.CONTRADICTED: 0.9,
    bands.PARTIAL: 0.7,
    bands.NO_EVIDENCE: 0.5,
}
# The graph kinds that evidence a delivery, the work, or the seats in use.
EVIDENCE_KINDS = ("grn", "shipment", "ticket", "timesheet", "seat_count")
CENT = Decimal("0.01")


def num(value: Decimal) -> str:
    """100.0000 -> "100", 2.50 -> "2.5": a quantity, not a money string."""
    out = format(value.normalize(), "f")
    return "0" if out in ("-0", "") else out


class _Settings:
    def __init__(self, settings: dict):
        self.settings = settings

    def get(self, tenant_id: str) -> dict:
        return self.settings


class Run:
    """One engine run: tenant-wide indexes built once, then per invoice.

    ponytail: every index is a tenant scan even for one document's run;
    narrow them to the invoice's vendor and PO if a tenant passes ~50k
    evidence rows.
    """

    def __init__(self, ectx: EngineContext):
        self.ectx = ectx
        self.tid = ectx.ctx.tenant_id
        self.window = int(ectx.threshold("reality.evidence_window_days"))

    # -- indexes -----------------------------------------------------------

    def _edges(self, kind: str) -> list[dict]:
        return self.ectx.graph.edges(self.tid, kind=kind)

    @cached_property
    def canonical(self) -> dict[str, str]:
        return self.ectx.links.canonical_map(self.tid)

    def canon(self, node_id: str | None) -> str | None:
        return self.canonical.get(node_id, node_id) if node_id else None

    @cached_property
    def nodes(self) -> dict[str, dict]:
        out = {}
        for kind in ("invoice_line", "grn_line", *EVIDENCE_KINDS):
            for n in self.ectx.graph.nodes(self.tid, kind=kind):
                out[n["id"]] = n
        return out

    @cached_property
    def parts(self) -> dict[str, list[dict]]:
        """Parent id -> its invoice or receipt lines, in line order."""
        out: dict[str, list[dict]] = defaultdict(list)
        for e in self._edges("part_of"):
            n = self.nodes.get(e["src"])
            if n and n["kind"] in ("invoice_line", "grn_line"):
                out[e["dst"]].append(n)
        for lines in out.values():
            lines.sort(key=lambda n: n["attrs"].get("line_number") or 0)
        return out

    @cached_property
    def vendor_of(self) -> dict[str, str]:
        return {
            e["src"]: self.canon(e["dst"]) for e in self._edges("issued_by")
        }

    @cached_property
    def po_of(self) -> dict[str, str]:
        return {e["src"]: e["dst"] for e in self._edges("references")}

    @cached_property
    def fulfils(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = defaultdict(list)
        for e in self._edges("fulfils"):
            if e["src"] in self.nodes:
                out[e["dst"]].append(self.nodes[e["src"]])
        return out

    @cached_property
    def by_vendor(self) -> dict[tuple[str, str], list[dict]]:
        """(canonical vendor, kind) -> evidence nodes it issued."""
        out: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for n in self.nodes.values():
            if n["kind"] in EVIDENCE_KINDS and n["id"] in self.vendor_of:
                out[(self.vendor_of[n["id"]], n["kind"])].append(n)
        return out

    @cached_property
    def proofs(self) -> dict[str, list[str]]:
        """Invoice node -> the proof `document_version` nodes of its
        fulfilled evidence requests."""
        from .requests import RequestStore  # noqa: PLC0415

        docs = RequestStore(self.ectx.url).proofs(self.tid)
        refs = self.ectx.graph.nodes_by_ref(
            self.tid,
            "documents",
            sorted({d for ds in docs.values() for d in ds}),
        )
        return {
            inv: [refs[d] for d in ds if d in refs] for inv, ds in docs.items()
        }

    # -- per invoice -------------------------------------------------------

    def lines(self, invoice: dict) -> list[dict]:
        return self.parts.get(invoice["id"], [])

    def vendor(self, invoice: dict) -> dict | None:
        vid = self.vendor_of.get(invoice["id"])
        return self.ectx.graph.node(self.tid, vid) if vid else None

    def vendor_name(self, invoice: dict) -> str:
        v = self.vendor(invoice)
        return (v or {}).get("attrs", {}).get("name") or (
            invoice["attrs"].get("vendor_name") or ""
        )

    def period(self, invoice: dict) -> tuple[date, date] | None:
        a = invoice["attrs"]
        start = iso_date(a.get("service_period_start"))
        end = iso_date(a.get("service_period_end"))
        if start and end:
            return start, end
        day = iso_date(a.get("invoice_date"))
        if day is None:
            return None
        last = calendar.monthrange(day.year, day.month)[1]
        return day.replace(day=1), day.replace(day=last)

    def late(self, invoice: dict, when: date | None) -> bool:
        day = iso_date(invoice["attrs"].get("invoice_date"))
        return bool(day and when and when > day + timedelta(days=self.window))

    def ask(self, check: bands.Check, capability: str, *args, keys) -> None:
        """A provider's answer as evidence: cited whatever it says, counted
        only when it carries a number (the fake's "unknown" carries none)."""
        result = self.ectx.providers.call(
            self.ectx.ctx,
            capability,
            *args,
            settings_store=_Settings(self.ectx.settings),
        )
        if result.evidence_node:
            check.evidence.append(result.evidence_node)
        if result.status != "ok":
            return
        data = result.data
        items = (
            data.get("items") if isinstance(data.get("items"), list) else [data]
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            value = next(
                (
                    v
                    for k in keys
                    if (v := classify.dec(item.get(k))) is not None
                ),
                None,
            )
            if value is None:
                continue
            check.present = True
            status = str(item.get("status") or "delivered").lower()
            if capability != "logistics" or status == "delivered":
                check.found += value

    # -- outputs -----------------------------------------------------------

    def record(self, invoice: dict, check: bands.Check, result: str) -> str:
        node = self.ectx.graph.upsert_node(
            self.ectx.ctx,
            kind="reality_check",
            natural_key=f"reality:{invoice['natural_key']}",
            attrs={
                "result": result,
                "kind": check.kind,
                "expected": num(check.expected),
                "found": num(check.found),
                "unit": check.unit,
                "evidence": list(dict.fromkeys(check.evidence)),
                "window_days": self.window,
                "engine_version": ENGINE_VERSION,
            },
            at=self.ectx.now,
        )
        for ev in dict.fromkeys(check.evidence):
            self.ectx.graph.upsert_edge(
                self.ectx.ctx,
                src=ev,
                dst=invoice["id"],
                kind="evidences",
                source_system=SOURCE,
                provenance="system_record",
                confidence=1.0,
                observed_at=self.ectx.now,
                at=self.ectx.now,
            )
        return node

    def check(self, invoice: dict, kind: str) -> FindingSpec | None:
        check = CHECKS[kind](self, invoice, self.lines(invoice))
        if check is None:
            return None
        if not check.present and self.proofs.get(invoice["id"]):
            # A proof a person attached stands in for missing evidence; it
            # never outweighs evidence that says otherwise.
            check.present = True
            check.found = check.expected
            check.evidence.extend(self.proofs[invoice["id"]])
        tol = Decimal(str(self.ectx.threshold(TOLERANCE[kind])))
        result = bands.band(check.expected, check.found, check.present, tol)
        node = self.record(invoice, check, result)
        if result == bands.CORROBORATED:
            return None
        impact = None
        if result != bands.NO_EVIDENCE and check.unit_price is not None:
            short = max(check.expected - check.found, Decimal(0))
            impact = (short * check.unit_price).quantize(CENT)
        summary = (
            f"Invoiced {num(check.expected)} {check.unit}; no evidence on "
            "record corroborates it."
            if result == bands.NO_EVIDENCE
            else f"Invoiced {num(check.expected)} {check.unit}; the evidence "
            f"on record shows {num(check.found)}."
        )
        return spec(
            CODE[result],
            invoice,
            "",
            summary,
            [invoice["id"], node, *check.evidence],
            impact=impact,
            confidence=CONFIDENCE[result],
            result=result,
            kind=check.kind,
            expected=num(check.expected),
            found=num(check.found),
            unit=check.unit,
            unit_price=None
            if check.unit_price is None
            else str(check.unit_price.quantize(CENT)),
            window_days=self.window,
            reality_node=node,
        )


def spec(
    code, invoice, suffix, summary, citations, *, impact, confidence, **details
) -> FindingSpec:
    return FindingSpec(
        type=code,
        transaction_node=invoice["id"],
        # Natural keys only: a re-versioned node gets a new id (spec §3.1).
        dedupe_key=f"{code}:{invoice['natural_key']}{suffix}",
        impact=impact,
        currency=invoice["attrs"].get("currency") or "INR",
        confidence=confidence,
        engine_version=ENGINE_VERSION,
        rule_version=RULE_VERSION,
        model_version="none",
        summary=summary,
        details=masking.mask_row(details),
        citations=tuple(Citation(n) for n in dict.fromkeys(citations)),
    )


def run(ectx: EngineContext) -> list[FindingSpec]:
    tid = ectx.ctx.tenant_id
    if ectx.document is not None:
        if ectx.invoice_node is None:
            return []  # a contract or a proof: no invoice to check
        node = ectx.graph.node(tid, ectx.invoice_node)
        invoices = [node] if node else []
    else:
        invoices = ectx.graph.nodes(tid, kind="invoice")
    r = Run(ectx)
    out: list[FindingSpec] = []
    kinds: dict[str, int] = defaultdict(int)
    history = plausibility.History(r)
    for invoice in invoices:
        kind = classify.classify(invoice, r.lines(invoice))
        kinds[kind] += 1
        if kind in CHECKS:
            found = r.check(invoice, kind)
            if found is not None:
                out.append(found)
        out.extend(plausibility.findings(r, invoice, history))
        out.extend(plausibility.shutdown(r, invoice))
    logger.info(
        "reality_checked",
        extra={"engine": ENGINE, "invoices": len(invoices), **kinds},
    )
    return out
