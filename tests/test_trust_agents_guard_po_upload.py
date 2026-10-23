"""PO creation and document upload behind the agent guard (Wave 5 B4):
`po.create` for each number not stored, `document.upload` for each upload,
refused in enforcing before anything is written, nothing read with the
engine off."""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest
from test_trust_agents_guard import TENANT, agent, ledger
from test_trust_agents_guard import env as guard_env  # noqa: F401 - fixture

from invoice_processing.adapters import LocalBlobStore
from invoice_processing.api import service
from invoice_processing.db import models
from invoice_processing.db.repositories import (
    PgDocumentRepository,
    PgPurchaseOrderRepository,
)
from invoice_processing.db.session import tenant_session
from invoice_processing.ports import RunContext
from invoice_processing.trust.agents import guard
from invoice_processing.trust.agents.store import AgentStore

CTX = RunContext(tenant_id=TENANT, run_id="", actor="bot@x.test")
PO = {
    "po_number": "PO-7001",
    "vendor_name": "QuickFix HVAC Repairs Pty Ltd",
    "total_amount": 1200.5,
    "currency": "aud",
    "lines": [
        {
            "line_number": 1,
            "description": "Service",
            "qty_ordered": 1,
            "unit_price": 1200.5,
        }
    ],
}
PDF = b"%PDF-1.4 governed upload test\n%%EOF"
KYC = b"%PDF-1.4 governed kyc test\n%%EOF"


@pytest.fixture
def env(guard_env, tmp_path, monkeypatch):  # noqa: F811
    url = guard_env["url"]
    orders, documents = PgPurchaseOrderRepository(url), PgDocumentRepository(url)
    monkeypatch.setattr(service, "_purchase_orders", orders)
    monkeypatch.setattr(service, "_documents", documents)
    blobs = tmp_path / "blobs"
    monkeypatch.setattr(
        service, "get_blobs", lambda tid: LocalBlobStore(blobs / tid)
    )
    with tenant_session(TENANT, url) as s:
        s.add(
            models.Vendor(
                tenant_id=TENANT,
                id="v-1",
                name="QuickFix HVAC Repairs",
                normalized_name="QUICKFIX HVAC REPAIRS",
                tax_id="",
            )
        )
    return {**guard_env, "orders": orders, "docs": documents, "blobs": blobs}


def po(n, **extra):
    return {**PO, "po_number": n, **extra}


def import_pos(env, token, *records):
    return env["client"](token).post(
        "/api/purchase-orders/import", json={"purchase_orders": list(records)}
    )


def upload(env, token, data=PDF, name="inv.pdf"):
    return env["client"](token).post(
        "/api/documents", files={"file": (name, data, "application/pdf")}
    )


def kyc(env, token, data=KYC, vendor="v-1"):
    return env["client"](token).post(
        f"/api/vendors/{vendor}/documents",
        files={"file": ("gst.pdf", data, "application/pdf")},
        data={"subkind": "gst_certificate"},
    )


def numbers(env):
    return {o["po_number"] for o in env["orders"].list_orders(TENANT)}


def subject(data=PDF):
    return f"documents:{hashlib.sha256(data).hexdigest()[:32]}"


# -- builders -----------------------------------------------------------------


def test_existing_numbers_are_the_stored_keys(env):
    env["orders"].upsert_orders(TENANT, [po("po 7002")], uploaded_by="a@x")
    assert env["orders"].existing_numbers(TENANT) == {"PO7002"}
    assert env["orders"].existing_numbers("tenant-other") == set()


def test_po_action_is_one_create_per_new_number(env):
    env["orders"].upsert_orders(TENANT, [po("PO-7002")], uploaded_by="a@x")
    (a,) = guard.po_action(CTX, [po("PO-7001"), po("PO-7002")], env["orders"])
    assert a.kind == "po.create" and a.subject_table == "purchase_orders"
    assert a.subject_ref == "purchase_orders:PO7001"
    assert a.vendor_key == "QUICKFIX HVAC REPAIRS" and a.vendor_id == "v-1"
    assert a.amount == Decimal("1200.5") and a.currency == "AUD"
    assert a.category is None and a.text is None


