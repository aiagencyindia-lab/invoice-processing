# Ledgerly `/v2` API

The `/v2` API exposes the trust layer to other systems: findings, explanations, lineage, scores, decisions and trust audits. Each `/v2` route returns exactly what its `/api` twin returns for the same caller. The routes below are the contract.

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
