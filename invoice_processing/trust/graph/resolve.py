"""Deterministic entity resolution (PRD FR-19.4).

Statutory ids decide; names and domains suggest; nothing below 0.7 is ever
linked. Scores on agreeing keys combine noisy-or; two different GSTINs (or
ABNs) are a contradiction and score zero whatever the name says.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Literal

from ...ports import RunContext
from ..decide import Question, decide

_SUFFIXES = {
    "pvt",
    "private",
    "ltd",
    "limited",
    "llp",
    "inc",
    "co",
    "company",
    "corp",
    "corporation",
    "the",
}
_KEY_SCORE = {
    "gstin": 1.0,
    "abn": 1.0,
    "pan": 0.98,
    "bank_account": 0.97,
    "name": 0.85,
    "email_domain": 0.75,
}
_HARD = ("gstin", "abn")


def normalize_name(s: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split()
    return " ".join(w for w in words if w not in _SUFFIXES)


def natural_keys(attrs: dict) -> dict:
    out: dict = {}
    gstin = (
        (
            attrs.get("gstin")
            or attrs.get("vendor_tax_id")
            or attrs.get("tax_id")
            or ""
        )
        .strip()
        .upper()
    )
    if len(gstin) == 15:
        out["gstin"] = gstin
        out["pan"] = gstin[2:12]
    pan = (attrs.get("pan") or "").strip().upper()
    if len(pan) == 10:
        out["pan"] = pan
    abn = re.sub(r"\D", "", attrs.get("abn") or "")
    if len(abn) == 11:
        out["abn"] = abn
    acct = re.sub(
        r"\s",
        "",
        attrs.get("bank_account_number") or attrs.get("bank_account") or "",
    )
    code = (attrs.get("bank_code") or "").strip().upper()
    if acct and not acct.startswith("…"):
        out["bank_account"] = f"{acct}|{code}"
    name = normalize_name(
        attrs.get("name")
        or attrs.get("vendor_name")
        or attrs.get("vendor_key")
        or ""
    )
    if name:
        out["name"] = name
    domain = (attrs.get("email_domain") or "").strip().lower()
    if "@" in domain:
        domain = domain.rsplit("@", 1)[1]
    if domain:
        out["email_domain"] = domain
    return out


def score(a: dict, b: dict) -> tuple[float, str]:
    for key in _HARD:
        if a.get(key) and b.get(key) and a[key] != b[key]:
            return 0.0, f"{key}-conflict"
    for key in _HARD:  # a statutory id decides on its own
        if a.get(key) and a[key] == b.get(key):
            return 1.0, key
    agreeing = [
        k for k, s in _KEY_SCORE.items() if a.get(k) and a.get(k) == b.get(k)
    ]
    if not agreeing:
        return 0.0, "none"
    miss = 1.0
    for k in agreeing:
        miss *= 1.0 - _KEY_SCORE[k]
    return round(1.0 - miss, 4), "+".join(agreeing)


def band(value: float) -> Literal["auto", "proposed", "none"]:
    if value >= 0.95:
        return "auto"
    if value >= 0.7:
        return "proposed"
    return "none"


def candidates(nodes: list[dict]) -> list[tuple[dict, dict]]:
    """Pairs that share at least one key value: blocking, not an n² scan.

    # ponytail: exact-value blocking; add trigram blocking if a tenant exceeds
    # ~50k vendors.
    """
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for n in nodes:
        for k, v in (n.get("natural_keys") or {}).items():
            buckets[(k, v)].append(n)
    seen: set[tuple[str, str]] = set()
    pairs = []
    for group in buckets.values():
        for i, x in enumerate(group):
            for y in group[i + 1 :]:
                pair = tuple(sorted((x["id"], y["id"])))
                if pair not in seen:
                    seen.add(pair)
                    pairs.append((x, y) if x["id"] < y["id"] else (y, x))
    return pairs


def run(
    ctx: RunContext, graph, links, *, kind: str = "vendor", decider=None
) -> dict:
    counts = {"auto": 0, "proposed": 0}
    existing = {
        (link["node_a"], link["node_b"])
        for link in links.open_links(ctx.tenant_id)
    }
    for x, y in candidates(graph.nodes(ctx.tenant_id, kind=kind)):
        if (x["id"], y["id"]) in existing:
            continue
        value, method = score(
            x.get("natural_keys") or {}, y.get("natural_keys") or {}
        )
        which = band(value)
        if which == "none":
            continue
        reason = None
        if which == "proposed":
            [answer] = decide(
                {"a": x.get("natural_keys"), "b": y.get("natural_keys")},
                [
                    Question(
                        "same",
                        "yes_no",
                        "Are these the same vendor?",
                        fallback=False,
                    )
                ],
                decider,
            )
            verdict = (
                "unknown"
                if answer.fallback
                else ("yes" if answer.value else "no")
            )
            reason = f"model says: {verdict}"
        links.propose_or_link(
            ctx, x["id"], y["id"], value, method, reason=reason
        )
        counts[which] += 1
    return counts
