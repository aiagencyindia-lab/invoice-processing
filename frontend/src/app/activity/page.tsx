"use client";

import { useState } from "react";
import type { AuditEvent } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

const POLL_MS = 10000;

function when(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export default function ActivityPage() {
  const [limit, setLimit] = useState(100);
  const { data, error } = usePoll<{ events: AuditEvent[]; count: number }>(
    `/api/audit?limit=${limit}`,
    POLL_MS,
  );
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
      </div>

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
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
