"""Engine readiness (U2): what each engine found and how people ruled on it
since it entered its mode, and what the evidence recommends. It only
recommends: an administrator moves the mode, with a reason
(`modes.set_engine_mode`).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from ...db.models import TrustHold
from ...db.session import tenant_session
from .. import modes as modes_mod
from .. import thresholds
from ..findings import model as finding_model
from ..types import TABLE
from . import dimensions as dm

OBSERVING = frozenset({"shadow", "advisory"})
TRIAGED_BY_PEOPLE = frozenset({"accepted", "rejected"})


def insight_only(engine: str) -> bool:
    """An engine that never holds payment nor refuses anything: enforcing
    changes nothing."""
    if engine in modes_mod.BLOCKING_ENGINES:
        return False
    return engine not in modes_mod.HOLD_ENGINES and not any(
        t.engine == engine and t.hold for t in TABLE.values()
    )


def recommend(
    mode: str,
    *,
    days: int,
    findings: int,
    decided: int,
    precision: float | None,
    failures: int,
    t: dict,
    insight_only: bool,
) -> dict:
    """{to: the next mode or None, reason}."""
    min_days = int(t["readiness.min_days"])
    if mode == "off":
        return {
            "to": "shadow",
            "reason": "Shadow mode observes and holds nothing.",
        }
    if mode == "enforcing":
        return {"to": None, "reason": "Already enforcing."}
    unmet = []
    if days < min_days:
        unmet.append(f"needs {min_days} days in {mode} (has {days})")
    if mode == "shadow":
        need = int(t["readiness.min_findings"])
        if findings < need:
            unmet.append(f"needs {need} findings (has {findings})")
        to = "advisory"
    elif insight_only:
        return {
            "to": None,
            "reason": "Stay: enforcing adds nothing, this engine raises no holds.",
        }
    else:
        need = int(t["readiness.min_decided"])
        floor = float(t["readiness.min_precision"])
        if decided < need:
            unmet.append(f"needs {need} decided findings (has {decided})")
        if precision is None or precision < floor:
            shown = "none" if precision is None else f"{precision:.2f}"
            unmet.append(f"needs precision {floor:.2f} (has {shown})")
        if failures:
            unmet.append(f"had {failures} engine failure(s)")
        to = "enforcing"
    if unmet:
        return {"to": None, "reason": "Stay: " + "; ".join(unmet) + "."}
    return {"to": to, "reason": f"Ready for {to}."}


def _at(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def row(
    engine: str,
    *,
    mode: str,
    since: datetime | None,
    now: datetime,
    findings: list[dict],
    events: list[dict],
    holds: list[dict],
    t: dict,
) -> dict:
    """`findings` and `events` are the tenant's since `since`; `holds` every
    ledger row ({engine, placed_at, lifted_at})."""
    own = [f for f in findings if dm.engine_of(f) == engine]
    failed = [
        f
        for f in findings
        if f["type"] == finding_model.ENGINE_FAILED
        and (f.get("details") or {}).get("engine") == engine
    ]
    found = [f for f in own if f["type"] != finding_model.ENGINE_FAILED]
    would = [
        f
        for f in found
        if dm.is_hold(f) and (f.get("details") or {}).get("mode") in OBSERVING
    ]
    ruled: dict[str, set] = {
        "decided": set(),
        "accepted": set(),
        "rejected": set(),
    }
    for e in events:
        if e["actor"] == "system":
            continue
        if finding_model.TYPE_ENGINE.get(e["type"]) != engine:
            continue
        if e["to_status"] in finding_model.DECIDED:
            ruled["decided"].add(e["finding_id"])
        if e["to_status"] in TRIAGED_BY_PEOPLE:
            ruled[e["to_status"]].add(e["finding_id"])
    accepted, rejected = len(ruled["accepted"]), len(ruled["rejected"])
    precision = (
        round(accepted / (accepted + rejected), 4)
        if accepted + rejected
        else None
    )
    mine = [h for h in holds if h["engine"] == engine]
    held = [h for h in mine if since is None or _at(h["placed_at"]) >= since]
    days = (now - since).days if since else 0
    triage = {
        "decided": len(ruled["decided"]),
        "accepted": accepted,
        "rejected": rejected,
        "precision": precision,
    }
    return {
        "engine": engine,
        "mode": mode,
        "since": since.isoformat() if since else None,
        "days_in_mode": days,
        "findings": len(found),
        "would_have_held": len(would),
        "held": len(held),
        "open_holds": sum(1 for h in mine if h["lifted_at"] is None),
        "triage": triage,
        "failures": len(failed),
        "recommendation": recommend(
            mode,
            days=days,
            findings=len(found),
            decided=triage["decided"],
            precision=precision,
            failures=len(failed),
            t=t,
            insight_only=insight_only(engine),
        ),
    }


def hold_rows(tenant_id: str, url: str | None = None) -> list[dict]:
    with tenant_session(tenant_id, url) as s:
        return [
            {
                "engine": h.engine,
                "type": h.type,
                "placed_at": _at(h.placed_at),
                "lifted_at": _at(h.lifted_at),
            }
            for h in s.scalars(
                select(TrustHold).where(TrustHold.tenant_id == tenant_id)
            )
        ]


def report(
    tenant_id: str,
    *,
    findings,
    settings_store=None,
    url: str | None = None,
    now: datetime | None = None,
    engines=modes_mod.ENGINES,
) -> list[dict]:
    from ... import tenant_settings  # noqa: PLC0415 - avoids an import cycle

    now = now or datetime.now(UTC)
    settings = tenant_settings.effective(tenant_id, settings_store)
    current = settings.get("trust_engine_modes") or {}
    t = thresholds.merged(settings.get("trust_thresholds"))
    holds = hold_rows(tenant_id, url)
    since_of = {
        e: _at(modes_mod.mode_since(tenant_id, e, settings_store))
        for e in engines
    }
    # One read of each, from the earliest clock (all time when any engine
    # never moved through set_engine_mode), cut per engine below.
    known = [v for v in since_of.values() if v is not None]
    floor = min(known) if len(known) == len(since_of) else None
    all_findings = findings.list(tenant_id, since=floor)
    all_events = findings.events(tenant_id, since=floor)
    created = {
        e["finding_id"]: _at(e["at"])
        for e in all_events
        if e["from_status"] is None and e.get("at")
    }
    out = []
    for engine in engines:
        since = since_of[engine]

        def after(at, since=since):
            return since is None or (at is not None and at >= since)

        out.append(
            row(
                engine,
                mode=current.get(engine, "off"),
                since=since,
                now=now,
                findings=[
                    f for f in all_findings if after(created.get(f["id"]))
                ],
                events=[e for e in all_events if after(_at(e.get("at")))],
                holds=holds,
                t=t,
            )
        )
    return out
