"""Payment Trust (FR-21): hold payment to unverified or impersonated payees and
flag behavioural anomalies.

Per document it checks the document's invoice; in a batch run, every open
invoice (one no payment `settles`). Bank verification goes only through the
`bank_verification` provider, and only when the vendor master was never
verified by two people; anything but `verified` holds (fail closed, §3.1.4).
Money at risk is the invoice amount; an anomaly's impact is the amount above
the vendor's median. Raw account numbers never leave the vendor row: details
and excerpts carry `masking.mask_bank` forms only.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from statistics import median

from sqlalchemy import select

from ... import masking
from ...db.models import (
    HistoryRecord,
    TrustHistoryImport,
    Vendor,
    VendorBankChange,
)
from ...db.session import tenant_session
from ..findings.model import Citation, FindingSpec, register_type
from . import signals as sg

logger = logging.getLogger(__name__)

ENGINE_VERSION = "payment-1"
ENGINE = "payment"

UNVERIFIED = "PAY-UNVERIFIED"
NAME_MISMATCH = "PAY-NAME-MISMATCH"
ANOMALY = "PAY-ANOMALY"
BEC = "PAY-BEC"
BEC_SIGNAL = "PAY-BEC-SIGNAL"

register_type(
    UNVERIFIED,
    "The payee's bank account is not verified; payment is held",
    engine=ENGINE,
    hold=True,
)
register_type(
    NAME_MISMATCH,
    "The bank's account name does not match the vendor; payment is held",
    engine=ENGINE,
    hold=True,
)
register_type(
    BEC,
    "Several business-email-compromise signals on this invoice; payment is held",
    engine=ENGINE,
    hold=True,
)
register_type(
    BEC_SIGNAL,
    "One business-email-compromise signal on this invoice",
    engine=ENGINE,
)
register_type(
    ANOMALY,
    "The amount or timing is unusual for this vendor, or the vendor is new",
    engine=ENGINE,
)


class _OneTenant:
    """The settings evaluate() already loaded, as a store the registry reads."""

    def __init__(self, settings: dict):
        self.settings = settings

    def get(self, tenant_id):
        return self.settings


@dataclass(frozen=True)
class _Master:
    node: str
    name: str | None
    email_domain: str | None
    account: str | None
    code: str | None
    account_name: str | None
    verified_at: str | None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _norm_name(name: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (name or "").lower()))


class _World:
    """Everything one run reads, loaded once."""

    def __init__(self, ectx):
        self.ectx = ectx
        tid = ectx.ctx.tenant_id
        graph = ectx.graph
        self.canonical = ectx.links.canonical_map(tid)
        self.vendor_nodes = {
            n["id"]: n for n in graph.nodes(tid, kind="vendor")
        }
        self.members: dict[str, list[dict]] = {}
        for nid, n in self.vendor_nodes.items():
            self.members.setdefault(self._canon(nid), []).append(n)
        self.issued_by = {
            e["src"]: e["dst"] for e in graph.edges(tid, kind="issued_by")
        }
        self.txns: dict[str, list] = {}
        for t in ectx.transactions():
            if t.vendor_key:
                self.txns.setdefault(self._canon(t.vendor_key), []).append(t)
        self.changes: dict[str, list[str]] = {}
        for e in graph.edges(tid, kind="changes"):
            self.changes.setdefault(self._canon(e["dst"]), []).append(e["src"])
        self._masters: dict[str, _Master | None] = {}
        self._verified: dict[tuple, object] = {}

    def _canon(self, node_id: str) -> str:
        return self.canonical.get(node_id, node_id)

    # -- vendor master ---------------------------------------------------

    def master(self, vendor_node: str | None) -> _Master | None:
        if vendor_node is None:
            return None
        key = self._canon(vendor_node)
        if key not in self._masters:
            self._masters[key] = self._load_master(vendor_node, key)
        return self._masters[key]

    def _load_master(self, vendor_node: str, key: str) -> _Master:
        group = self.members.get(key) or [
            self.vendor_nodes.get(vendor_node)
            or {
                "id": vendor_node,
                "attrs": {},
                "ref_table": None,
                "ref_id": None,
            }
        ]
        # The v1 row (two-person bank changes) beats an imported record.
        group = sorted(
            group,
            key=lambda n: {"vendors": 0, "history_records": 1}.get(
                n.get("ref_table"), 2
            ),
        )
        best = group[0]
        tid = self.ectx.ctx.tenant_id
        attrs = best.get("attrs") or {}
        domain = next(
            (
                (n.get("attrs") or {}).get("email_domain")
                for n in group
                if (n.get("attrs") or {}).get("email_domain")
            ),
            None,
        )
        with tenant_session(tid, self.ectx.url) as s:
            if best.get("ref_table") == "vendors":
                v = s.get(Vendor, best["ref_id"])
                if v is not None and v.tenant_id == tid:
                    return _Master(
                        best["id"],
                        v.name,
                        v.email_domain or domain,
                        v.bank_account_number,
                        v.bank_code,
                        v.bank_account_name,
                        v.bank_verified_at
                        and _utc(v.bank_verified_at).isoformat(),
                    )
            if best.get("ref_table") == "history_records":
                rec = s.get(HistoryRecord, best["ref_id"])
                if rec is not None and rec.tenant_id == tid:
                    imp = s.get(TrustHistoryImport, rec.import_id)
                    header = {
                        field: h
                        for h, field in ((imp and imp.mapping) or {}).items()
                    }
                    fields = rec.fields or {}

                    def mapped(name: str) -> str | None:
                        value = fields.get(header.get(name, name))
                        return str(value).strip() or None if value else None

                    return _Master(
                        best["id"],
                        rec.vendor_key,
                        mapped("email_domain") or domain,
                        mapped("bank_account"),
                        mapped("bank_code"),
                        None,
                        None,
                    )
        return _Master(
            best["id"],
            attrs.get("name"),
            domain,
            None,
            attrs.get("bank_code"),
            attrs.get("bank_account_name"),
            attrs.get("bank_verified_at"),
        )

    def history(self, vendor_node: str | None) -> list:
        return (
            self.txns.get(self._canon(vendor_node), []) if vendor_node else []
        )

    def recent_change(
        self, vendor_node: str | None, days: float
    ) -> tuple | None:
        """(bank_change node, days ago) of the latest pending or approved change
        to this vendor's payee details within `days`."""
        if vendor_node is None:
            return None
        nodes = self.changes.get(self._canon(vendor_node)) or []
        if not nodes:
            return None
        tid = self.ectx.ctx.tenant_id
        refs = {}
        for nid in nodes:
            n = self.ectx.graph.node(tid, nid)
            if n and n.get("ref_table") == "vendor_bank_changes":
                refs[n["ref_id"]] = nid
        best = None
        with tenant_session(tid, self.ectx.url) as s:
            rows = s.scalars(
                select(VendorBankChange).where(
                    VendorBankChange.tenant_id == tid,
                    VendorBankChange.id.in_(list(refs)),
                )
            ).all()
            for row in rows:
                if row.status == VendorBankChange.REJECTED:
                    continue
                when = max(
                    t for t in (_utc(row.created_at), _utc(row.decided_at)) if t
                )
                ago = (self.ectx.now - when) / timedelta(days=1)
                if -1 <= ago <= days and (best is None or ago < best[1]):
                    best = (refs[row.id], max(ago, 0.0))
        return best

    # -- provider ----------------------------------------------------------

    def verify(self, m: _Master):
        name = m.account_name or m.name or ""
        key = (m.account, m.code, name)
        if key not in self._verified:
            self._verified[key] = self.ectx.providers.call(
                self.ectx.ctx,
                "bank_verification",
                m.account,
                m.code or "",
                name,
                settings_store=_OneTenant(self.ectx.settings),
            )
        return self._verified[key]


