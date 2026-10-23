# Wave 5 branch B (governed) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (native, chosen by the parent session). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put PO creation and document upload behind the agent guard, and make a log BLOCK on an invoice in enforcing place a payment hold.

**Architecture:** Two new `Action` builders in `trust/agents/guard.py` are called lazily by the three routes through `guard.check` / `guard.settling` (the Wave 4 contract: engine off reads nothing). `record.record` computes the hold itself; `FindingStore._upsert(hold=)` (scaffold) places the `trust_holds` row; `gate.payment_hold_detail` already honours `details["hold"]`.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2, pytest (SQLite; Postgres via `TEST_DATABASE_URL`).

**Spec:** `docs/superpowers/specs/2026-10-23-v2-wave5-governed-design.md` (program plan B4, B5).

## Global Constraints

- Own only: `api/po_routes.py` (`store_orders`), `db/repositories.py` (`PgPurchaseOrderRepository.existing_numbers`), `api/ingest.py` (`upload_document`), `api/trust_provenance_routes.py` (`upload_vendor_document`), `trust/agents/guard.py` (`po_action`, `document_action`), `trust/agents/record.py` (hold decision), `tests/test_trust_agents_guard_po_upload.py`, `tests/test_trust_agents_holds.py`.
- Engine `off`: `guard.check` returns before the builder runs — no hash, no PO-number read, no vendor read.
- `po.create` amount is recorded, never summed by caps.
- Never set `hold=True` on an `AGENT-*` row of `trust/types.py` TABLE.

## Rulings (builder's calls, go in the PR body)

1. `subject_ref` for a PO is `purchase_orders:{normalize_po_number(po_number)}` — the stored key, so "new" is decided on the same key the repository upserts on.
2. `record(..., hold=False)` stays as an explicit override (the scaffold's `test_trust_agents_store._record_held` passes it); the effective hold is `hold or <B5 rule>`.
3. The person's decision that lifts the hold is a **reject** (`POST /v2/findings/{id}/decision {"decision":"reject"}`): `holds.LIFTS == gate.HELD_UNTIL == "rejected"`; an accepted finding keeps holding, as for every engine. (The spec's `/api/trust/findings/{id}/decide` path does not exist.)
4. Correction to the spec: `modes.set_engine_mode` → `holds.reconcile` lifts the `agents` hold rows when the engine leaves enforcing and re-places them on return (from `details["hold"]`). The effect the spec wants holds: payment is refused only while agents enforces, and a person's reject is the only release while it does. No code change (modes.py is not mine).
5. KYC upload keeps the existing `_vendor_exists` 404 ahead of the guard (same check, no new read in mode off); the builder reads the vendor row (`service.get_vendors().get`) only when the engine is on.

## Review Focus

1. A PO import whose upload *updates* an existing number alongside a new one → one `po.create` row (the new one) — test `test_only_new_numbers_are_creates`.
2. A PO number differing only in case/spaces from a stored one is not a create — covered by ruling 1, test `test_a_stored_number_in_another_spelling_is_not_a_create`.
3. A non-PDF / empty upload by an agent in enforcing → 400, no ledger row — test `test_a_bad_upload_is_400_before_the_guard`.
4. `IngestionRefused` after the guard allowed it → ledger row stamped `failed` (covered by the 400 path above only if `check_acceptable` runs first; `ingest_document`'s own refusal is the same check, so no separate test).
5. A log BLOCK on an unknown invoice (no node) → finding, no hold — test `test_a_log_block_on_an_unknown_invoice_holds_nothing`.

---

### Task 1: Builders and `existing_numbers`

**Files:** Modify `invoice_processing/trust/agents/guard.py`, `invoice_processing/db/repositories.py`. Test: `tests/test_trust_agents_guard_po_upload.py`.

**Interfaces — produces:**
- `PgPurchaseOrderRepository.existing_numbers(tenant_id: str) -> set[str]` (stored, normalized numbers; one select).
- `guard.po_action(ctx, records: list[dict], repo) -> list[Action]`
- `guard.document_action(ctx, data: bytes, filename: str | None, *, vendor: dict | None = None) -> Action`

- [ ] Failing tests: `po_action` skips stored numbers, carries `vendor_key`, `vendor_id` (stored vendor), `Decimal` amount, upper currency, `subject_table="purchase_orders"`; `document_action` subject `documents:<sha256[:32]>`, `text is None`, vendor fields from `vendor`.
- [ ] Run → fail (AttributeError). Implement. Run → pass. Commit.

### Task 2: Governed PO routes

**Files:** Modify `invoice_processing/api/po_routes.py` (`store_orders`). Test: same file.

- [ ] Failing tests: enforcing, `po.create` not in mandate → 403 `AGENT-OUT-OF-MANDATE`, `list_orders` empty (import and `PUT /api/purchase-orders`); advisory → stored + one row per new number; off → `AgentStore.by_principal` and `existing_numbers` never called; a person imports in enforcing untouched; rows stamped `executed`.
- [ ] `store_orders`: validate → `clean = _clean_orders(records)` → `decision = guard.check(principal, lambda: guard.po_action(principal.context(), clean, repo))` → `with guard.settling(principal, decision) as settle: out = repo.upsert_orders(...); settle.written()`; `ValueError` → `_refused` as before. Commit.

### Task 3: Governed uploads

**Files:** Modify `invoice_processing/api/ingest.py` (`upload_document`), `invoice_processing/api/trust_provenance_routes.py` (`upload_vendor_document`, `_store`). Test: same file.

- [ ] Failing tests: refused upload → 403, `documents.find_by_sha` None, no blob; advisory → stored, one `document.upload` row with the hash subject; same bytes twice → same subject, second answers `duplicate: true`; bad upload → 400, no row; KYC upload in advisory carries `vendor_key`/`vendor_id`, refused KYC stores nothing; off → builder never runs.
- [ ] `upload_document`: `check_acceptable` (→400) then a sync helper in `run_in_threadpool` that runs `guard.check` + `settling(ingest_document)`. KYC: 413, `check_acceptable` (400), `_vendor_exists` (404) moved ahead in a threadpool helper, then guard with `vendor=` (lazy read), then `_store` inside `settling`. Commit.

### Task 4: Holds from log BLOCKs

**Files:** Modify `invoice_processing/trust/agents/record.py`. Test: `tests/test_trust_agents_holds.py`.

- [ ] Failing tests: log BLOCK enforcing on a known invoice → open `trust_holds` row, `payment_hold_detail` names it, approve 409 and export 409, summary ends "Payment is held until a person decides this finding."; advisory → finding, no hold; dryrun → no finding; non-invoice subject → no hold; unknown invoice → no hold; reject the finding → hold lifted, export 200; `trust.evaluate` afterwards leaves an open hold (`_release_cleared` not run for agents).
- [ ] In `record`: `hold = hold or (source == "log" and decision.decision == "BLOCK" and mode == "enforcing" and action.subject_table == "invoices" and bool(action.transaction_node))`; summary sentence appended when held. Commit.

### Task 5: Verification, review, PR

- [ ] Full SQLite suite, ruff (CI form), eval `--seed 7` 56/56, Postgres trust suites on a fresh DB.
- [ ] Independent review (subagent, model `fable`), one test-first fix pass.
- [ ] Merge `main`, push as `aiagencyindia-lab`, open PR with rulings.
