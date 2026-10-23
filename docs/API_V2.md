# Ledgerly `/v2` API

The `/v2` API exposes the trust layer to other systems: findings, explanations, lineage, scores, decisions, trust audits and agent control. Each `/v2` route returns exactly what its `/api` twin returns for the same caller. The routes below are the contract.

## Authentication

An admin creates a key under **Integrations** (or with `POST /api/integrations/api-keys {name, role}`). The response shows the key once:

```
ik_<8 hex prefix>_<secret>
```

Ledgerly stores only a SHA-256 hash of the secret. Send the key either way:

```
X-API-Key: ik_…
Authorization: Bearer ik_…
```

- A key acts in its own tenant with its own role: `viewer`, `clerk` or `approver`. There are no admin keys.
- A key cannot use `/api/integrations/*` (403). A key can never create another key or point a tenant's events somewhere new.
- A request that also carries a signed-in user's bearer token is that user's request, and the key header is ignored.
- An unknown key, a malformed key, a revoked key and the key of a suspended tenant all get **401**. If the database cannot be reached, the request gets **503**.
- Revoke a key with `DELETE /api/integrations/api-keys/{id}`. Creating and revoking keys are both audited.

There is no rate limit and no idempotency key yet. Transitions take `expect` (on `/api`) to guard against concurrent changes.

## Routes

A transaction id is the invoice's evidence-graph node id. It is the `transaction_node` field on every finding.

| Method | `/v2` path | Role | Same answer as |
|---|---|---|---|
| GET | `/v2/transactions/{node_id}/trust` | viewer | `GET /api/trust/transactions/{node_id}/trust` |
| GET | `/v2/transactions/{node_id}/explanation` | viewer | `{transaction, findings: [{finding, explanation}]}`: each open finding with `GET /api/trust/findings/{id}/explanation` |
| GET | `/v2/transactions/{node_id}/lineage` | viewer | `GET /api/trust/lineage/{node_id}` (`?as_of=`) |
| GET | `/v2/findings` | viewer | `GET /api/trust/findings` (same filters: `status`, `open`, `type`, `engine`, `vendor`, `hold`, `since`, `until`, `limit`, `offset`) |
| POST | `/v2/findings/{finding_id}/decision` | clerk; approver for `accept`/`reject` | `POST /api/trust/findings/{id}/transition`. The body is `{decision: accept\|reject\|needs_vendor_input, reason}`. A `detected` finding is triaged first. `reject` needs a `reason`. |
| GET | `/v2/vendors/{node_id}/trust` | viewer | `GET /api/trust/vendors/{node_id}/trust` |
| POST | `/v2/documents/{document_id}/authenticity` | viewer | `POST /api/trust/documents/{document_id}/authenticity` |
| GET | `/v2/evidence/{node_id}` | viewer | `GET /api/trust/nodes/{node_id}` |
| POST | `/v2/trust-audits` | approver | `POST /api/trust/audits` |
| POST | `/v2/trust-audits/{audit_id}/runs` | approver | `POST /api/trust/audits/{audit_id}/runs` |
| GET | `/v2/trust-audits/{audit_id}/report` | viewer | `GET /api/trust/audits/{audit_id}/report` (`?format=json\|html`) |
| POST | `/v2/agents` | admin (a signed-in person; a key gets 403) | `POST /api/trust/agents` |
| POST | `/v2/agents/{agent_id}/mandates` | admin (a signed-in person; a key gets 403) | `POST /api/trust/agents/{agent_id}/mandates` |
| POST | `/v2/agents/{agent_id}/actions/evaluate` | approver, or the agent's own key about itself | `POST /api/trust/agents/{agent_id}/actions/evaluate` |
| GET | `/v2/agents/{agent_id}/ledger` | approver | `GET /api/trust/agents/{agent_id}/ledger` (`?since=&until=`) |
| POST | `/v2/agents/actions` | approver (API key or user) | — (the JSON twin of the log import, `POST /api/trust/agents/actions/import`) |

## Agents

Agent control is **off** unless an admin turns the `agents` engine on (`shadow`, `advisory` or `enforcing`). While it is off, nothing below changes what any write does.

