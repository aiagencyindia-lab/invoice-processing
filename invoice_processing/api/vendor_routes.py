"""Suppliers, their verified bank details, and the two-person queue that
changes them.

Payment redirection is the fraud this exists for: a convincing email asks AP
to update a vendor's account, one person does, the next invoice is paid to the
attacker. So a bank change here is proposed by one person and applied by a
different one, and the repository refuses the same email in both roles.

Where no database is configured the supplier list still works from the
per-tenant file as it always did, and the bank-change routes say plainly that
they need a database: the API and the worker are separate containers in any
real deployment, and a queue on one container's disk is not a control.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import master_files, po_matching, vendor_master
from ..db import repositories as repos

# Last four only: the queue is read by clerks who must not be able to copy
# a full account number out of the UI. Shared so every surface masks alike.
from ..masking import mask_bank as mask
from .auth import Principal, require_role

router = APIRouter()

NEEDS_DB = "Bank-change control needs a database. Configure DATABASE_URL."


class VendorUpdate(BaseModel):
    vendors: list[dict[str, Any]]


class VendorPatch(BaseModel):
    email_domain: str | None = Field(default=None, max_length=255)


# Limits match the column widths: an over-long value would otherwise fail at
# flush as a 500, or be silently truncated on a lenient database.
class BankChangeProposal(BaseModel):
    bank_account_name: str | None = Field(default=None, max_length=255)
    bank_account_number: str | None = Field(default=None, max_length=64)
    bank_code: str | None = Field(default=None, max_length=64)
    bank_iban: str | None = Field(default=None, max_length=64)
    # Text column, but the reason is copied into every audit payload.
    reason: str = Field(default="", max_length=2000)
    source_case_id: str | None = Field(default=None, max_length=64)


class BankChangeDecision(BaseModel):
    action: str  # approve | reject
    note: str | None = None


def vendor_out(v: dict) -> dict:
    has_bank = v.get("bank_account_number") or v.get("bank_iban")
    if v.get("pending_change_id"):
        status = "pending"
    else:
        status = "verified" if has_bank else "unverified"
    return {
        "id": v["id"],
        "name": v["name"],
        "tax_id": v["tax_id"],
        "kind": v.get("kind", ""),
        "email_domain": v.get("email_domain"),
        "bank_status": status,
        "bank_account_masked": mask(has_bank),
        "bank_account_name": v.get("bank_account_name"),
        "bank_code": v.get("bank_code"),
        "bank_verified_at": v.get("bank_verified_at"),
        "bank_verified_by": v.get("bank_verified_by"),
        "pending_change_id": v.get("pending_change_id"),
    }


def change_out(c: dict, *, full: bool = False) -> dict:
    """`full` is for approvers, who must compare the real number before
    releasing it. Everything else, the audit trail included, gets the mask."""
    out = dict(c)
    if full:
        return out
    for key in (
        "proposed_account_number",
        "previous_account_number",
        "proposed_iban",
        "previous_iban",
    ):
        out[key] = mask(c.get(key))
    return out


def _repo():
    from . import service  # noqa: PLC0415

    return service.get_vendors()


def _audit():
    from . import service  # noqa: PLC0415

    return service.get_audit()


def _validate_list(records: list[dict]) -> None:
    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise HTTPException(400, f"Entry {index + 1} is not an object.")
        name = str(record.get("name") or "").strip()
        if not name:
            raise HTTPException(400, f"Entry {index + 1} has no supplier name.")
        if not str(record.get("tax_id") or "").strip():
            raise HTTPException(
                400,
                f"{name} has no tax ID. Without one there is nothing to "
                "compare an invoice against, and check 3.2 would report a "
                "match it never made.",
            )
        key = po_matching.normalize_vendor(name)
        if key in seen:
            raise HTTPException(
                400,
                f"{name} appears twice. The check would use whichever "
                "loaded last.",
            )
        seen.add(key)


@router.get("/api/vendors")
def list_vendors(
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    repo = _repo()
    if repo is None:
        from .. import paths  # noqa: PLC0415

        path = paths.vendor_master_file(principal.tenant_id)
        records = master_files.read_records(path, vendor_master.RECORD_KEY)
        # Through Vendor.from_dict so a hand-edited record missing a field
        # still lists. Only the plain fields: bank details never come from a
        # file, which has no queue behind it.
        vendors = []
        for r in records:
            v = vendor_master.Vendor.from_dict(r)
            vendors.append(
                vendor_out(
                    {
                        "id": "",
                        "name": v.name,
                        "tax_id": v.tax_id,
                        "kind": v.kind,
                        "email_domain": v.email_domain,
                    }
                )
            )
        return {"vendors": vendors, "count": len(vendors), "source": path.name}
    vendors = [vendor_out(v) for v in repo.list(principal.tenant_id)]
    return {"vendors": vendors, "count": len(vendors), "source": "database"}


@router.put("/api/vendors")
def replace_vendors(
    request: VendorUpdate,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    """Bulk-load name, tax ID and kind, validated in full before any write.

    Bank details and email domain are ignored on purpose: an export is one
    person's file, and bank details only change through the two-person queue.
    """
    _validate_list(request.vendors)
    plain = [
        {"name": r["name"], "tax_id": r["tax_id"], "kind": r.get("kind", "")}
        for r in request.vendors
    ]
    repo = _repo()
    if repo is None:
        from .. import paths  # noqa: PLC0415

        master_files.write_records(
            paths.vendor_master_file(principal.tenant_id),
            vendor_master.RECORD_KEY,
            plain,
        )
    else:
        repo.upsert_many(principal.tenant_id, plain)
    _audit().emit(
        principal.context(), "vendors_replaced", {"count": len(plain)}
    )
    return {"count": len(plain), "message": f"Loaded {len(plain)} suppliers."}


# The literal /api/vendors/bank-changes routes are declared before the
# /api/vendors/{vendor_id} ones so a vendor id can never shadow them, whatever
# methods are added to either later.
@router.get("/api/vendors/bank-changes")
def list_bank_changes(
    principal: Annotated[Principal, Depends(require_role("clerk"))],
    status: str | None = Query(default=None),
) -> dict:
    repo = _repo()
    if repo is None:
        raise HTTPException(503, NEEDS_DB)
    changes = [
        change_out(c, full=principal.can("approver"))
        for c in repo.list_changes(principal.tenant_id, status)
    ]
    return {"changes": changes, "count": len(changes)}


@router.post("/api/vendors/bank-changes/{change_id}/decide")
def decide_bank_change(
    change_id: str,
    request: BankChangeDecision,
    principal: Annotated[Principal, Depends(require_role("approver"))],
) -> dict:
    repo = _repo()
    if repo is None:
        raise HTTPException(503, NEEDS_DB)
    if request.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    if request.action == "reject" and not (request.note or "").strip():
        raise HTTPException(
            400, "A rejection needs a note the proposer can act on."
        )
    try:
        change = repo.decide(
            principal.tenant_id,
            change_id,
            action=request.action,
            decided_by=principal.email,
            note=request.note,
        )
    except KeyError:
        raise HTTPException(404, "Unknown bank change") from None
    except repos.SelfApproval:
        raise HTTPException(
            403, "The person who proposed a bank change cannot decide it."
        ) from None
    except repos.ChangeNotPending as exc:
        raise HTTPException(409, f"This change was already {exc}.") from None
    _audit().emit(
        principal.context(change["vendor_id"]),
        "vendor_bank_change_approved"
        if request.action == "approve"
        else "vendor_bank_change_rejected",
        change_out(change),
    )
    return change_out(change, full=principal.can("approver"))


@router.patch("/api/vendors/{vendor_id}")
def patch_vendor(
    vendor_id: str,
    request: VendorPatch,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict:
    repo = _repo()
    if repo is None:
        raise HTTPException(503, NEEDS_DB)
    before = repo.get(principal.tenant_id, vendor_id)
    if before is None:
        raise HTTPException(404, "Unknown vendor")
    after = repo.patch(
        principal.tenant_id, vendor_id, email_domain=request.email_domain
    )
    _audit().emit(
        principal.context(vendor_id),
        "vendor_updated",
        {"vendor_id": vendor_id, "email_domain": after["email_domain"]},
        before={"email_domain": before["email_domain"]},
    )
    return vendor_out(after)


def propose_change(
    principal: Principal, vendor_id: str, proposal: BankChangeProposal
) -> dict:
    """Shared by the vendors page and the invoice shortcut."""
    repo = _repo()
    if repo is None:
        raise HTTPException(503, NEEDS_DB)
    if not proposal.reason.strip():
        raise HTTPException(
            400, "A bank change needs a reason the approver can check."
        )
    if not (proposal.bank_account_number or proposal.bank_iban):
        raise HTTPException(
            400, "A bank change needs an account number or IBAN."
        )
    if proposal.source_case_id:
        from . import service  # noqa: PLC0415

        # Client-supplied and written to the audit trail, so it has to be a
        # case this tenant owns.
        ctx = principal.context(proposal.source_case_id)
        if not service.get_cases().get_artifacts(ctx):
            raise HTTPException(400, "Unknown invoice")
    try:
        change = repo.propose(
            principal.tenant_id,
            vendor_id,
            proposed=proposal.model_dump(
                include={
                    "bank_account_name",
                    "bank_account_number",
                    "bank_code",
                    "bank_iban",
                }
            ),
            reason=proposal.reason.strip(),
            proposed_by=principal.email,
            source_case_id=proposal.source_case_id,
        )
    except KeyError:
        raise HTTPException(404, "Unknown vendor") from None
    except repos.PendingChangeExists:
        raise HTTPException(
            409, "A bank change for this vendor is already awaiting approval."
        ) from None
    _audit().emit(
        principal.context(vendor_id),
        "vendor_bank_change_proposed",
        change_out(change),
    )
    return change_out(change, full=principal.can("approver"))


@router.post("/api/vendors/{vendor_id}/bank-changes", status_code=201)
def propose_bank_change(
    vendor_id: str,
    request: BankChangeProposal,
    principal: Annotated[Principal, Depends(require_role("clerk"))],
) -> dict:
    return propose_change(principal, vendor_id, request)
