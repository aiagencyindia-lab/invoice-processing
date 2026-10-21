"use client";

import { Fragment, useState } from "react";
import { api, ApiError, type WebhookDeliveryList, type WebhookEndpoint, type WebhookEndpointList } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { copy } from "./ApiKeysPanel";

/** The events an endpoint can subscribe to (trust/webhooks EVENTS, less ping,
 *  which only the Test button sends). */
const EVENTS = ["finding.created", "finding.decided", "hold.placed", "hold.lifted"];

function Deliveries({ endpoint }: { endpoint: WebhookEndpoint }) {
  const { data, error } = usePoll<WebhookDeliveryList>(
    `/api/integrations/webhooks/${encodeURIComponent(endpoint.id)}/deliveries`,
  );
  if (error) return <span className="muted">{error}</span>;
  if (!data) return <span className="muted">Loading…</span>;
  if (!data.deliveries.length) return <span className="muted">Nothing sent yet.</span>;
  return (
    <table className="data-table" aria-label={`Deliveries to ${endpoint.url}`}>
      <thead>
        <tr>
          <th>Event</th>
          <th>Status</th>
          <th>Attempts</th>
          <th>Outcome</th>
        </tr>
      </thead>
      <tbody>
        {data.deliveries.map((delivery) => (
          <tr key={delivery.id}>
            <td>{delivery.event}</td>
            <td>{delivery.last_status ?? "—"}</td>
            <td>{delivery.attempts}</td>
            <td>
              {delivery.delivered_at
                ? `delivered ${new Date(delivery.delivered_at).toLocaleString()}`
                : delivery.dead_at
                  ? `gave up: ${delivery.last_error ?? "no response"}`
                  : `retrying ${delivery.next_attempt_at ? new Date(delivery.next_attempt_at).toLocaleTimeString() : "soon"}`}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Outbound webhooks, HMAC-signed per endpoint. Like an API key, the signing
 *  secret is readable once, in the creation response. */
export function WebhooksPanel() {
  const { data, error, reload } = usePoll<WebhookEndpointList>("/api/integrations/webhooks");
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState<string[]>(["hold.placed", "hold.lifted"]);
  const [description, setDescription] = useState("");
  const [created, setCreated] = useState<WebhookEndpoint | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const https = /^https:\/\/[^\s/]+/i.test(url.trim());

  async function create() {
    setBusy(true);
    setProblem(null);
    try {
      const endpoint = await api<WebhookEndpoint>("/api/integrations/webhooks", {
        method: "POST",
        body: JSON.stringify({ url: url.trim(), events, description: description.trim() || null }),
      });
      setCreated(endpoint);
      setUrl("");
      setDescription("");
      reload();
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function test(endpoint: WebhookEndpoint) {
    setProblem(null);
    try {
      await api(`/api/integrations/webhooks/${encodeURIComponent(endpoint.id)}/test`, { method: "POST" });
      setNotice(`Queued a ping to ${endpoint.url}.`);
      setOpen(endpoint.id);
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    }
  }

  return (
    <div className="panel stack" style={{ padding: 16, gap: 10 }} aria-label="Webhooks">
      <h2 style={{ margin: 0 }}>Webhooks</h2>
      {error ? (
        <div className="empty-state">{error}</div>
      ) : (
        <>
          <form
            className="stack"
            style={{ gap: 8 }}
            onSubmit={(event) => {
              event.preventDefault();
              void create();
            }}
          >
            <label className="stack" style={{ gap: 4 }}>
              <span className="eyebrow">Endpoint URL (https)</span>
              <input
                className="input-field"
                type="url"
                placeholder="https://example.com/hooks/ledgerly"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
              />
            </label>
            {url.trim() && !https ? (
              <span style={{ color: "var(--error)", fontSize: 12 }}>Webhooks are sent over https only.</span>
            ) : null}
            <fieldset className="row" style={{ gap: 12, border: 0, padding: 0, margin: 0, flexWrap: "wrap" }}>
              <legend className="eyebrow">Events</legend>
              {EVENTS.map((event) => (
                <label key={event} className="row" style={{ gap: 4, fontSize: 13 }}>
                  <input
                    type="checkbox"
                    checked={events.includes(event)}
                    onChange={(change) =>
                      setEvents(change.target.checked ? [...events, event] : events.filter((e) => e !== event))
                    }
                  />
                  {event}
                </label>
              ))}
            </fieldset>
            <label className="stack" style={{ gap: 4 }}>
              <span className="eyebrow">Description (optional)</span>
              <input
                className="input-field"
                maxLength={256}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
              />
            </label>
            <div>
              <button type="submit" className="btn-primary" disabled={busy || !https || !events.length}>
                Add endpoint
              </button>
            </div>
          </form>

          {created ? (
            <div className="panel stack" style={{ padding: 12, gap: 6 }} role="status" aria-label="Signing secret">
              <strong style={{ fontSize: 13 }}>Copy the signing secret now. It is not shown again.</strong>
              <code style={{ wordBreak: "break-all" }}>{created.secret}</code>
              <div className="row" style={{ gap: 8 }}>
                <button type="button" className="btn-secondary" onClick={() => void copy(created.secret)}>
                  Copy
                </button>
                <button type="button" className="btn-secondary" onClick={() => setCreated(null)}>
                  Done
                </button>
              </div>
            </div>
          ) : null}

          {data?.endpoints.length ? (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Endpoint</th>
                    <th>Events</th>
                    <th>Active</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {data.endpoints.map((endpoint) => (
                    <Fragment key={endpoint.id}>
                      <tr>
                        <td>
                          {endpoint.url}
                          {endpoint.description ? <div className="muted">{endpoint.description}</div> : null}
                        </td>
                        <td>{endpoint.events.join(", ")}</td>
                        <td>{endpoint.active ? "yes" : "no"}</td>
                        <td>
                          <div className="row" style={{ gap: 4 }}>
                            <button type="button" className="btn-chip" onClick={() => test(endpoint)}>
                              Test
                            </button>
                            <button
                              type="button"
                              className="btn-chip"
                              aria-expanded={open === endpoint.id}
                              onClick={() => setOpen(open === endpoint.id ? null : endpoint.id)}
                            >
                              Deliveries
                            </button>
                          </div>
                        </td>
                      </tr>
                      {open === endpoint.id ? (
                        <tr>
                          <td colSpan={4}>
                            <Deliveries endpoint={endpoint} />
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          ) : data ? (
            <span className="muted" style={{ fontSize: 12.5 }}>
              No endpoints yet.
            </span>
          ) : null}
        </>
      )}
      {problem ? (
        <p role="alert" style={{ margin: 0, color: "var(--error)", fontSize: 12.5 }}>
          {problem}
        </p>
      ) : null}
      {notice ? (
        <span className="muted" role="status" style={{ fontSize: 12.5 }}>
          {notice}
        </span>
      ) : null}
    </div>
  );
}
