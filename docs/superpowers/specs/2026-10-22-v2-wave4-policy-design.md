# Wave 4 — Structural rules and interceptor design (`v2/w4-policy`)

**Program plan:** `docs/superpowers/plans/2026-10-22-v2-wave4-program.md`, branch B. FR-28.2, FR-28.3. Decisions U1, U2, U3, U8; calls B1, B3, B7, B8, B9, B11, B12.

**Owns:** `trust/agents/rules.py`, `trust/agents/guard.py` (fills), the call-site edits in `api/vendor_routes.py` and in `api/service.py` (`act`, `export_invoice` only), `tests/test_trust_agents_rules.py`, `tests/test_trust_agents_guard.py`, its plan doc.

## Guard

`check(principal, action, *, settings_store=None, now=None, store=None) -> Decision | None`:
1. `mode = engine_mode(tenant, "agents", settings_store)`; `off` → `None` (no agent query).
2. `agent = store.by_principal(tenant, principal.user_id)`. None → for `invoice.approve`/`invoice.reject` only, the owner check (U9): if any agent owned by `principal.email` is in `prior_deciders`, record the decision against that agent and, in enforcing, refuse with 403 `AGENT-SAME-OWNER-CHAIN` ("you and your agent cannot both approve this invoice"); otherwise `None`. A revoked agent is still found (and blocked by `mandate_active`).
3. In one session: `store.lock_agent` (B17); `mandate = store.current_mandate(tenant, agent.id, now)`; `decision = evaluate_action(ctx, action, agent=…, mandate=…, store=…, settings=thresholds, now=…, decider=service decider)`; `record(..., mode=mode, source="inline")`.
4. `mode == "enforcing"` and `BLOCK` → `HTTPException(403, {"detail": explanation, "action_id": id, "reasons": [{type, message}]})`. Otherwise return the decision; the caller proceeds.
5. Any exception in 2–3: enforcing → `HTTPException(503, "Agent control is unavailable; the action was not taken.")`; else `logger.warning("agent_guard_failed", extra={"error": type(exc).__name__})` and `None`.

## Call sites

Each builds an `Action` from data the handler has, plus the one lookup it needs (`get_change` for `decide_bank_change`, the vendor for `propose_change`; B19), and calls `guard.check` immediately before its existing write:

| Handler | Action fields |
|---|---|
| `replace_vendors` | one `vendor.create` per record whose normalised name is not already a vendor (a replace that keeps a vendor is not a create); all checked before `upsert_many`; any 403 stops the whole replace |
| `patch_vendor` | `vendor.patch`, `vendor_key`, `category=kind` |
| `propose_change` | `bank_change.propose`, `vendor_key` |
| `decide_bank_change` | `bank_change.decide`, `initiated_by = change.proposed_by` |
| `act` | `invoice.approve` / `invoice.reject`; `amount = total` (approve only), `currency`, `vendor_key` from the invoice vendor name, `category` from the vendor's `kind`, `initiated_by` = the document's uploader (`source_ref`) or email sender, `prior_deciders` = decided steps of the chain, `vendor_verified` / `vendor_verified_by` from the vendor's bank verification, `transaction_node` via `graph.nodes_by_ref` when a graph store exists, `text` = `inputs.text_of` (branch C; until C merges, `None`) |
| `export_invoice` | `invoice.export`, `amount`, `vendor_key` |

## Rules (`rules.RULES`, severity order; each returns `Reason | None`)

0. `currency` — an approve whose currency differs from the mandate's → BLOCK `AGENT-OUT-OF-MANDATE` (U10).
1. `mandate_active` — agent revoked, or no current mandate → BLOCK `AGENT-OUT-OF-MANDATE` ("has no active mandate" / "is revoked").
2. `in_scope` — action not in `scope.actions`, or `scope.counterparties` set and `vendor_key` not in it (actions without a vendor skip the counterparty test) → BLOCK `AGENT-OUT-OF-MANDATE`.
3. `self_approval` — `bank_change.decide` / `invoice.approve` / `invoice.reject` with `initiated_by` equal (casefold) to the agent's `principal_email` → BLOCK `AGENT-SELF-APPROVAL`.
4. `same_owner_chain` — the agent's `owner` (a person), or another agent with the same `owner`, is in `prior_deciders` or is `initiated_by` → BLOCK `AGENT-SAME-OWNER-CHAIN` (U9).
5. `over_limit` — approve only: `amount > per_action`; or `spent(since=period start) + amount > per_period.amount`; or per-vendor spend in the period + amount over `per_vendor`; or per-category likewise → BLOCK `AGENT-OVER-LIMIT` naming the cap.
6. `unverified_vendor` — approve/export where `creators_of(vendor_key)` is non-empty and the vendor's bank is not verified by a person (`vendor_verified` false, or `vendor_verified_by` is an agent's principal email); `bank_change.decide` where `initiated_by` is any agent's principal email (B11) → BLOCK `AGENT-UNVERIFIED-VENDOR`.

`RULE_VERSION = "agents-rules-1"` stamped into finding details.

## Tests

Each rule positive/negative with an in-memory ledger fixture; caps at the boundary (equal passes, one paisa over blocks); BLOCK rows do not count toward spend. Route level, per call site: enforcing BLOCK → 403 body has the explanation and the governed table is unchanged (no vendor row, no `approval_steps` change, no exporter call, no bank-change row); advisory → the write happens and a ledger row + finding exist; shadow likewise; engine off → `AgentStore.by_principal` monkeypatched to raise is never called; unregistered key in enforcing → untouched; human principal → untouched; two agents with one owner on one chain → 403 on the second; owner then own agent → 403 for the agent; own agent then owner → 403 for the owner (and no change for a human with no agents); two concurrent approvals that each fit the cap but not together → exactly one passes (Postgres); guard DB failure → 503 in enforcing, proceeds in advisory.

**Done:** ruff; suite both dialects; the six governed routes' existing tests unchanged and green.
