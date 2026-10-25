# Wave 5 — Governed PO creation and document upload; holds from log BLOCKs (`v2/w5-governed`, branch B)

**Program plan:** `docs/superpowers/plans/2026-10-23-v2-wave5-program.md` (B4, B5). **Owns:** `api/po_routes.py` (`store_orders`), `db/repositories.py` (`PgPurchaseOrderRepository.existing_numbers` only), `api/ingest.py` (`upload_document`), `api/trust_provenance_routes.py` (`upload_vendor_document`), `trust/agents/guard.py` (`po_action`, `document_action`), `trust/agents/record.py` (the hold branch only), `tests/test_trust_agents_guard_po_upload.py`, `tests/test_trust_agents_holds.py`. **Touches nothing else**; `ACTIONS`, `SUBJECT_TABLES`, `_judge`'s `transaction_node` and `record(..., hold=)` come from the scaffold.

## Governed PO creation (B4)

- `PgPurchaseOrderRepository.existing_numbers(tenant_id) -> set[str]`: one `select(PurchaseOrder.po_number)` for the tenant.
- `guard.po_action(ctx, records: list[dict], repo) -> list[Action]`: for each cleaned record whose `po_number` is not in `repo.existing_numbers(ctx.tenant_id)` (a stored PO updated by the upload is not a create): `kind="po.create"`, `subject_table="purchase_orders"`, `subject_ref=f"purchase_orders:{po_number}"`, `vendor_key=normalize_vendor(vendor_name) or None`, `vendor_id` via `service.get_vendors().find_by_name` when a vendors store exists, `category=None`, `amount=Decimal(total_amount)` or None, `currency` upper-cased. The amount is recorded, never summed by caps (Wave 4 B9 stands: caps count `invoice.approve` only).
- `po_routes.store_orders(repo, principal, records, *, replace=False)`: after `validate_orders` and `_clean_orders`, `decision = guard.check(principal, lambda: guard.po_action(principal.context(), clean, repo))` (a list decision: every create is checked before any is written; the first refusal is the 403), then `with guard.settling(principal, decision) as settle: out = repo.upsert_orders(...); settle.written()`. Both callers (`POST /api/purchase-orders/import` via `_import_orders`, `PUT /api/purchase-orders` via `config_routes.replace_purchase_orders`) go through it unchanged. With `replace=True` every number not stored is a create, as before.

## Governed document upload (B4)

- `guard.document_action(ctx, data: bytes, filename: str | None, *, vendor: dict | None = None) -> Action`: `kind="document.upload"`, `subject_table="documents"`, `subject_ref=f"documents:{sha256(data).hexdigest()[:32]}"`, `vendor_key`/`vendor_id` from `vendor` when given (KYC), `amount=None`, `currency=None`, `text=None` (the document is not scanned for injection at upload: nothing has acted on it yet), `transaction_node=None`.
- `ingest.upload_document`: after `data = await file.read()` and `check_acceptable(data, file.filename)` (size, `%PDF`, so an oversized or non-PDF upload is a 400 before anything is hashed or ledgered), `decision = await run_in_threadpool(guard.check, principal, lambda: guard.document_action(principal.context(), data, file.filename))`; then `ingest_document(...)` runs inside `guard.settling(principal, decision)` (also in the threadpool), `settle.written()` after it returns. A refused upload (403) stores nothing: `check` runs before `ingest_document`. `IngestionRefused` still answers 400 (and stamps the ledger row `failed`).
- `trust_provenance_routes.upload_vendor_document`: the existing 413, `check_acceptable` and unknown-vendor 404 run first (move the vendor lookup ahead of the guard: `service.get_vendors().get(tenant, vendor_id)`, `None` → 404); then the guard with `vendor=` that row; then `_store` inside `settling`. A duplicate (same bytes already received) still answers 200/409 as today; its ledger row is stamped by `settling` like any finished request (my call: a request that ran is one action, whatever it stored).
- Mode `off`: `guard.check` returns before the builder runs — no hash, no vendor read (the Wave 4 contract; test it with a builder that fails the test).

## Holds from log BLOCKs (B5)

In `record.record`, the hold is decided once:

```
hold = (
    source == "log" and decision.decision == "BLOCK" and mode == "enforcing"
    and action.subject_table == "invoices" and bool(action.transaction_node)
)
```

(the scaffold's `hold` parameter becomes this computed value; inline refusals never hold because the write never happened; dryrun never holds). `details["hold"] = hold`; `findings._upsert(s, …, hold=hold)` places the `trust_holds` row (`holds.place`) and emits `hold.placed` in the same session. The finding's summary gains the sentence "Payment is held until a person decides this finding." when `hold`.

Why nothing else changes: `gate.payment_hold_detail` reads each finding's `details["hold"]` and whether its engine is enforcing — `HOLD_ENGINES` is not consulted — so approval and export refuse (409 `PaymentHold`) as for any held finding. `AGENT-*` rows in `types.TABLE` keep `hold=False`, so `_owns_holds("agents")` stays false and `evaluate._release_cleared` never lifts an agent hold on a later engine run; a person's finding decision (`FindingStore.transition` → `holds.lift`) is the only release. When `agents` leaves enforcing, the gate stops counting agent findings (it counts a finding's hold only while its engine enforces) but the `trust_holds` row stays open, so moving back to enforcing makes it bite again; a person's decision is still the only release. This matches every engine's "holds bite only in enforcing" rule (my call; no gate change). Say so in the API doc line branch C writes, and in PENDING. A log BLOCK on an invoice that was already approved or exported still places the hold (it is the record a person must clear); nothing further is refused on a paid invoice.

## Notes

- Mandates proposed before this lands do not list `po.create` / `document.upload`, so in enforcing those agents are refused on PO import and upload until a new version adds the kinds — the registered-only, mandate-grants rule (Wave 4 U2/U4). No mandate exists in any database, so no migration step.
- Velocity counts every ledger row, so a large PO import can raise `AGENT-VELOCITY` (a review, never a block) on the agent's later actions in the window — accepted; tune `agents.velocity_max` per tenant if an agent's job is bulk loads. A refused PO in a list stamps the earlier rows `failed` (scaffold).

## Tests

- `tests/test_trust_agents_guard_po_upload.py` (fixture copied from `test_trust_agents_guard.py`): PO import by a registered key — enforcing, `po.create` not in the mandate → 403 `AGENT-OUT-OF-MANDATE`, `list_orders` empty; advisory → stored, one `po.create` ledger row per new number with the PO's amount and vendor key, none for a number already stored; `PUT /api/purchase-orders` goes through the same check; off → `AgentStore.by_principal` never called and the builder never runs. Upload: refused → `documents.find_by_sha` is None and no blob; advisory → stored, one `document.upload` row with the hash subject; same bytes twice → same subject, second upload still answers as ingest does today; KYC upload carries `vendor_key`/`vendor_id`. A person (unregistered) uploads and imports as before in every mode.
- `tests/test_trust_agents_holds.py`: an imported log row BLOCK in enforcing on a known invoice → `trust_holds` row open, `gate.payment_hold_detail` names it, approve and export answer 409; the same row in advisory → finding, no hold; dryrun → nothing; a log BLOCK on a non-invoice subject → finding, no hold; deciding the finding (`POST /api/trust/findings/{id}/decide`) lifts the hold and export goes out; running `trust.evaluate` on the invoice afterwards leaves the hold in place; the finding summary ends with the held sentence.
- Existing suites: `tests/test_trust_agents_guard.py`, `tests/test_po_routes*.py`, `tests/test_ingest*.py` unchanged and green; Postgres `tests/test_trust_*.py`.
