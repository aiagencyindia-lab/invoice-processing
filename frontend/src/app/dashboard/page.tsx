"use client";

import Link from "next/link";
import { can, type AgentBlockCount, type AuditEvent, type Me, type Stats, type SystemStatus } from "@/lib/api";
import { Tile } from "@/components/Tile";
import { usePoll } from "@/lib/hooks";

const POLL_MS = 5000;

function percent(rate: number | null | undefined): string {
  // `null` means no invoices yet, which is not the same as 0% touchless --
  // showing a hard zero would read as a failing system on day one.
  if (rate === null || rate === undefined) return "—";
  return `${Math.round(rate * 100)}%`;
}

function when(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export default function DashboardPage() {
  const { data: stats, error } = usePoll<Stats>("/api/stats", POLL_MS);
  const { data: status } = usePoll<SystemStatus>("/api/system/status", POLL_MS);
  const { data: audit } = usePoll<{ events: AuditEvent[] }>(
    "/api/audit?limit=8",
    POLL_MS,
  );
  const { data: me } = usePoll<Me>("/api/me");
  // Agent counts are for approvers; below that nothing is asked.
  const { data: agents } = usePoll<AgentBlockCount>(
    me && can(me, "approver") ? "/api/trust/agents/blocks/count" : null,
    60_000,
  );

  const events = audit?.events ?? [];

  return (
    <div className="stack" style={{ gap: 24 }}>
      <h1>Dashboard</h1>

      {error ? <div className="empty-state">{error}</div> : null}

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
          gap: 16,
        }}
      >
        <Tile
          label="Needs review"
          value={String(stats?.needs_review ?? "—")}
          hint="Waiting on a human"
          href="/invoices"
        />
        <Tile
          label="Auto-routed"
          value={String(stats?.auto ?? "—")}
          hint="Read confidently; the rules still decide"
        />
        <Tile
          label="Touchless rate"
          value={percent(stats?.touchless_rate)}
          href="/kpis"
          hint={stats?.total ? `${stats.touchless} of ${stats.total} accepted untouched · see KPIs` : "no invoices yet"}
        />
        <Tile
          label="Corrections"
          value={String(stats?.corrections ?? "—")}
          hint="Reviewer edits recorded"
        />
        <Tile
          label="Queue"
          value={String(status?.queue.depth ?? "—")}
          hint="Waiting for a worker"
          href="/documents"
        />
        <Tile
          label="Failed"
          value={String(status?.queue.dead_letters ?? "—")}
          hint="Dead-lettered — retry from Documents"
          href="/documents"
        />
        {agents ? (
          <>
            <Tile
              label="Agent blocks"
              value={String(agents.blocked)}
              hint={`Stopped · last ${agents.window_hours} h`}
              href="/agents"
              testId="agent-blocks"
            />
            <Tile
              label="Agent reviews"
              value={String(agents.reviews)}
              hint={`Need a look · last ${agents.window_hours} h`}
              href="/agents"
              testId="agent-reviews"
            />
          </>
        ) : null}
      </div>

      <section className="stack" style={{ gap: 10 }}>
        <div className="row-between">
          <h2>Recent activity</h2>
          <Link href="/activity" style={{ fontSize: 13 }}>
            See all
          </Link>
        </div>

        {events.length === 0 ? (
          <div className="empty-state">
            Nothing yet. Upload an invoice from{" "}
            <Link href="/documents">Documents</Link> to get started.
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
                {events.map((event, index) => (
                  <tr key={`${event.at}-${index}`}>
                    <td className="faint">{when(event.at)}</td>
                    <td>{event.actor ?? "—"}</td>
                    <td>{event.action ?? "—"}</td>
                    <td className="muted">{event.entity_id ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
