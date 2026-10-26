"""Mandate scope validation, signing and verification (FR-28.5, B4, B16)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fresh_schema import fresh_schema
from sqlalchemy import update

from invoice_processing.db import models
from invoice_processing.db.repositories import PgVendorRepository
from invoice_processing.db.session import tenant_session
from invoice_processing.po_matching import normalize_vendor
from invoice_processing.ports import RunContext
from invoice_processing.trust.agents import mandates
from invoice_processing.trust.agents.store import AgentStore
from invoice_processing.trust.graph.store import GraphStore
from tests.pg_roles import pg_only

T = "tenant-acme"
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
ADMIN_A = RunContext(tenant_id=T, case_id="", run_id="", actor="a@acme.test")
ADMIN_B = RunContext(tenant_id=T, case_id="", run_id="", actor="b@acme.test")


def body(**over):
    scope = {
        "actions": ["invoice.approve"],
        "counterparties": ["Acme Steel Pvt. Ltd."],
        "caps": {
            "per_action": "50000",
            "per_period": {"period": "month", "amount": "100000.00"},
            "per_vendor": "80000",
            "per_category": {"goods": "60000"},
        },
        "currency": "inr",
    }
    scope.update(over.pop("scope", {}))
    return {
        "scope": scope,
        "valid_from": over.pop("valid_from", NOW),
        "valid_to": over.pop("valid_to", NOW + timedelta(days=30)),
    }


def test_a_valid_scope_is_normalised():
    scope, start, end = mandates.validate(body(), now=NOW)
    assert scope["actions"] == ["invoice.approve"]
    # Without a vendors store, names only.
    assert scope["counterparties"] == [
        {"vendor_id": None, "key": "ACME STEEL PVT"}
    ]
    assert scope["currency"] == "INR"
    assert scope["caps"]["per_period"] == {
        "period": "month",
        "amount": "100000.00",
    }
    assert scope["caps"]["per_category"] == {"goods": "60000"}
    assert (start, end) == (NOW, NOW + timedelta(days=30))


def test_naive_times_are_utc():
    _, start, _ = mandates.validate(
        body(valid_from=NOW.replace(tzinfo=None)), now=NOW
    )
    assert start == NOW and start.tzinfo is not None


def test_no_counterparties_means_any():
    scope, _, _ = mandates.validate(
        body(scope={"counterparties": None}), now=NOW
    )
    assert scope["counterparties"] is None


@pytest.mark.parametrize(
    ("over", "problem"),
    [
        ({"scope": {"actions": []}}, "actions"),
        ({"scope": {"actions": ["invoice.pay"]}}, "actions"),
        ({"scope": {"actions": "invoice.approve"}}, "actions"),
        ({"scope": {"currency": None}}, "currency"),
        ({"scope": {"currency": "rupees"}}, "currency"),
        ({"scope": {"caps": {"per_day": "1"}}}, "caps"),
        ({"scope": {"caps": {"per_action": "-5"}}}, "per_action"),
        ({"scope": {"caps": {"per_action": "1,000"}}}, "per_action"),
        ({"scope": {"caps": {"per_action": "NaN"}}}, "per_action"),
        ({"scope": {"caps": {"per_action": 10.5}}}, "per_action"),
        (
            {
                "scope": {
                    "caps": {"per_period": {"period": "week", "amount": "1"}}
                }
            },
            "period",
        ),
        ({"scope": {"caps": {"per_period": "100"}}}, "per_period"),
        ({"scope": {"caps": {"per_category": {"goods": "x"}}}}, "goods"),
        ({"scope": {"counterparties": ["  "]}}, "counterpart"),
        ({"scope": {"counterparties": "acme"}}, "counterpart"),
        ({"scope": {"counterparties": [{"x": "acme"}]}}, "counterpart"),
        ({"scope": {"counterparties": [{"name": " "}]}}, "counterpart"),
        ({"scope": {"counterparties": [7]}}, "counterpart"),
        ({"valid_to": NOW}, "valid_to"),
        ({"valid_to": NOW + timedelta(days=367)}, "366"),
    ],
)
def test_each_bad_scope_is_refused_with_its_problem(over, problem):
    with pytest.raises(ValueError, match=problem):
        mandates.validate(body(**over), now=NOW)


def test_a_signature_is_per_tenant(monkeypatch):
    monkeypatch.setenv("TRUST_MANDATE_KEY", "k1")
    a = mandates.sign(T, "d" * 64)
    assert a == mandates.sign(T, "d" * 64) and len(a) == 64
    assert a != mandates.sign("tenant-globex", "d" * 64)
    monkeypatch.setenv("TRUST_MANDATE_KEY", "k2")
    assert a != mandates.sign(T, "d" * 64)
    monkeypatch.delenv("TRUST_MANDATE_KEY")
    with pytest.raises(mandates.MandateKeyMissing):
        mandates.sign(T, "d" * 64)


@pytest.fixture
def env(scratch_db, monkeypatch):
    monkeypatch.setenv("TRUST_MANDATE_KEY", "test-key")
    url = scratch_db("mandates.db")
    fresh_schema(url)
    with tenant_session(T, url) as s:
        s.add(models.Tenant(id=T, name="Acme", slug="acme", status="active"))
    store = AgentStore(url, graph=GraphStore(url))
    agent = store.register(
        ADMIN_A, name="bot", owner="o@acme.test", kind="external"
    )
    return store, agent["id"], url


def propose(store, agent_id):
    scope, start, end = mandates.validate(body(), now=NOW)
    return store.propose_mandate(
        ADMIN_A, agent_id, scope=scope, valid_from=start, valid_to=end
    )


def edit(url, mandate_id, **cols):
    with tenant_session(T, url) as s:
        s.execute(
            update(models.AgentMandate)
            .where(models.AgentMandate.id == mandate_id)
            .values(**cols)
        )


def test_an_approved_mandate_verifies_and_chains(env):
    store, agent_id, _ = env
    v1 = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    assert v1["signature"] and mandates.verify(v1, None)
    v2 = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    rows = store.mandates(T, agent_id)
    assert [m["verified"] for m in mandates.annotate(rows, NOW)] == [
        True,
        True,
    ]
    assert mandates.verify(rows[1], rows[0])
    assert not mandates.verify(rows[1], None)  # chain broken
    assert v2["prev_digest"] == v1["digest"]


def test_a_proposed_mandate_verifies_on_its_digest_alone(env):
    store, agent_id, url = env
    m = propose(store, agent_id)
    assert mandates.verify(m, None)
    edit(url, m["id"], valid_to=NOW + timedelta(days=60))
    assert not mandates.verify(store.mandates(T, agent_id)[0], None)


@pytest.mark.parametrize(
    "cols",
    [
        {"scope": {"actions": ["invoice.export"], "currency": "INR"}},
        {"valid_to": NOW + timedelta(days=90)},
        {"approved_by": "mallory@acme.test"},
    ],
)
def test_editing_a_signed_column_in_the_db_breaks_verification(env, cols):
    store, agent_id, url = env
    m = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    edit(url, m["id"], **cols)
    [row] = store.mandates(T, agent_id)
    assert not mandates.verify(row, None)


def test_a_re_digested_edit_still_fails_on_the_signature(env):
    from invoice_processing.trust.agents.store import digest  # noqa: PLC0415

    store, agent_id, url = env
    m = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    forged = {**m, "valid_to": NOW + timedelta(days=90)}
    edit(url, m["id"], valid_to=forged["valid_to"], digest=digest(forged))
    [row] = store.mandates(T, agent_id)
    assert not mandates.verify(row, None)


def test_without_the_key_approval_changes_nothing(env, monkeypatch):
    store, agent_id, _ = env
    m = propose(store, agent_id)
    monkeypatch.delenv("TRUST_MANDATE_KEY")
    with pytest.raises(mandates.MandateKeyMissing):
        mandates.approve(ADMIN_B, store, m["id"])
    [row] = store.mandates(T, agent_id)
    assert row["status"] == "proposed" and row["approved_by"] is None


def test_an_unsigned_active_mandate_does_not_verify(env, monkeypatch):
    store, agent_id, _ = env
    m = propose(store, agent_id)
    store.approve_mandate(ADMIN_B, m["id"], signature=None)
    [row] = store.mandates(T, agent_id)
    assert row["status"] == "active" and not mandates.verify(row, None)
    monkeypatch.delenv("TRUST_MANDATE_KEY")
    assert not mandates.verify(row, None)


def test_expired_is_computed_on_read(env):
    store, agent_id, _ = env
    mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    rows = store.mandates(T, agent_id)
    assert mandates.annotate(rows, NOW)[0]["expired"] is False
    later = NOW + timedelta(days=31)
    assert mandates.annotate(rows, later)[0]["expired"] is True


def test_a_non_ascii_signature_in_the_db_reads_unverified(env):
    store, agent_id, url = env
    m = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    edit(url, m["id"], signature="é" * 64)
    [row] = store.mandates(T, agent_id)
    assert mandates.verify(row, None) is False
    edit(url, m["id"], digest="é" * 64)
    [row] = store.mandates(T, agent_id)
    assert mandates.verify(row, None) is False


def test_in_force_picks_the_version_that_held_at_a_time(env):
    store, agent_id, url = env
    v1 = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    v2 = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    # Both valid NOW..NOW+30d; v1 held until v2 replaced it on day 10.
    edit(url, v1["id"], approved_at=NOW)
    edit(url, v1["id"], revoked_at=NOW + timedelta(days=10))
    edit(url, v2["id"], approved_at=NOW + timedelta(days=10))
    rows = store.mandates(T, agent_id)
    at = mandates.in_force(rows, NOW + timedelta(days=5))
    assert at["id"] == v1["id"] and at["status"] == "active"
    assert mandates.in_force(rows, NOW + timedelta(days=20))["id"] == v2["id"]
    assert mandates.in_force(rows, NOW - timedelta(days=1)) is None
    assert mandates.in_force(rows, NOW + timedelta(days=31)) is None


# -- the signature covers the mandate's state (2026-10-23) ---------------------


def test_a_superseded_version_is_resealed_and_still_verifies(env):
    store, agent_id, _ = env
    mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    v1, v2 = store.mandates(T, agent_id)
    assert v1["status"] == "revoked" and v1["reason"] == "superseded"
    assert mandates.verify(v1, None) and mandates.verify(v2, v1)


@pytest.mark.parametrize(
    "cols",
    [
        {"status": "active"},
        {"status": "active", "revoked_at": None, "revoked_by": None},
        {"revoked_at": NOW},
    ],
)
def test_editing_a_revoked_mandates_state_breaks_verification(env, cols):
    store, agent_id, url = env
    m = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    mandates.revoke(ADMIN_A, store, m["id"], "no longer needed")
    [row] = store.mandates(T, agent_id)
    assert row["status"] == "revoked" and mandates.verify(row, None)
    edit(url, m["id"], **cols)
    [row] = store.mandates(T, agent_id)
    assert not mandates.verify(row, None)


def test_un_revoking_an_active_mandate_by_status_alone_fails(env):
    store, agent_id, url = env
    m = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    edit(url, m["id"], status="revoked")
    [row] = store.mandates(T, agent_id)
    assert not mandates.verify(row, None)


def test_a_proposed_row_flipped_to_active_without_a_signature_fails(env):
    store, agent_id, url = env
    m = propose(store, agent_id)
    edit(url, m["id"], status="active")
    [row] = store.mandates(T, agent_id)
    assert not mandates.verify(row, None)


def test_revoking_the_agent_reseals_each_mandate_it_closed(env):
    from invoice_processing.trust.agents import registry  # noqa: PLC0415

    class Audit:
        def emit(self, *a, **k):
            pass

    store, agent_id, _ = env
    mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    propose(store, agent_id)  # a pending v2, closed too
    registry.revoke(ADMIN_A, agent_id, "retired", store=store, audit=Audit())
    v1, v2 = store.mandates(T, agent_id)
    assert v1["status"] == v2["status"] == "revoked"
    assert mandates.verify(v1, None) and mandates.verify(v2, v1)


def test_a_reseal_never_touches_a_row_the_call_did_not_change(env):
    store, agent_id, url = env
    v1 = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    edit(url, v1["id"], valid_to=NOW + timedelta(days=90))  # tampered
    v2 = propose(store, agent_id)
    # Approving v2 supersedes v1 and reseals v1's *state*; the tampered
    # content still fails its digest, so nothing blessed it.
    mandates.approve(ADMIN_B, store, v2["id"])
    v1_row, _ = store.mandates(T, agent_id)
    assert not mandates.verify(v1_row, None)


def test_revoking_without_the_key_still_revokes(env, monkeypatch):
    store, agent_id, _ = env
    m = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    monkeypatch.delenv("TRUST_MANDATE_KEY")
    out = mandates.revoke(ADMIN_A, store, m["id"], "key rotated away")
    assert out["status"] == "revoked"
    [row] = store.mandates(T, agent_id)
    assert row["status"] == "revoked" and row["signature"] is None


def test_revoking_an_already_revoked_mandate_reseals_nothing(env):
    """A repeat revoke changes nothing, so it must not sign whatever the
    database now holds (fable review): a tampered revoked_at stays caught."""
    store, agent_id, url = env
    m = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    mandates.revoke(ADMIN_A, store, m["id"], "first")
    edit(url, m["id"], revoked_at=NOW + timedelta(days=400))
    mandates.revoke(ADMIN_A, store, m["id"], "again")
    [row] = store.mandates(T, agent_id)
    assert not mandates.verify(row, None)


@pg_only
def test_a_reseal_waits_for_a_revoke_in_flight_and_signs_what_it_left(env):
    """approve() signs in a second session. A revoke of the same version
    committing in between must not be overwritten with the "active" seal:
    the reseal locks the row, so it signs the state the revoke left."""
    import threading  # noqa: PLC0415
    import time  # noqa: PLC0415

    from sqlalchemy import create_engine  # noqa: PLC0415

    store, agent_id, url = env
    m = mandates.approve(ADMIN_B, store, propose(store, agent_id)["id"])
    revoker = create_engine(url).connect()
    tx = revoker.begin()
    revoker.execute(
        update(models.AgentMandate)
        .where(models.AgentMandate.id == m["id"])
        .values(status="revoked", revoked_by="a@acme.test", revoked_at=NOW)
    )  # holds the row lock, not committed yet
    done = threading.Event()
    worker = threading.Thread(
        target=lambda: (mandates.reseal(ADMIN_B, store, [m["id"]]), done.set())
    )
    worker.start()
    time.sleep(0.5)
    assert not done.is_set()  # waiting on the revoke, not racing it
    tx.commit()
    worker.join(10)
    revoker.close()
    [row] = store.mandates(T, agent_id)
    assert row["status"] == "revoked" and mandates.verify(row, None)


# -- counterparties by vendor id (U3) -----------------------------------------


@pytest.fixture
def vendors(env):
    _, _, url = env
    repo = PgVendorRepository(url)
    repo.upsert_many(
        T, [{"name": "Acme Steel Pvt. Ltd."}, {"name": "Globe Freight"}]
    )
    ids = {v["name"]: v["id"] for v in repo.list(T)}
    return repo, ids["Acme Steel Pvt. Ltd."], ids["Globe Freight"]


def parties(items, repo):
    scope, _, _ = mandates.validate(
        body(scope={"counterparties": items}),
        now=NOW,
        vendors=repo,
        tenant_id=T,
    )
    return scope["counterparties"]


def test_names_resolve_to_ids_where_the_vendor_exists(vendors):
    repo, acme, _ = vendors
    assert parties(["acme steel pvt ltd", "Nobody Ltd"], repo) == [
        {"vendor_id": acme, "key": "ACME STEEL PVT"},
        {"vendor_id": None, "key": "NOBODY"},
    ]


def test_an_id_is_accepted_whatever_its_case(vendors):
    repo, acme, globe = vendors
    assert parties([globe, acme.upper()], repo) == [
        {"vendor_id": acme, "key": "ACME STEEL PVT"},
        {"vendor_id": globe, "key": "GLOBE FREIGHT"},
    ]


def test_objects_name_an_id_or_a_name(vendors):
    repo, acme, globe = vendors
    assert parties([{"vendor_id": acme}, {"name": "Globe Freight"}], repo) == [
        {"vendor_id": acme, "key": "ACME STEEL PVT"},
        {"vendor_id": globe, "key": "GLOBE FREIGHT"},
    ]


def test_one_vendor_by_name_and_by_id_is_stored_once(vendors):
    repo, acme, _ = vendors
    assert parties(
        [acme, "Acme Steel Pvt. Ltd.", {"vendor_id": acme}], repo
    ) == [{"vendor_id": acme, "key": "ACME STEEL PVT"}]


@pytest.mark.parametrize(
    "item", ["0b8f6c1e-0000-4000-8000-000000000000", {"vendor_id": "nope"}]
)
def test_an_unknown_vendor_id_is_refused(vendors, item):
    repo, _, _ = vendors
    with pytest.raises(ValueError, match="Unknown vendor id"):
        parties([item], repo)


def test_a_name_object_is_a_name_even_when_it_looks_like_an_id(vendors):
    repo, _, _ = vendors
    looks = "0b8f6c1e-0000-4000-8000-000000000000"
    assert parties([{"name": looks}], repo) == [
        {"vendor_id": None, "key": normalize_vendor(looks)}
    ]


def test_without_a_store_an_id_object_is_refused(vendors):
    _, acme, _ = vendors
    with pytest.raises(ValueError, match="Unknown vendor id"):
        parties([{"vendor_id": acme}], None)


def test_a_mandate_with_vendor_ids_verifies_after_approval(env, vendors):
    store, agent_id, _ = env
    repo, acme, _ = vendors
    scope, start, end = mandates.validate(
        body(scope={"counterparties": [acme, "Nobody"]}),
        now=NOW,
        vendors=repo,
        tenant_id=T,
    )
    m = store.propose_mandate(
        ADMIN_A, agent_id, scope=scope, valid_from=start, valid_to=end
    )
    approved = mandates.approve(ADMIN_B, store, m["id"])
    assert approved["scope"]["counterparties"][0]["vendor_id"] == acme
    assert mandates.verify(approved, None)