def _spec(
    ectx, code, inv, summary, *, impact, currency, confidence, details, cites
):
    return FindingSpec(
        type=code,
        transaction_node=inv["id"],
        dedupe_key=f"{code}:{inv['id']}",
        impact=impact,
        currency=currency if impact is not None else None,
        confidence=confidence,
        engine_version=ENGINE_VERSION,
        rule_version=f"FR-21:{code}",
        model_version="none",
        summary=summary,
        details=details,
        citations=tuple(
            Citation(n, sg.mask_text(x) if x else x) for n, x in cites
        ),
    )


def _amount(attrs: dict) -> Decimal | None:
    try:
        return Decimal(attrs["amount"]) if attrs.get("amount") else None
    except ArithmeticError:
        return None


def _name_score(data: dict, m: _Master) -> float | None:
    score = data.get("name_match")
    if isinstance(score, (int, float)):
        return float(score)
    returned = data.get("account_name")
    if returned:
        ours = m.account_name or m.name
        return difflib.SequenceMatcher(
            None, _norm_name(ours), _norm_name(returned)
        ).ratio()
    return None


def _verification(ectx, world, inv, vendor_node, m, amount, currency):
    out = []
    if m is not None and m.verified_at:
        return out
    cites = [(inv["id"], "invoice to be paid")]
    if vendor_node:
        cites.append((vendor_node, "vendor master"))
    if m is None or not m.account:
        out.append(
            _spec(
                ectx,
                UNVERIFIED,
                inv,
                "No verified bank account is on file for this vendor.",
                impact=amount,
                currency=currency,
                confidence=1.0,
                details={"reason": "no_account_on_file"},
                cites=cites,
            )
        )
        return out
    res = world.verify(m)
    result = res.data.get("result") if res.status == "ok" else None
    evidence = [(res.evidence_node, f"bank_verification: {res.status}")]
    masked = masking.mask_bank(m.account)
    if result != "verified":
        why = (
            "the verifier is unavailable"
            if res.status != "ok"
            else f"the verifier answered {result or 'nothing'}"
        )
        out.append(
            _spec(
                ectx,
                UNVERIFIED,
                inv,
                f"Bank account {masked} is not verified: {why}.",
                impact=amount,
                currency=currency,
                confidence=1.0,
                details={
                    "reason": "not_verified",
                    "account": masked,
                    "provider": res.provider,
                    "provider_status": res.status,
                    "result": result,
                },
                cites=evidence + cites,
            )
        )
    score = _name_score(res.data, m) if res.status == "ok" else None
    floor = ectx.threshold("payment.name_match_min")
    if score is not None and score < floor:
        out.append(
            _spec(
                ectx,
                NAME_MISMATCH,
                inv,
                f"The bank's name for account {masked} matches the vendor "
                f"only {score:.0%}.",
                impact=amount,
                currency=currency,
                confidence=1.0,
                details={
                    "account": masked,
                    "name_match": round(score, 4),
                    "name_match_min": floor,
                },
                cites=evidence + cites,
            )
        )
    return out


