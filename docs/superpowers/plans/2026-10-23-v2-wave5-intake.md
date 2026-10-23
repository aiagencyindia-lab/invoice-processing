# Wave 5 branch C — Intake API, counterparties by vendor id, scope doc — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill `POST /v2/agents/actions` (the CSV log import over JSON), let a mandate's `counterparties` name vendors by id or name (stored as `{vendor_id, key}`), match `in_scope` by id first, and document the scope JSON in `docs/API_V2.md`.

**Architecture:** One shared resolver `trust_agent_routes.resolve_agents` feeds both the CSV import and the intake; each row goes through the scaffold's `_judge(..., "log", ...)`. `mandates._counterparties(items, vendors, tenant_id)` resolves names/ids against `PgVendorRepository` when one exists; `rules.in_scope` reads the stored shape.

**Tech Stack:** Python 3.11, FastAPI, Pydantic 2, SQLAlchemy 2, pytest.

**Spec:** `docs/superpowers/specs/2026-10-23-v2-wave5-intake-design.md` (program plan `docs/superpowers/plans/2026-10-23-v2-wave5-program.md` U1, U3, B9).

## Global Constraints

- Own only: `api/v2_routes.py` (`intake_actions`), `api/trust_agent_routes.py` (`propose_mandate`, `resolve_agents`, the import using it), `trust/agents/mandates.py`, `trust/agents/rules.py` (`in_scope`), `docs/API_V2.md` (Agents), `tests/test_trust_api_routes.py`, `tests/test_trust_agents_mandates.py`, `tests/test_trust_agents_rules.py`. `_judge` unchanged.
- Row errors, copy verbatim: "Two agents share this name; use its principal email.", "Unknown agent.", "Name the agent: this caller is not registered as one.", `CHECK_FAILED`; too many rows → 400 "At most 5000 rows per request."; empty key → "Each counterparty must be a vendor name or id."; UUID-shaped unknown → "Unknown vendor id: …".
- `IntakeRow` caps: `agent ≤ 320, action ≤ 32, subject_ref ≤ 128, vendor ≤ 256, category ≤ 64, amount ≤ 32, currency ≤ 8, occurred_at ≤ 40`, all optional `str`.
- Stored counterparties: `[{"vendor_id": str|None, "key": str}]`, sorted by `(key, vendor_id or "")`, de-duplicated; `None` = any.

## Rulings (builder's calls, go in the PR body)

- `PgVendorRepository._vendor_dict` has no `normalized_name` (repositories.py is not ours): the key is `normalize_vendor(vendor["name"])`, which is how `upsert_many` derives `normalized_name`.
- The intake row-error index is 1-based over `actions` (the CSV import keeps line numbers, header = 1).
- `resolve_agents` folds principal emails (the import compared a casefolded cell to the stored email).
- An empty `actions` list → 422 (`min_length=1`); more than 5000 → 400 with the import's wording (checked in the handler so the message is ours).

## Review Focus

1. A JSON number in a row (`"amount": 1200.5`) — a strict str field would 422 the whole request; expected: 422 is acceptable only if it names the field; we keep `str` and test that a numeric `amount` is refused as 422 (documented: amounts are strings, as in mandates).
2. An agent name differing only in case/whitespace ("  Payables-Bot ") — expected to resolve (casefold + strip). Test in Task 3.
3. A UUID-shaped id with uppercase hex — expected to match the vendor (lowercase before lookup). Test in Task 1.
4. The same vendor given by name and by id in one list — expected one stored entry. Test in Task 1.
5. A vendor renamed after the mandate — expected id still matches though the key differs. Test in Task 2.

---

### Task 1: `mandates._counterparties` resolves names and ids

**Files:** Modify `invoice_processing/trust/agents/mandates.py`, `invoice_processing/api/trust_agent_routes.py` (`propose_mandate`); Test `tests/test_trust_agents_mandates.py`, `tests/test_trust_api_routes.py`.

**Interfaces:** Produces `mandates.validate(body, *, now, vendors=None, tenant_id=None) -> (scope, start, end)`; scope `counterparties` in the stored shape.

- [ ] **Step 1: failing tests** (mandates file, vendors = `PgVendorRepository(url)` on the existing scratch env, seeded via `upsert_many`):
  - `test_a_valid_scope_is_normalised`: without a store → `[{"vendor_id": None, "key": "ACME STEEL PVT"}]`.
  - `test_names_resolve_to_ids_where_the_vendor_exists`: `["Acme Steel Pvt. Ltd.", "Nobody"]` → `[{"vendor_id": acme_id, "key": "ACME STEEL PVT"}, {"vendor_id": None, "key": "NOBODY"}]`.
  - `test_an_id_is_accepted` (also upper-cased id), `test_objects_are_accepted` (`{"vendor_id": id}`, `{"name": "…"}`), `test_name_and_id_of_one_vendor_store_once`.
  - `test_an_unknown_uuid_is_refused` → ValueError "Unknown vendor id".
  - `test_a_proposed_mandate_with_ids_verifies_after_approval`.
  - bad-scope table: `["  "]` → "counterpart", `[{"x": 1}]` → "counterpart".
