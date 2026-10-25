"""A goods receipt arriving re-matches the invoices step 2.3 held, clearing
the hold only when nothing else holds the invoice."""

import pytest
from fresh_schema import fresh_schema

from invoice_processing import rematch, vendor_master
from invoice_processing.adapters import FsCaseRepository
from invoice_processing.db import models
from invoice_processing.db.repositories import PgCaseRepository
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.shared_libraries.acting import general_invoice_agent
from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    Phase2ValidatorAgent,
)
from tests.test_three_way_pipeline import LINES, VENDOR, FakeBook

TENANT = "tenant-acme"
CTX = RunContext(tenant_id=TENANT, case_id="case-1", actor="worker")


class FakeAudit:
    def __init__(self):
        self.events = []

    def emit(self, ctx, event_type, payload):
        self.events.append((event_type, payload))


def _config(book):
    return {
        **general_invoice_agent.DEFAULT_CONFIG,
        "duplicate_check_enabled": False,
        "require_purchase_order": False,
        "purchase_order_book": book,
        "vendor_master_records": [VENDOR],
    }


@pytest.fixture
def cases(scratch_db):
    url = scratch_db("rematch.db")
    fresh_schema(url)
    with tenant_session(TENANT, url) as session:
        session.add(
            models.Tenant(id=TENANT, name="Acme", slug="acme", status="active")
        )
    return PgCaseRepository(url)


@pytest.fixture
def held(cases, tmp_path, monkeypatch):
    """Run step 2.3 against a short receipt and store the held case."""

    def make(
        received=None, extra_review=(), ctx=CTX, repo=cases, routing="AUTO"
    ):
        config = _config(FakeBook(list(LINES), received or {1: 2}))
        monkeypatch.setitem(
            general_invoice_agent._config_store, "CONFIG", config
        )
        extraction = {
            "invoice": {
                "po_number": "PO-1",
                "vendor_name": "Acme Pty Ltd",
                "invoice_total_inc_tax": 500,
                "currency": "AUD",
                "line_items": [
                    {
                        "description": "Bearing 6205",
                        "quantity": 5,
                        "unit_price": 100,
                        "amount_ex_tax": 500,
                    }
                ],
                "_routing": routing,
                "_evidence": {
                    "_summary": {"fields_needing_review": list(extra_review)}
                },
            }
        }
        phase2 = Phase2ValidatorAgent().run(
            tmp_path, extraction, {"decision": "CONTINUE"}
        )
        repo.put_artifact(ctx, "extraction", extraction)
        repo.put_artifact(ctx, "phase2", phase2)
        repo.project_case(ctx, repo.get_artifacts(ctx))
        return config

    return make


def _run(cases, received, audit, ctx=CTX):
    book = FakeBook(list(LINES), received)
    return rematch.rematch_case(
        ctx,
        cases,
        book,
        vendors=vendor_master.VendorMaster(records=[VENDOR]),
        config=_config(book),
        audit=audit,
    )


def _invoice(cases, ctx=CTX):
    return cases.get_artifacts(ctx)["extraction"]["invoice"]


def _step(cases, ctx=CTX):
    validations = cases.get_artifacts(ctx)["phase2"]["validations"]
    return next(v for v in validations if v["step"] == "2.3")


def test_a_fuller_receipt_clears_the_hold(cases, held):
    held()
    assert cases.held_by_receipt(TENANT, "po 1") == ["case-1"]
    audit = FakeAudit()
    out = _run(cases, {1: 10}, audit)
    assert out == {"before": ["short_received"], "after": [], "cleared": True}
    invoice = _invoice(cases)
    assert invoice["_routing"] == "AUTO"
    assert invoice["_evidence"]["_summary"]["fields_needing_review"] == []
    assert _step(cases)["passed"] is True
    assert audit.events == [
        (
            "three_way_rematched",
            {
                "po_number": "PO-1",
                "before": ["short_received"],
                "after": [],
                "cleared": True,
            },
        )
    ]
    # Projected: no longer listed as held.
    assert cases.held_by_receipt(TENANT, "PO-1") == []


def test_still_short_stays_held_with_the_new_signals(cases, held):
    held()
    out = _run(cases, {1: 3}, FakeAudit())
    assert out["cleared"] is False and out["after"] == ["short_received"]
    invoice = _invoice(cases)
    assert invoice["_routing"] == "REVIEW"
    assert invoice["_evidence"]["_summary"]["fields_needing_review"] == [
        "receipt: short_received"
    ]
    step = _step(cases)
    assert step["passed"] is False and step["routing_before"] == "AUTO"