def _anomaly(ectx, world, inv, vendor_node, amount, currency):
    attrs = inv["attrs"]
    others = [
        t
        for t in world.history(vendor_node)
        if t.node_id != inv["id"]
        and t.amount is not None
        and (currency is None or t.currency in (None, currency))
    ]
    signals: dict = {}
    if len(others) < ectx.threshold("payment.min_history"):
        signals["first_time_vendor"] = {"history": len(others)}
    else:
        if amount is not None:
            z = sg.robust_z(amount, [t.amount for t in others])
            if z > ectx.threshold("payment.amount_zscore"):
                signals["amount_zscore"] = {
                    "z": round(z, 2) if z != float("inf") else "inf"
                }
        when = attrs.get("invoice_date")
        dates = [t.date for t in others if t.date]
        if when and dates:
            try:
                ratio = sg.nearest_gap_ratio(date.fromisoformat(when), dates)
            except ValueError:
                ratio = None
            if ratio is not None and ratio < ectx.threshold(
                "payment.cadence_ratio_min"
            ):
                signals["cadence"] = {"gap_ratio": round(ratio, 3)}
    if not signals:
        return []
    impact = None
    med = median(t.amount for t in others) if others else None
    if amount is not None and med is not None and amount > med:
        impact = (amount - med).quantize(Decimal("0.01"))
    cites = [(inv["id"], "invoice")]
    if vendor_node:
        cites.append(
            (vendor_node, f"vendor history: {len(others)} transactions")
        )
    return [
        _spec(
            ectx,
            ANOMALY,
            inv,
            "Unusual for this vendor: " + ", ".join(sorted(signals)) + ".",
            impact=impact,
            currency=currency,
            confidence=0.6,
            details={
                "signals": signals,
                "history": len(others),
                "median": str(med) if med is not None else None,
            },
            cites=cites,
        )
    ]


def _mail(ectx, source, source_ref) -> sg.Mail | None:
    if source != "email" or not source_ref:
        return None
    try:
        return sg.parse_email(ectx.blobs.get(source_ref))
    except Exception as exc:  # a missing blob is a gap, not a pass
        logger.warning(
            "payment_email_unreadable", extra={"error": type(exc).__name__}
        )
        return None


