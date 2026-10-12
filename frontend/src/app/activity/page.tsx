"use client";

import { useState } from "react";
import { ApiError, downloadFile, type AuditEvent, type Me } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

const POLL_MS = 10000;

type Change = { field: string; was: string; now: string };

/** What actually changed, from the two states the trail stores.
 *
 *  `fields_corrected` nests its values one level down (`{values: {po: ...}}`)
 *  because that is the shape the edit path writes; everything else compares
 *  key by key. Both are rendered the same way, so a reader learns one format.
 */
function changes(event: AuditEvent): Change[] {
  const unwrap = (state: Record<string, unknown> | null) => {
    const values = state?.values;
    return (values && typeof values === "object" ? values : state) as
      | Record<string, unknown>
      | null
      | undefined;
  };
  const was = unwrap(event.before);
  const now = unwrap(event.payload);
  if (!was) return [];

  return Object.entries(was)
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([field, value]) => ({
      field,
      was: String(value),
      now: now && field in now ? String(now[field]) : "—",
    }));
}

function when(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export default function ActivityPage() {
  const [limit, setLimit] = useState(100);
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [problem, setProblem] = useState("");
  const { data: me } = usePoll<Me>("/api/me");

  const params = new URLSearchParams({ limit: String(limit) });
  if (since) params.set("since", new Date(since).toISOString());
  if (until) params.set("until", new Date(until).toISOString());
  const { data, error } = usePoll<{ events: AuditEvent[]; count: number }>(
    `/api/audit?${params}`,
    POLL_MS,
  );

  const exportCsv = async () => {
    // The server requires both bounds; default to "everything so far".
    const from = since ? new Date(since) : new Date(0);
    const to = until ? new Date(until) : new Date();
    const q = new URLSearchParams({ since: from.toISOString(), until: to.toISOString() });
    const name = `audit-${from.toISOString().slice(0, 10)}-${to.toISOString().slice(0, 10)}.csv`;
    setProblem("");
    try {
      await downloadFile(`/api/audit/export?${q}`, name);
    } catch (caught) {
      setProblem(
        caught instanceof ApiError
          ? caught.friendly
          : "Could not download the export. Check your connection and try again.",
      );
    }
  };
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");

  const events = data?.events ?? [];
  // Filtering client-side: the API caps at 500 events, which is one screenful
  // of scrolling, and a server-side filter would be a query parameter that
  // every future caller has to be trusted not to point at another tenant.
  const shown = events.filter(
    (event) =>
      (!action || event.action === action) &&
      (!actor || event.actor === actor),
  );

  const actions = [...new Set(events.map((e) => e.action).filter(Boolean))];
  const actors = [...new Set(events.map((e) => e.actor).filter(Boolean))];

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="row-between">
        <h1>Activity</h1>
        <span className="muted" style={{ fontSize: 12.5 }}>
          {shown.length} of {events.length} events
        </span>
      </div>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <select
          className="input-field"
          style={{ width: "auto" }}
          value={action}
          onChange={(event) => setAction(event.target.value)}
          aria-label="Filter by action"
        >
          <option value="">All actions</option>
          {actions.map((value) => (
            <option key={value} value={value as string}>
              {value}
            </option>
          ))}
        </select>
        <select
          className="input-field"
          style={{ width: "auto" }}
          value={actor}
          onChange={(event) => setActor(event.target.value)}
          aria-label="Filter by actor"
        >
          <option value="">Everyone</option>
          {actors.map((value) => (
            <option key={value} value={value as string}>
              {value}
            </option>
          ))}
        </select>
        <input type="datetime-local" className="input-field" style={{ width: "auto" }}
          aria-label="Since" value={since} onChange={(e) => { setSince(e.target.value); setProblem(""); }} />
        <input type="datetime-local" className="input-field" style={{ width: "auto" }}
          aria-label="Until" value={until} onChange={(e) => { setUntil(e.target.value); setProblem(""); }} />
        <select
          className="input-field"
          style={{ width: "auto" }}
          value={limit}
          onChange={(event) => setLimit(Number(event.target.value))}
          aria-label="How many events to load"
        >
          <option value={50}>Last 50</option>
          <option value={100}>Last 100</option>
          <option value={500}>Last 500</option>
        </select>
        {/* Only Since/Until bound the export; the action, actor and limit
            filters narrow this screen, not the file. */}
        {me?.role === "admin" ? (
          <button type="button" className="btn-secondary" onClick={exportCsv}>
            Export CSV (whole range)
          </button>
        ) : null}
      </div>

      {problem ? (
        <p role="alert" style={{ color: "var(--error)", fontSize: 12.5 }}>{problem}</p>
      ) : null}

      {error ? <div className="empty-state">{error}</div> : null}

      {shown.length === 0 && !error ? (
        <div className="empty-state">
          No activity recorded yet. Uploads, corrections, approvals and
          settings changes all land here.
        </div>
      ) : (
        <div className="panel table-scroll" style={{ padding: 8 }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Who</th>
                <th>Did what</th>
                <th>To</th>
                <th>Changed</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((event, index) => (
                <tr key={`${event.at}-${index}`}>
                  <td className="faint">{when(event.at)}</td>
                  <td>{event.actor ?? "—"}</td>
                  <td>{event.action ?? "—"}</td>
                  <td className="muted">
                    {event.entity_id ? (
                      <>
                        {event.entity_type ?? "record"} {event.entity_id}
                      </>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="muted">
                    {changes(event).length ? (
                      changes(event).map((change) => (
                        <div key={change.field}>
                          {change.field}: <s>{change.was}</s> → {change.now}
                        </div>
                      ))
                    ) : (
                      <>—</>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