def test_a_stored_number_in_another_spelling_is_not_a_create(env):
    env["orders"].upsert_orders(TENANT, [po("PO-7001")], uploaded_by="a@x")
    assert guard.po_action(CTX, [po("po 7001")], env["orders"]) == []


def test_a_po_without_amount_or_known_vendor(env):
    (a,) = guard.po_action(
        CTX,
        [{"po_number": "X1", "vendor_name": "Nobody Ltd", "lines": []}],
        env["orders"],
    )
    assert (a.amount, a.currency, a.vendor_id) == (None, None, None)
    assert a.vendor_key == "NOBODY"


def test_document_action_is_the_hash_subject(env):
    a = guard.document_action(CTX, PDF, "inv.pdf")
    assert a.kind == "document.upload" and a.subject_table == "documents"
    assert a.subject_ref == subject()
    assert (a.vendor_key, a.vendor_id, a.amount, a.text) == (None,) * 4
    assert a.transaction_node is None
    v = guard.document_action(
        CTX, PDF, None, vendor={"id": "v-1", "name": "QuickFix HVAC Repairs"}
    )
    assert (v.vendor_key, v.vendor_id) == ("QUICKFIX HVAC REPAIRS", "v-1")


# -- PO import and replace ------------------------------------------------------


def test_enforcing_refuses_a_po_import_outside_the_mandate(env):
    env["settings"].mode = "enforcing"
    agent(env, "clerk:bot", actions=["invoice.approve"])
    r = import_pos(env, "clerk:bot", po("PO-7001"))
    assert r.status_code == 403, r.text
    reasons = [x["type"] for x in r.json()["detail"]["reasons"]]
    assert "AGENT-OUT-OF-MANDATE" in reasons
    assert env["orders"].list_orders(TENANT) == []


def test_enforcing_refuses_a_po_replace_outside_the_mandate(env):
    env["settings"].mode = "enforcing"
    agent(env, "admin:bot", actions=["invoice.approve"])
    r = env["client"]("admin:bot").put(
        "/api/purchase-orders", json={"purchase_orders": [po("PO-7001")]}
    )
    assert r.status_code == 403, r.text
    assert env["orders"].list_orders(TENANT) == []


def test_only_new_numbers_are_creates(env):
    env["orders"].upsert_orders(TENANT, [po("PO-7002")], uploaded_by="a@x")
    env["settings"].mode = "advisory"
    agent(env, "clerk:bot", actions=["invoice.approve"])
    r = import_pos(env, "clerk:bot", po("PO-7001"), po("PO-7002"))
    assert r.status_code == 201, r.text
    assert numbers(env) == {"PO7001", "PO7002"}
    (row,) = ledger(env)
    assert (row.action, row.subject_ref) == (
        "po.create",
        "purchase_orders:PO7001",
    )
    assert row.vendor_key == "QUICKFIX HVAC REPAIRS"
    assert row.amount == Decimal("1200.5") and row.currency == "AUD"
    assert row.decision == "BLOCK" and row.outcome == "executed"


def test_a_po_create_inside_the_mandate_is_allowed(env):
    env["settings"].mode = "enforcing"
    agent(env, "admin:bot", actions=["po.create"])
    r = env["client"]("admin:bot").put(
        "/api/purchase-orders", json={"purchase_orders": [po("PO-7001")]}
    )
    assert r.status_code == 200, r.text
    (row,) = ledger(env)
    assert row.decision == "ALLOW" and row.outcome == "executed"


def test_engine_off_reads_nothing_for_a_po_import(env, monkeypatch):
    agent(env, "clerk:bot", mandate=False)

    def boom(*a, **k):
        raise AssertionError("read with the engine off")

    monkeypatch.setattr(AgentStore, "by_principal", boom)
    monkeypatch.setattr(PgPurchaseOrderRepository, "existing_numbers", boom)
    monkeypatch.setattr(guard, "po_action", boom)
    assert import_pos(env, "clerk:bot", po("PO-7001")).status_code == 201
    assert ledger(env) == []


def test_a_person_imports_as_before_in_enforcing(env):
    env["settings"].mode = "enforcing"
    assert import_pos(env, "clerk:person", po("PO-7001")).status_code == 201
    assert numbers(env) == {"PO7001"} and ledger(env) == []