def _bec(ectx, world, inv, vendor_node, m, amount, currency):
    attrs = inv["attrs"]
    doc = ectx.document if ectx.document is not None else {}
    if doc:
        source, source_ref = doc.get("source"), doc.get("source_ref")
    else:
        source = attrs.get("document_source")
        source_ref = attrs.get("document_source_ref")
    mail = _mail(ectx, source, source_ref) or sg.Mail()
    sender = (
        sg.domain_of(doc.get("sender"))
        or attrs.get("document_sender_domain")
        or sg.domain_of(mail.sender)
    )
    reply = (
        sg.domain_of(doc.get("reply_to"))
        or attrs.get("document_reply_to_domain")
        or sg.domain_of(mail.reply_to)
    )
    vendor_domain = (m.email_domain if m else None) or None
    if vendor_domain and "@" in vendor_domain:
        vendor_domain = vendor_domain.rsplit("@", 1)[1]
    found: dict[str, str] = {}
    extra_cites = []
    if vendor_domain and sender and not sg.same_domain(sender, vendor_domain):
        found["sender_domain"] = (
            f"From domain {sender} is not the vendor's {vendor_domain}"
        )
        if sg.lookalike(
            vendor_domain, sender, ectx.threshold("payment.lookalike_ratio")
        ):
            found["lookalike_domain"] = (
                f"From domain {sender} looks like the vendor's {vendor_domain}"
            )
    if (
        reply
        and sender
        and not sg.same_domain(reply, sender)
        and not sg.same_domain(sender, reply)
        and not (vendor_domain and sg.same_domain(reply, vendor_domain))
    ):
        found["reply_to_domain"] = (
            f"Reply-To domain {reply} differs from From domain {sender}"
        )
    text = f"{mail.subject}\n{mail.body}"
    urgent = sg.urgency(text)
    if urgent:
        found["urgency"] = urgent
    if m is not None:
        differ = sg.bank_details_differ(text, m.account, m.code)
        if differ:
            found["body_bank_details"] = differ
    change = world.recent_change(
        vendor_node, ectx.threshold("payment.bank_change_recent_days")
    )
    if change:
        found["recent_bank_change"] = (
            f"Payee bank details changed {change[1]:.0f} days ago"
        )
        extra_cites.append((change[0], found["recent_bank_change"]))
    if not found:
        return []
    hold = len(found) >= ectx.threshold("payment.bec_hold_signals")
    code = BEC if hold else BEC_SIGNAL
    cites = [(inv["id"], "; ".join(found[k] for k in sorted(found)))]
    if vendor_node:
        cites.append((vendor_node, "vendor master"))
    names = sorted(found)
    return [
        _spec(
            ectx,
            code,
            inv,
            f"{len(found)} business-email-compromise signal"
            f"{'s' if len(found) > 1 else ''}: {', '.join(names)}.",
            impact=amount if hold else None,
            currency=currency,
            confidence=0.9 if hold else 0.5,
            details={
                "signals": names,
                "evidence": {k: sg.mask_text(v) for k, v in found.items()},
            },
            cites=cites + extra_cites,
        )
    ]


def _targets(ectx) -> list[dict]:
    tid = ectx.ctx.tenant_id
    if ectx.document is not None:
        if ectx.invoice_node is None:
            return []
        node = ectx.graph.node(tid, ectx.invoice_node)
        return [node] if node else []
    # ponytail: a batch walks every open invoice; page by vendor if a tenant's
    # open book passes ~10k invoices.
    settled = {e["dst"] for e in ectx.graph.edges(tid, kind="settles")}
    return [
        n
        for n in ectx.graph.nodes(tid, kind="invoice")
        if n["id"] not in settled
    ]


def run(ectx) -> list[FindingSpec]:
    targets = _targets(ectx)
    if not targets:
        return []
    world = _World(ectx)
    out: list[FindingSpec] = []
    for inv in targets:
        vendor_node = world.issued_by.get(inv["id"])
        m = world.master(vendor_node)
        amount = _amount(inv["attrs"])
        currency = inv["attrs"].get("currency")
        out += _verification(ectx, world, inv, vendor_node, m, amount, currency)
        out += _anomaly(ectx, world, inv, vendor_node, amount, currency)
        out += _bec(ectx, world, inv, vendor_node, m, amount, currency)
    return out
