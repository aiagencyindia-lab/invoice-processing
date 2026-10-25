"""GL coding: the tenant's rules, and a person coding one invoice line.

Rules are tenant policy, so only an admin writes them (a clerk reads, to see
why a line went where it did). A line's coding is a clerk's call, recorded as
"manual" so a re-run never overwrites it, and closed once the invoice is
decided: the account an approver signed off is the account that is exported.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import case_projection, gl_coding, po_matching
from .auth import Principal, require_role

logger = logging.getLogger(__name__)
router = APIRouter()

NEEDS_DB = "GL coding needs a database. Configure DATABASE_URL."

Account = Annotated[str, Field(max_length=64)]
Vendor = Annotated[str, Field(max_length=255)]
Keyword = Annotated[str, Field(max_length=200)]


class GlRuleIn(BaseModel):
    vendor_name: Vendor | None = None
    keyword: Keyword | None = None
    gl_account: Account
    enabled: bool = True


class GlRulePatch(BaseModel):
    vendor_name: Vendor | None = None
    keyword: Keyword | None = None
    gl_account: Account | None = None
    enabled: bool | None = None


class LineCoding(BaseModel):
    gl_account: Account | None = None
    remember: bool = False


def _repo():
    from . import service  # noqa: PLC0415

    try:
        repo = service.get_gl_rules()
    except Exception:
        logger.exception("GL rules could not be reached")
        raise HTTPException(503, "GL coding is unavailable.") from None
    if repo is None:
        raise HTTPException(503, NEEDS_DB)
    return repo


def _audit():
    from . import service  # noqa: PLC0415

    return service.get_audit()


def _blank(value) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _clean(vendor_name, keyword, gl_account) -> dict:
    """The stored shape of a rule, or a 400 saying why it could never code."""
    account = _blank(gl_account)
    if account is None:
        raise HTTPException(400, "A GL rule needs a GL account.")
    vendor, words = _blank(vendor_name), _blank(keyword)
    if vendor is None and words is None:
        raise HTTPException(
            400, "A GL rule needs a vendor, a keyword, or both to match on."
        )
    return {"vendor_name": vendor, "keyword": words, "gl_account": account}


@router.get("/api/gl-rules")
def list_rules(
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    return {"rules": _repo().list_rules(principal.tenant_id)}


@router.post("/api/gl-rules", status_code=201)
def create_rule(
    request: GlRuleIn,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    repo = _repo()
    fields = _clean(request.vendor_name, request.keyword, request.gl_account)
    rule = repo.create(
        principal.tenant_id,
        **fields,
        enabled=request.enabled,
        created_by=principal.email,
    )
    _audit().emit(principal.context(), "gl_rule_created", rule)
    return rule


@router.patch("/api/gl-rules/{rule_id}")
def update_rule(
    rule_id: str,
    request: GlRulePatch,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    repo, t = _repo(), principal.tenant_id
    before = repo.get(t, rule_id)
    if before is None:
        raise HTTPException(404, "Unknown GL rule")
    sent = request.model_dump(exclude_unset=True)
    fields = _clean(
        **{
            key: sent.get(key, before[key])
            for key in ("vendor_name", "keyword", "gl_account")
        }
    )
    if sent.get("enabled") is not None:
        fields["enabled"] = sent["enabled"]
    after = repo.update(t, rule_id, **fields)
    if after is None:
        raise HTTPException(404, "Unknown GL rule")
    _audit().emit(principal.context(), "gl_rule_updated", after, before=before)
    return after


@router.delete("/api/gl-rules/{rule_id}")
def delete_rule(
    rule_id: str,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    repo, t = _repo(), principal.tenant_id
    rule = repo.get(t, rule_id)
    if rule is None or not repo.delete(t, rule_id):
        raise HTTPException(404, "Unknown GL rule")
    _audit().emit(principal.context(), "gl_rule_deleted", rule)
    return {"deleted": True}


def _remember(repo, principal: Principal, vendor: str, account: str) -> None:
    """Point this vendor's vendor-only rule at `account`, creating it if new.

    Updated rather than added: equally specific rules tie to the oldest, so a
    second vendor-only rule would never code anything.
    """
    t, key = principal.tenant_id, po_matching.normalize_vendor(vendor)
    existing = next(
        (
            r
            for r in repo.list_rules(t)
            if not _blank(r["keyword"])
            and po_matching.normalize_vendor(r["vendor_name"]) == key
        ),
        None,
    )
    if existing is None:
        rule = repo.create(
            t,
            vendor_name=vendor,
            keyword=None,
            gl_account=account,
            created_by=principal.email,
        )
        _audit().emit(principal.context(), "gl_rule_created", rule)
        return
    rule = repo.update(t, existing["id"], gl_account=account, enabled=True)
    _audit().emit(principal.context(), "gl_rule_updated", rule, before=existing)


@router.patch("/api/invoices/{case_id}/lines/{line_number}/gl")
def code_line(
    case_id: str,
    line_number: int,
    request: LineCoding,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    from . import service  # noqa: PLC0415

    if request.remember and not principal.can("admin"):
        raise HTTPException(
            403,
            "Only an admin can remember an account for a vendor: the rule "
            "codes every future invoice from them.",
        )
    service.require_invoice_case(principal.tenant_id, case_id)
    service._require_unlocked(principal, case_id)
    ctx = principal.context(case_id)
    artifacts = service.get_cases().get_artifacts(ctx)
    if not artifacts:
        raise HTTPException(404, f"Unknown case {case_id}")
    decision = (artifacts.get(service.WORKFLOW_ARTIFACT) or {}).get("decision")
    if decision:
        raise HTTPException(
            409,
            "This invoice was already decided; its GL coding is closed.",
        )
    lines = case_projection.lines(artifacts)
    line = next((x for x in lines if x["line_number"] == line_number), None)
    if line is None:
        raise HTTPException(404, f"This invoice has no line {line_number}.")

    account = _blank(request.gl_account)
    invoice = case_projection.extraction(artifacts)
    vendor = _blank(invoice.get("vendor_name"))
    repo = None
    if request.remember:
        # All refusals before anything is written.
        if account is None:
            raise HTTPException(
                400, "Give the line an account to remember for this vendor."
            )
        if vendor is None:
            raise HTTPException(
                400,
                "This invoice has no vendor name, so there is nothing to "
                "remember the account against.",
            )
        if len(vendor) > 255:
            raise HTTPException(
                400,
                "This invoice's vendor name is too long to remember an "
                "account for (255 characters at most).",
            )
        repo = _repo()

    before: dict = {}

    def set_line(stored):
        """This one line on the coding as stored now, so a line someone else
        coded meanwhile is kept."""
        coding = (
            stored
            if isinstance(stored, dict)
            and isinstance(stored.get("lines"), list)
            else gl_coding.code_lines(invoice, lines, [])
        )
        entries = []
        for e in gl_coding.valid_entries(coding):
            if e["line_number"] == line_number:
                before["gl_account"] = e.get("gl_account")
            else:
                entries.append(e)
        entries.append(
            {
                "line_number": line_number,
                "gl_account": account,
                "source": "manual",
                "rule_id": None,
                "coded_by": principal.email,
                "coded_at": datetime.now(UTC).isoformat(),
            }
        )
        entries.sort(key=lambda e: e["line_number"])
        return {**coding, "lines": entries}

    coding = service.get_cases().update_artifact(ctx, "gl_coding", set_line)
    old = before.get("gl_account")
    artifacts = service.get_cases().get_artifacts(ctx)
    service._reproject(ctx, artifacts)
    service.get_audit().emit(
        ctx,
        "gl_line_coded",
        {"line_number": line_number, "gl_account": account},
        before={"line_number": line_number, "gl_account": old},
    )
    # After gl_line_coded so the trail reads cause then effect.
    if account != old:
        service.reroute(ctx, artifacts, ["gl_account"])
    if repo is not None:
        _remember(repo, principal, vendor, account)
    return coding