def test_another_hold_reason_keeps_it_held(cases, held):
    held(extra_review=["bank: bank_unverified"])
    out = _run(cases, {1: 10}, FakeAudit())
    assert out["after"] == [] and out["cleared"] is False
    invoice = _invoice(cases)
    assert invoice["_routing"] == "REVIEW"
    assert invoice["_evidence"]["_summary"]["fields_needing_review"] == [
        "bank: bank_unverified"
    ]


def test_a_decided_case_is_never_rematched(cases, held):
    held()
    cases.put_artifact(CTX, "workflow", {"decision": "approved"})
    before = cases.get_artifacts(CTX)
    audit = FakeAudit()
    _run(cases, {1: 10}, audit)
    assert cases.get_artifacts(CTX) == before
    assert audit.events == []
    assert cases.held_by_receipt(TENANT, "PO-1") == []


def test_a_book_that_fails_is_audited_and_raised(cases, held):
    held()
    audit = FakeAudit()
    book = FakeBook(list(LINES), {}, raises=RuntimeError("db down"))
    with pytest.raises(RuntimeError, match="db down"):
        rematch.rematch_case(
            CTX,
            cases,
            book,
            vendors=vendor_master.VendorMaster(records=[VENDOR]),
            config=_config(book),
            audit=audit,
        )
    assert audit.events == [
        ("three_way_rematch_failed", {"po_number": "PO-1", "error": "db down"})
    ]
    assert _invoice(cases)["_routing"] == "REVIEW"


def test_held_by_receipt_skips_other_pos_and_passing_cases(cases, held):
    held()
    held(received={1: 10}, ctx=RunContext(tenant_id=TENANT, case_id="case-2"))
    assert cases.held_by_receipt(TENANT, "PO-1") == ["case-1"]
    assert cases.held_by_receipt(TENANT, "PO-2") == []
    assert cases.held_by_receipt("tenant-other", "PO-1") == []


def test_filesystem_store_finds_held_cases_too(held, tmp_path):
    fs = FsCaseRepository(root=tmp_path / "cases", tenant_id=TENANT)
    held(repo=fs)
    assert fs.held_by_receipt(TENANT, "PO-1") == ["case-1"]
    assert fs.held_by_receipt("tenant-other", "PO-1") == []
    _run(fs, {1: 10}, FakeAudit())
    assert fs.held_by_receipt(TENANT, "PO-1") == []


def test_a_repeat_never_lifts_an_invoice_routed_to_review(cases, held):
    # Routed REVIEW at extraction with nothing listed (no field scores).
    held(routing="REVIEW")
    for _ in range(2):  # two GRN imports, or a redelivery
        _run(cases, {1: 10}, FakeAudit())
        assert _invoice(cases)["_routing"] == "REVIEW"
    assert _step(cases)["routing_before"] == "REVIEW"


def test_a_cleared_invoice_stays_auto_on_a_repeat(cases, held):
    held()
    first = _run(cases, {1: 10}, FakeAudit())
    second = _run(cases, {1: 10}, FakeAudit())
    assert first["cleared"] is True and second["cleared"] is False
    assert _invoice(cases)["_routing"] == "AUTO"


def test_a_decision_landing_mid_rematch_leaves_the_case_untouched(
    cases, held, monkeypatch
):
    held()
    before = cases.get_artifacts(CTX)
    real = general_invoice_agent.three_way_entry

    def decide_first(*args, **kwargs):
        # The approver decides after the job read the case, before it writes.
        cases.put_artifact(CTX, "workflow", {"decision": "approved"})
        return real(*args, **kwargs)

    monkeypatch.setattr(general_invoice_agent, "three_way_entry", decide_first)
    audit = FakeAudit()
    assert _run(cases, {1: 10}, audit) == {"skipped": "decided"}
    after = cases.get_artifacts(CTX)
    assert after.pop("workflow") == {"decision": "approved"}
    assert after == before
    assert audit.events == []


def test_a_first_hold_by_rematch_remembers_the_routing_it_found(cases, held):
    # Matched clean at extraction (no routing_before), routed REVIEW by
    # something else; a receipt correction later makes 2.3 hold.
    held(received={1: 10}, routing="REVIEW")
    assert "routing_before" not in _step(cases)
    _run(cases, {1: 2}, FakeAudit())
    assert _step(cases)["routing_before"] == "REVIEW"
