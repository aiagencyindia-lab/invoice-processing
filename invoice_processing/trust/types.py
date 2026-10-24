"""Every finding type, its engine, whether it holds payment, and the trust
dimension it speaks to.

A static table rather than the engines' registrations: the API process never
imports the engine packages, so without this it knew only the types
`findings/model.py` registers itself, and the engine and hold filters and the
open-holds count were wrong there. Engines still call `register_type`; a test
holds the two to agreement.
"""

from __future__ import annotations

from dataclasses import dataclass

DIMENSIONS = (
    "identity",
    "authorization",
    "reality",
    "price",
    "authenticity",
    "consistency",
)


@dataclass(frozen=True)
class Type:
    engine: str
    hold: bool
    dimension: str | None
    description: str


def _t(engine, hold, dimension, description) -> Type:
    return Type(engine, hold, dimension, description)


TABLE: dict[str, Type] = {
    "TRUST-ENGINE-FAILED": _t(
        "trust",
        True,
        None,
        "An engine that can hold payment failed in enforcing mode; "
        "held until it runs",
    ),
    # Leakage (FR-20)
    "LEAK-RATE": _t(
        "leakage", False, "price", "Unit price above the contract rate"
    ),
    "LEAK-ESCALATOR": _t(
        "leakage",
        False,
        "price",
        "Unit price above the escalated contract rate",
    ),
    "LEAK-TIER": _t(
        "leakage",
        False,
        "price",
        "Tier price not applied after the tier quantity was reached",
    ),
    "LEAK-REBATE": _t(
        "leakage",
        False,
        "price",
        "Rebate threshold reached with no rebate credit",
    ),
    "LEAK-DISCOUNT": _t(
        "leakage",
        False,
        "price",
        "Paid early in full without taking the early-payment discount",
    ),
    "LEAK-TERM": _t(
        "leakage", False, "price", "Invoice dated outside the contract term"
    ),
    "DUP-NEAR": _t(
        "leakage",
        False,
        "consistency",
        "Near-duplicate invoice from the same vendor",
    ),
    "DUP-SPLIT": _t(
        "leakage",
        False,
        "consistency",
        "Invoices split to stay under the high-value threshold",
    ),
    "DUP-CROSS": _t(
        "leakage",
        False,
        "consistency",
        "Same invoice recorded under two vendor records",
    ),
    "STMT-UNAPPLIED-CREDIT": _t(
        "leakage",
        False,
        "consistency",
        "Vendor statement credit we never applied",
    ),
    "STMT-DOUBLE-PAID": _t(
        "leakage", False, "consistency", "Invoice paid more than once"
    ),
    "STMT-DISPUTED": _t(
        "leakage", False, "consistency", "Vendor statement line unknown to us"
    ),
    "RADAR-RENEWAL": _t(
        "leakage",
        False,
        "price",
        "Contract renewal or expiry inside the notice window",
    ),
    "RADAR-ESCALATOR": _t(
        "leakage",
        False,
        "price",
        "Contract price escalation date inside the notice window",
    ),
    # Payment Trust (FR-21)
    "PAY-UNVERIFIED": _t(
        "payment",
        True,
        "identity",
        "The payee's bank account is not verified; payment is held",
    ),
    "PAY-NAME-MISMATCH": _t(
        "payment",
        True,
        "identity",
        "The bank's account name does not match the vendor; payment is held",
    ),
    "PAY-BEC": _t(
        "payment",
        True,
        "identity",
        "Several business-email-compromise signals on this invoice; "
        "payment is held",
    ),
    "PAY-BEC-SIGNAL": _t(
        "payment",
        False,
        "identity",
        "One business-email-compromise signal on this invoice",
    ),
    "PAY-ANOMALY": _t(
        "payment",
        False,
        "consistency",
        "The amount or timing is unusual for this vendor, or the vendor is new",
    ),
    # Provenance (FR-22)
    "PROV-SUSPECT": _t(
        "provenance",
        False,
        "authenticity",
        "Low authenticity score: channel, template, text layer or metadata",
    ),
    "PROV-TAMPER": _t(
        "provenance",
        True,
        "authenticity",
        "An approved invoice re-submitted with different document bytes",
    ),
    "PROV-KYC-SUSPECT": _t(
        "provenance",
        False,
        "authenticity",
        "A vendor's onboarding document scores low on authenticity",
    ),
    "PROV-EDITED": _t(
        "provenance",
        False,
        "authenticity",
        "The invoice document was edited and re-saved after it was issued",
    ),
    # GST/ITC (FR-26)
    "GST-INACTIVE": _t(
        "gst_itc",
        True,
        "identity",
        "The supplier's GST registration is not active",
    ),
    "GST-UNVERIFIED": _t(
        "gst_itc",
        True,
        "identity",
        "The supplier's GSTIN could not be verified",
    ),
    "GST-NONFILER": _t(
        "gst_itc",
        False,
        "identity",
        "The supplier has missed GST return periods",
    ),
    "GST-NEW-LARGE-FIRST": _t(
        "gst_itc",
        False,
        "identity",
        "A new supplier's invoice is far above the norm",
    ),
    "GST-SHARED-IDENTITY": _t(
        "gst_itc",
        False,
        "identity",
        "Two GSTINs share a PAN, bank account or domain",
    ),
    "GST-ITC-RISK": _t(
        "gst_itc", False, "identity", "Input tax credit at risk on this invoice"
    ),
    # Reality checks (FR-23)
    "REAL-CONTRADICTED": _t(
        "reality",
        True,
        "reality",
        "The delivery, hours or seats on record are fewer than invoiced",
    ),
    "REAL-PARTIAL": _t(
        "reality",
        False,
        "reality",
        "Some of what was invoiced is corroborated, not all of it",
    ),
    "REAL-NO-EVIDENCE": _t(
        "reality",
        False,
        "reality",
        "Nothing on record corroborates what was invoiced",
    ),
    "REAL-PRICE-JUMP": _t(
        "reality",
        False,
        "price",
        "Unit price far above this vendor's history, with no contract change",
    ),
    "REAL-VOLUME-SPIKE": _t(
        "reality",
        False,
        "consistency",
        "Quantity far above this vendor's usual volume",
    ),
    "REAL-SHUTDOWN": _t(
        "reality",
        False,
        "reality",
        "Billed for a period the organisation was shut down",
    ),
    # Wave 4: agent control. Never a payment hold: enforcing refuses the
    # agent's action instead (program plan U3).
    "AGENT-OUT-OF-MANDATE": _t(
        "agents",
        False,
        "authorization",
        "An agent acted without an active mandate covering the action",
    ),
    "AGENT-SELF-APPROVAL": _t(
        "agents",
        False,
        "authorization",
        "An agent decided something it initiated",
    ),
    "AGENT-SAME-OWNER-CHAIN": _t(
        "agents",
        False,
        "authorization",
        "One owner's agents (or the owner and their agent) form an approval chain",
    ),
    "AGENT-OVER-LIMIT": _t(
        "agents",
        False,
        "authorization",
        "An agent action exceeds its mandate's spend cap",
    ),
    "AGENT-UNVERIFIED-VENDOR": _t(
        "agents",
        False,
        "authorization",
        "An agent paid or approved toward an agent-created vendor no person verified",
    ),
    "AGENT-CIRCULAR": _t(
        "agents",
        False,
        "authorization",
        "An agent approves an invoice to a vendor its owner's agents created",
    ),
    "AGENT-SPLIT": _t(
        "agents",
        False,
        "authorization",
        "An agent split one purchase into approvals each under its cap",
    ),
    "AGENT-VELOCITY": _t(
        "agents",
        False,
        "authorization",
        "An agent acted unusually fast",
    ),
    "AGENT-INJECTION": _t(
        "agents",
        False,
        "authorization",
        "A document an agent acted on carries instructions aimed at agents",
    ),
    "AGENT-DRIFT": _t(
        "agents",
        False,
        "authorization",
        "An agent handled an amount far beyond its own recent past",
    ),
}