def test_a_po_the_repository_refuses_is_stamped_failed(env, monkeypatch):
    env["settings"].mode = "advisory"
    agent(env, "clerk:bot", actions=["po.create"])

    def refuse(*a, **k):
        raise ValueError("row 1: PO PO-7001 has goods received")

    monkeypatch.setattr(PgPurchaseOrderRepository, "upsert_orders", refuse)
    r = import_pos(env, "clerk:bot", po("PO-7001"), po("PO-7002"))
    assert r.status_code == 400, r.text
    assert [row.outcome for row in ledger(env)] == ["failed", "failed"]


# -- uploads ------------------------------------------------------------------


def test_enforcing_refuses_an_upload_and_stores_nothing(env):
    env["settings"].mode = "enforcing"
    agent(env, "clerk:bot", actions=["invoice.approve"])
    r = upload(env, "clerk:bot")
    assert r.status_code == 403, r.text
    sha = hashlib.sha256(PDF).hexdigest()
    assert env["docs"].find_by_sha(TENANT, sha) is None
    assert not env["blobs"].exists()
    (row,) = ledger(env)
    assert row.action == "document.upload" and row.subject_ref == subject()


def test_advisory_upload_is_stored_and_ledgered(env):
    env["settings"].mode = "advisory"
    agent(env, "clerk:bot", actions=["document.upload"])
    first = upload(env, "clerk:bot")
    assert first.status_code == 200, first.text
    again = upload(env, "clerk:bot")
    assert again.status_code == 200 and again.json()["duplicate"] is True
    rows = ledger(env)
    assert [r.subject_ref for r in rows] == [subject(), subject()]
    assert {r.decision for r in rows} == {"ALLOW"}
    assert {r.outcome for r in rows} == {"executed"}


def test_a_bad_upload_is_400_before_the_guard(env):
    env["settings"].mode = "enforcing"
    agent(env, "clerk:bot", actions=["invoice.approve"])
    assert upload(env, "clerk:bot", data=b"not a pdf").status_code == 400
    assert upload(env, "clerk:bot", data=b"").status_code == 400
    assert ledger(env) == []


def test_engine_off_never_hashes_an_upload(env, monkeypatch):
    agent(env, "clerk:bot", mandate=False)

    def boom(*a, **k):
        raise AssertionError("built an upload action with the engine off")

    monkeypatch.setattr(guard, "document_action", boom)
    monkeypatch.setattr(AgentStore, "by_principal", boom)
    assert upload(env, "clerk:bot").status_code == 200
    assert kyc(env, "clerk:bot").status_code == 201
    assert ledger(env) == []


def test_a_person_uploads_as_before_in_enforcing(env):
    env["settings"].mode = "enforcing"
    assert upload(env, "clerk:person").status_code == 200
    assert kyc(env, "clerk:person").status_code == 201
    assert ledger(env) == []


def test_a_kyc_upload_carries_the_vendor(env, monkeypatch):
    env["settings"].mode = "advisory"
    agent(env, "clerk:bot", actions=["document.upload"])
    built, real = [], guard.document_action
    monkeypatch.setattr(
        guard,
        "document_action",
        lambda *a, **k: built.append(real(*a, **k)) or built[-1],
    )
    r = kyc(env, "clerk:bot")
    assert r.status_code == 201, r.text
    assert built[0].vendor_id == "v-1"
    (row,) = ledger(env)
    assert row.action == "document.upload" and row.subject_ref == subject(KYC)
    assert row.vendor_key == "QUICKFIX HVAC REPAIRS"
    assert row.outcome == "executed"


def test_enforcing_refuses_a_kyc_upload_and_stores_nothing(env):
    env["settings"].mode = "enforcing"
    agent(env, "clerk:bot", actions=["invoice.approve"])
    assert kyc(env, "clerk:bot").status_code == 403
    sha = hashlib.sha256(KYC).hexdigest()
    assert env["docs"].find_by_sha(TENANT, sha) is None


def test_a_kyc_upload_for_an_unknown_vendor_is_404_before_the_guard(env):
    env["settings"].mode = "enforcing"
    agent(env, "clerk:bot", actions=["invoice.approve"])
    assert kyc(env, "clerk:bot", vendor="nope").status_code == 404
    assert kyc(env, "clerk:bot", data=b"nope").status_code == 400
    assert ledger(env) == []
