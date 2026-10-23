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

## Agents

Agent control is **off** unless an admin turns the `agents` engine on (`shadow`, `advisory` or `enforcing`). While it is off, nothing below changes what any write does.

An **agent** is a machine actor an admin registered: `{name, owner, description?, principal}`. `owner` is the email of the person answerable for it. `principal` binds it to who it acts as: `{"api_key_id": "…"}` (one of the tenant's live keys), `{"user_id", "email"}` (a service account), or `null` (an outside agent known only from imported logs). One principal belongs to at most one agent (409).

A **mandate** is what a person authorised the agent to do:

```json
{
  "scope": {
    "actions": ["invoice.approve", "invoice.export"],
    "counterparties": ["Acme Steel"],
    "caps": {
      "per_action": "50000",
      "per_period": {"period": "month", "amount": "100000"},
      "per_vendor": "60000",
      "per_category": {"raw_material": "80000"}
    },
    "currency": "INR"
  },
  "valid_from": "2026-10-01T00:00:00Z",
  "valid_to": "2026-12-31T00:00:00Z"
}
```

`actions` is a subset of `vendor.create`, `vendor.patch`, `bank_change.propose`, `bank_change.decide`, `invoice.approve`, `invoice.reject`, `invoice.export`. `counterparties` is a list of vendor names, or `null` for any vendor. Amounts are strings in the mandate's one currency. A mandate runs at most 366 days. One admin proposes it; a **different** admin approves it on `/api` (`POST /api/trust/agents/{agent_id}/mandates/{mandate_id}/approve`), and only then is it signed and in force. Until then every action of the agent is blocked.

`actions/evaluate` asks about one action, `{action, subject_ref, vendor?, category?, amount?, currency?, occurred_at?, source}`:

- `source: "dryrun"` asks "would this be allowed?". The answer is recorded but never counts toward a cap, never raises a finding and never refuses anything.
- `source: "log"` records an action the agent already took elsewhere (`occurred_at` required; approver only). It counts and raises a finding like any other.

While the tenant's `agents` engine is `off`, `actions/evaluate` (and the log import on `/api`) answers 409 and records nothing: turn it to `shadow` or higher first.

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