- [ ] **Step 2:** run, see them fail.
- [ ] **Step 3:** implement `_counterparties(items, vendors, tenant_id)`; `validate` passes `vendors, tenant_id`. `propose_mandate` passes `vendors=_service().get_vendors(), tenant_id=principal.tenant_id`.
- [ ] **Step 4:** route test `test_propose_with_a_vendor_name_stores_its_id` in `tests/test_trust_api_routes.py` (seed `PgVendorRepository(url)` via monkeypatched `service._vendors`).
- [ ] **Step 5:** run `pytest tests/test_trust_agents_mandates.py tests/test_trust_api_routes.py -q`, commit.

### Task 2: `rules.in_scope` matches id first, key as fallback

**Files:** Modify `invoice_processing/trust/agents/rules.py`; Test `tests/test_trust_agents_rules.py`.

**Interfaces:** Consumes the stored shape; `Action.vendor_id` from the scaffold.

- [ ] **Step 1: failing tests:** `test_a_renamed_vendor_still_matches_by_id` (party `{"vendor_id": "v1", "key": "OLD NAME"}`, action `vendor_id="v1", vendor_key="NEW NAME"` → None); `test_a_create_matches_by_key` (`vendor_id=None`); `test_another_vendor_is_out_of_mandate` (id and key both differ → `AGENT-OUT-OF-MANDATE`); `test_a_legacy_list_of_names_reads_as_keys`; existing `counterparties: None` tests stay.
- [ ] **Step 2:** fail. **Step 3:** implement: `ids = {p["vendor_id"] for dict p if set}`, `keys = {p["key"]} ∪ {normalize_vendor(str p)}`; block unless `action.vendor_id in ids or action.vendor_key in keys`; an action with neither id nor key skips (as today).
- [ ] **Step 4:** pass, commit.

### Task 3: the intake route and the shared resolver

**Files:** Modify `invoice_processing/api/trust_agent_routes.py` (`resolve_agents`, `import_actions`), `invoice_processing/api/v2_routes.py` (`IntakeRow`, `Intake`, `intake_actions`); Test `tests/test_trust_api_routes.py`.

**Interfaces:** Produces `trust_agent_routes.resolve_agents(store, tenant_id) -> tuple[dict[str, list[dict]], dict[str, dict]]` and `trust_agent_routes.pick_agent(by_name, by_email, who: str) -> dict` (raises the row ValueErrors).

- [ ] **Step 1: failing tests:** PROBED += `/v2/agents/actions` with a probe body `{"actions": [{"agent": "payables-bot", "action": "invoice.export", "subject_ref": "p", "occurred_at": NOW.isoformat()}]}` (globex sees "Unknown agent." for Acme's name only because names are per tenant — no Acme ids leak); `range(25)`; `test_intake_judges_rows_for_the_keys_own_agent`; `test_a_key_without_an_agent_must_name_one`; `test_intake_names_an_agent_by_name_or_email` (incl. `"  PAYABLES-BOT "`); `test_two_agents_sharing_a_name_is_a_row_error`; `test_intake_while_off_is_409_and_writes_nothing`; `test_a_crashing_rule_is_a_row_error`; `test_too_many_rows_is_400`, empty list 422; `test_mixed_rows_fill_decisions_and_errors`; clerk key → 403; doc test.
- [ ] **Step 2:** fail. **Step 3:** implement; `import_actions` uses the same helpers (its tests in `tests/test_trust_agent_routes.py` must stay green).
- [ ] **Step 4:** pass, commit.

### Task 4: `docs/API_V2.md` Agents section

- [ ] Replace the stub row with `| POST | `/v2/agents/actions` | approver (API key or user) | — (the JSON twin of `POST /api/trust/agents/actions/import`) |`; document the intake body, resolution, answer, 409, attestation note; the full scope JSON (nine kinds with what each governs and which routes raise it, currency, counterparties input and stored shape, caps, validity, two-person rule, `verified`); one line on log-BLOCK holds.
- [ ] Run `pytest tests/test_trust_api_routes.py -k doc`; commit.

### Task 5: verification

- [ ] Full suite, ruff, eval `--seed 7` 56/56, Postgres trust + isolation on a fresh database.
