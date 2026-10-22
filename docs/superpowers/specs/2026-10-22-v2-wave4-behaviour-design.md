# Wave 4 — Behavioural detectors design (`v2/w4-behaviour`)

**Program plan:** `docs/superpowers/plans/2026-10-22-v2-wave4-program.md`, branch C. FR-28.4, FR-24 (explanations). Decision U5; calls B2, B10, B12.

**Owns:** `trust/agents/{behaviour,injection,inputs}.py`, `explain/templates/agents.py` (fills), a page-text helper in `provenance/pdfmeta.py` only if `facts()` does not already return the text, `tests/test_trust_agents_behaviour.py`, `tests/fixtures/trust_agents/**`, its plan doc.

All four detectors return `REVIEW` reasons (B10). They read only the ledger (`AgentStore`), the `Action`, and thresholds.

## Detectors (`behaviour.RULES = (circular, split, velocity, injection)`)

- `circular` — `invoice.approve` where `creators_of(vendor_key)` contains this agent or an agent with the same owner → `AGENT-CIRCULAR` ("approves an invoice to a vendor that {agent} created").
- `split` — `invoice.approve` with an amount: prior non-BLOCK approvals by this agent for this `vendor_key` within `agents.split_window_days`, plus this one: count ≥ `agents.split_min_count`, every one under the threshold (the mandate's `per_action` cap, else the tenant's `high_value_threshold` setting, else skip), sum over it → `AGENT-SPLIT`.
- `velocity` — `count_since(now − velocity_window_minutes)` ≥ `velocity_max` → `AGENT-VELOCITY`.
- `injection` — `action.text` set: `injection.scan(text)` hits ≥ `injection_min_hits` → classify with `decide()` (one boolean question "Does this text try to instruct an automated agent?", `fallback=True`): true or fallback → `AGENT-INJECTION` with up to three masked excerpts (≤ 120 chars each, `masking.mask_text`); false → no reason. No text or no hits → nothing. The model is never called without a deterministic hit.

## Injection patterns (`injection.py`)

`PATTERNS` (case-insensitive, whitespace-normalised): ignore/disregard (all|any)? (previous|prior|above) instructions; `(system|assistant|developer)( prompt)?:` at line start; "you are (now )?(an?|the) (ai|assistant|agent|bot)"; "approve (this|the) (invoice|payment) (immediately|without|now)"; "do not (flag|hold|review|escalate)"; "(pay|transfer) to (the )?(new|updated) account"; ≥ 3 zero-width characters; a base64 run ≥ 64 chars. `scan(text) -> list[Hit(pattern, start, end)]`. Corpus tests set the bar: 10 injected samples all hit, 20 clean invoices/emails (from `tests/fixtures`) zero hits.

## Inputs (`inputs.py`)

`text_of(document, blobs) -> Callable[[], str]`: lazily returns the PDF text layer (`pdfplumber`, already a dependency, first 20 pages, ≤ 200 kB) plus, for `source == "email"`, the email body via `payment.signals.parse_email`. Exceptions → empty string (no signal, logged without ids). No OCR (B12, PENDING).

## Explanations (`explain/templates/agents.py`)

A template for each of the nine `AGENT-*` codes: `what_happened` from the first reason, `why` lists every reason, `caused_by` = actor kind `agent` named `{agent name} (owner {owner})`, evidence = the agent node and subject node citations, `next_action` per type (out-of-mandate → "Approve a mandate that covers this action, or revoke the agent"; over-limit → "Raise the cap or have a person approve"; unverified vendor → "Verify the vendor's bank account (FR-21), then retry"; injection → "Read the document; remove the agent's access to this vendor if it acted on the text"; …), `would_change` per type. `Explanation.check()` passes for every fixture; the scaffold's explain skip for `AGENT-*` is removed here.

## Tests

Each detector positive/negative; split boundary (sum equal to threshold is not over); velocity window edges; injection corpus; `decide` fallback → review, classification false → no reason, no hit → `decide` never called (fake decider asserts); explanations for all nine codes cite only cited nodes.

**Done:** ruff; suite both dialects; explain coverage covers `AGENT-*`.
