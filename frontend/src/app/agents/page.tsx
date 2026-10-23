"use client";

import { can, type Me } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

/** Agent control: registered agents, their mandates and spend (Wave 5). */
export default function AgentsPage() {
  const { data: me } = usePoll<Me>("/api/me");
  return (
    <section className="stack" style={{ gap: 16 }}>
      <h1>Agents</h1>
      {!me ? (
        <div className="empty-state">Loading…</div>
      ) : !can(me, "approver") ? (
        <div className="empty-state">Agents are for approvers and above.</div>
      ) : (
        <div className="empty-state">No agents registered.</div>
      )}
    </section>
  );
}