An **agent** is a machine actor an admin registered: `{name, owner, description?, principal}`. `owner` is the email of the person answerable for it. `principal` binds it to who it acts as: `{"api_key_id": "…"}` (one of the tenant's live keys), `{"user_id", "email"}` (a service account), or `null` (an outside agent known only from imported logs). One principal belongs to at most one agent (409).

A **mandate** is what a person authorised the agent to do. An admin proposes it with `POST /v2/agents/{agent_id}/mandates`:

```json
{
  "scope": {
    "actions": ["invoice.approve", "invoice.export"],
    "counterparties": ["Acme Steel", "6f1c2d8e-4b1a-4c55-9a2e-1f0b7c3d9e21", {"vendor_id": "…"}, {"name": "Globe Freight"}],
    "caps": {
      "per_action": "50000",
      "per_period": {"period": "month", "amount": "100000"},
      "per_vendor": "60000",
      "per_category": {"raw_material": "80000"}
    },
    "currency": "INR"
  },
  "valid_from": "2026-10-01T00:00:00Z",
  "valid_to": "2026-12-31T00:00:00Z",
  "reason": "Q4 payables run"
}
```

`actions` is a non-empty subset of these kinds:

| Kind | Governs | Raised by |
|---|---|---|
| `vendor.create` | Adding a supplier the tenant did not have. | `PUT /api/vendors` (each new name in the upload) |
| `vendor.patch` | Changing a supplier's details (its email domain). | `PATCH /api/vendors/{vendor_id}` |
| `bank_change.propose` | Proposing new payee bank details for a supplier. | `POST /api/vendors/{vendor_id}/bank-changes`, `POST /api/invoices/{case_id}/bank-change` |
| `bank_change.decide` | Approving or rejecting a proposed bank change. | `POST /api/vendors/bank-changes/{change_id}/decide` |
| `invoice.approve` | Approving an invoice for payment; the only kind the caps count. | `POST /api/invoices/{case_id}/actions` (`approve`) |
| `invoice.reject` | Rejecting an invoice. | `POST /api/invoices/{case_id}/actions` (`reject`) |
| `invoice.export` | Exporting an approved invoice to the ERP. | `POST /api/invoices/{case_id}/export` |
| `po.create` | Adding a purchase order the tenant did not have (the amount is recorded, never counted by caps). | `POST /api/purchase-orders/import`, `PUT /api/purchase-orders` (each new PO number) |
| `document.upload` | Storing a document (an invoice, or a supplier's KYC file). | `POST /api/documents`, `POST /api/vendors/{vendor_id}/documents` |

`currency` is the mandate's one ISO currency; an approval in another currency, or with none, is out of mandate. Amounts are strings (a JSON number has already been through a float).

`counterparties` is `null` (any vendor) or a non-empty list. Each item is a vendor name, a vendor id, `{"vendor_id": "…"}` or `{"name": "…"}`. At proposal a name resolves to its vendor when the tenant has one; an id must name a stored vendor (otherwise 400 `Unknown vendor id`, so a typo never becomes a name that matches nothing). Without the vendors table every item must be a name. The mandate stores and returns them as `[{"vendor_id": "…" | null, "key": "ACME STEEL"}]`, sorted and de-duplicated, where `key` is the normalised name. An action that names a vendor is in scope when that vendor's id is listed, or, failing that, its normalised name is: a renamed vendor still matches by id, and a `vendor.create` (no id yet) matches by name. An action that names no vendor at all (a plain document upload, or a logged row without `vendor`) is not tested against `counterparties`; its other limits still apply.

`caps` are optional and count only `invoice.approve` amounts: `per_action` (one approval), `per_period` (`day` or `month`, calendar, UTC), `per_vendor` and `per_category` (by vendor kind) over that period. An approval that would pass a cap is blocked.

A mandate runs from `valid_from` to `valid_to`, at most 366 days; it is expired, not deleted, after `valid_to`. One admin proposes it; a **different** admin approves it on `/api` (`POST /api/trust/agents/{agent_id}/mandates/{mandate_id}/approve`), and only then is it signed and in force (a new version supersedes the old on approval). Until then every action of the agent is blocked. Each mandate read carries `verified`: the row is what was signed, in the state it was signed in, and follows the version before it; a mandate edited in the database reads `verified: false` and authorises nothing.

`actions/evaluate` asks about one action, `{action, subject_ref, vendor?, category?, amount?, currency?, occurred_at?, source}`:

- `source: "dryrun"` asks "would this be allowed?". The answer is recorded but never counts toward a cap, never raises a finding and never refuses anything. With a past `occurred_at` it is judged as of then: the mandate in force then, and the agent revoked only from its revocation.
- `source: "log"` records an action the agent already took elsewhere (`occurred_at` required; approver only). It counts and raises a finding like any other.

While the tenant's `agents` engine is `off`, `actions/evaluate`, the intake below and the log import on `/api` answer 409 and record nothing: turn it to `shadow` or higher first.

`POST /v2/agents/actions` takes a batch of logged actions from an agent's own system, the log import's columns as JSON:

```json
{"actions": [
  {"agent": "payables-bot", "action": "invoice.approve", "subject_ref": "invoices:case_0412",
   "vendor": "Acme Steel", "category": "raw_material", "amount": "1200.50", "currency": "INR",
   "occurred_at": "2026-10-21T10:00:00Z"}
]}
```

- 1 to 5000 rows (more: 400). Every field is an optional string; a bad value (an unknown `action`, a missing or unreadable `occurred_at`, an amount that is not a number) is that row's error, and the other rows still land. A field over its length limit, or a JSON number where a string belongs, is a 422 for the request.
- `agent` names the agent by name (case-blind; a revoked agent of the same name is passed over for the active one) or by its principal email. Two active agents with that name → the row error "Two agents share this name; use its principal email."; no such agent → "Unknown agent.". Leave `agent` out and the row is the calling API key's own agent; a caller bound to no agent gets "Name the agent: this caller is not registered as one.".
- Each row is judged as `source: "log"` against the mandate in force at its `occurred_at`, counts and raises a finding like any other. The answer is `{"rows", "by_decision": {"ALLOW": n, …}, "errors": [{"row", "message"}]}`, rows numbered from 1.
- A logged row is an approver's attestation: an approver key may log rows for any agent it names, exactly as an approver's CSV upload can. Rate limits and idempotency keys are not offered yet; resending a batch records it twice.
- In `enforcing`, a logged `BLOCK` on an invoice (`subject_ref` `invoices:<case_id>` of a known invoice) places a payment hold that only a person's decision on its finding lifts; changing the engine's mode does not reconcile agent holds (the hold stops biting below `enforcing` and bites again when it returns).

The answer is `{action_id, decision, reasons: [{rule, type, severity, message}], explanation, mode, source}`, where `decision` is:

| Decision | Means |
|---|---|
| `ALLOW` | Inside the mandate, nothing unusual. |
| `ALLOW-WITH-REVIEW` | Allowed, with a finding for a person to look at: a circular vendor, a split approval, unusual velocity, or instruction-like text in the document. |
| `BLOCK` | Outside what was authorised: no mandate, out of scope, over a cap, deciding something it (or its owner) started, or an unverified agent-created vendor. On Ledgerly's own write routes, with the engine `enforcing`, the write is refused with 403 and nothing is written. |

The ledger answers `{agent, mandate, since, until, spend, period, actions, exceptions}`: what the agent approved and exported, against its caps, and every non-`ALLOW` decision in the window (default: this month).


## Webhooks

An admin registers an endpoint with `POST /api/integrations/webhooks {url, events, description}`:

- `url` must be a public `https://` address. Loopback, private and link-local hosts are refused.
- `events` is `["*"]` or a non-empty subset of the events below.

The response shows the signing secret (`whsec_…`) once; later reads mask it. The other endpoint routes are:

- `PATCH /api/integrations/webhooks/{id}`: change `active`, `events` or `description`.
- `DELETE /api/integrations/webhooks/{id}`: remove the endpoint.
- `POST /api/integrations/webhooks/{id}/test`: queue a `ping` to this endpoint (409 while it is paused).
- `GET /api/integrations/webhooks/{id}/deliveries`: the last 50 deliveries.

Lists answer as `{"keys": […]}`, `{"endpoints": […]}` and `{"deliveries": […]}`.

| Event | When | `data` |
|---|---|---|
| `finding.created` | An engine records a new finding. | The finding, without citation excerpts. |
| `finding.decided` | A finding is accepted, rejected, sent for vendor input, claimed, recovered, prevented or written off. | The finding, without citation excerpts. |
| `hold.placed` | An enforcing engine holds a payment. | `{finding_id, type, engine, transaction_node, summary, impact, currency}` |
| `hold.lifted` | A held finding is rejected, or its engine leaves `enforcing`. | The `hold.placed` fields, plus `lifted_by` and `reason`. |
| `agent.action.blocked` | An enforcing guard refused an agent's write. | `{agent_id, agent_name, owner, action, subject_ref, decision, reasons, explanation, action_id, finding_id}` |
| `ping` | You pressed **Test**. | `{}` |

Every delivery is a `POST` of a JSON body `{"event", "tenant", "occurred_at", "data"}` with these headers:

```
X-Trust-Event: hold.placed
X-Trust-Delivery: <unique id; dedupe on it, delivery is at least once>
X-Trust-Timestamp: <unix seconds>
X-Trust-Signature: sha256=<hex HMAC-SHA256(secret, "{timestamp}.{body}")>
```

**Retries.** Ledgerly checks for due deliveries every minute. A 2xx response counts as delivered. Redirects are not followed, so a 3xx response is a failed attempt. A failed attempt is retried after 2, 4, 8, 16, 32, 64 and then 64 minutes. The eighth failure dead-letters the delivery, and the dead-letter is audited (`webhook_dead_lettered`). A paused endpoint's deliveries wait until it is active again.

### Verifying a signature

```python
import hashlib, hmac, time

def verify(secret: str, headers: dict, body: bytes) -> bool:
    ts = headers["X-Trust-Timestamp"]
    mac = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256)
    fresh = abs(time.time() - int(ts)) < 300
    return fresh and hmac.compare_digest(headers["X-Trust-Signature"], "sha256=" + mac.hexdigest())
```

Pass the raw request body, not a re-serialised one: the signature covers the exact bytes Ledgerly sent.
