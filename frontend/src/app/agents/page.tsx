"use client";

import Link from "next/link";
import { useState } from "react";
import { Tile } from "@/components/Tile";
import { AgentMandateDialog } from "@/components/trust/AgentMandateDialog";
import {
  api,
  ApiError,
  can,
  isController,
  type Agent,
  type AgentAction,
  type AgentBlockCount,
  type AgentLedger,
  type AgentList,
  type ApiKeyList,
  type Mandate,
  type MandateList,
  type Me,
  type TenantLedger,
} from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { money } from "@/lib/trust";

const FILTERS = ["all", "proposed", "active", "rejected", "revoked"] as const;

/** proposed = registered, waiting for the Controller; rejected = the
 *  Controller refused it; revoked = an admin ended an active one. */
function StatusBadge({ status }: { status: Agent["status"] }) {
  if (status === "active") return <span className="badge badge-ok">active</span>;
  if (status === "proposed") return <span className="badge badge-warn">awaiting Controller</span>;
  return <span className="badge badge-neutral">{status}</span>;
}

/** An amount in its currency; a logged row may carry none (keyed ""), and
 *  `money()` reads a missing currency as rupees. */
function amountIn(amount: string, currency: string): string {
  return currency ? money(amount, currency) : money(amount, "INR").replace(/^₹/, "");
}

/** "₹1,200.00 · 50.00 USD": a per-currency map, never summed across. */
function joined(byCurrency: Record<string, string> | undefined): string {
  const entries = Object.entries(byCurrency ?? {});
  return entries.length ? entries.map(([currency, amount]) => amountIn(amount, currency)).join(" · ") : "—";
}

function sumByCurrency(maps: Record<string, string>[]): Record<string, string> {
  const out: Record<string, number> = {};
  for (const map of maps) for (const [c, a] of Object.entries(map)) out[c] = (out[c] ?? 0) + Number(a);
  return Object.fromEntries(Object.entries(out).map(([c, a]) => [c, a.toFixed(2)]));
}

/** The reader's local day: validity was entered as local time. */
function day(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "—";
}

function MandateCell({ mandate }: { mandate: Mandate | null }) {
  if (!mandate) return <span className="badge badge-warn">none</span>;
  const text = `v${mandate.version} · until ${day(mandate.valid_to)}`;
  if (mandate.expired) return <span className="badge badge-warn">{text} · expired</span>;
  if (!mandate.verified) return <span className="badge badge-warn">{text} · unverified</span>;
  return <span>{text} · verified</span>;
}

function scopeSummary(m: Mandate): string {
  const caps = m.scope.caps as Record<string, unknown>;
  const parts = [m.scope.actions.join(", "), m.scope.currency];
  parts.push(m.scope.counterparties ? `${m.scope.counterparties.length} vendor(s)` : "any vendor");
  if (caps.per_action) parts.push(`≤ ${caps.per_action} per action`);
  const period = caps.per_period as { period: string; amount: string } | undefined;
  if (period) parts.push(`≤ ${period.amount} per ${period.period}`);
  if (caps.per_vendor) parts.push(`≤ ${caps.per_vendor} per vendor`);
  if (caps.per_category) parts.push(`per kind: ${Object.entries(caps.per_category as object).map(([k, v]) => `${k} ≤ ${v}`).join(", ")}`);
  return parts.join(" · ");
}

function decisionBadge(decision: AgentAction["decision"]): string {
  if (decision === "BLOCK") return "badge badge-warn";
  if (decision === "ALLOW") return "badge badge-ok";
  return "badge";
}

type Pending =
  | { kind: "approve-agent" }
  | { kind: "reject-agent" }
  | { kind: "approve"; mandate: Mandate }
  | { kind: "revoke-mandate"; mandate: Mandate }
  | { kind: "revoke-agent" };

/** Agent control: registered agents, their mandates and spend (Wave 5). */
export default function AgentsPage() {
  const { data: me } = usePoll<Me>("/api/me");
  const approver = Boolean(me && can(me, "approver"));
  const admin = Boolean(me && can(me, "admin"));
  const controller = isController(me ?? null);
  const agents = usePoll<AgentList>(approver ? "/api/trust/agents" : null);
  const count = usePoll<AgentBlockCount>(approver ? "/api/trust/agents/blocks/count" : null);
  const spend = usePoll<TenantLedger>(approver ? "/api/trust/agents/ledger" : null);
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("all");
  const [selected, setSelected] = useState<string | null>(null);
  const [registering, setRegistering] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const rows = agents.data?.agents ?? [];
  const shown = rows.filter((a) => filter === "all" || a.status === filter);
  const agent = rows.find((a) => a.id === selected) ?? null;
  const owners = spend.data?.owners ?? [];
  const hours = count.data?.window_hours ?? 24;

  function done(message: string) {
    setNotice(message);
    agents.reload();
    count.reload();
    spend.reload();
  }

  return (
    <section className="stack" style={{ gap: 16 }}>
      <h1>Agents</h1>
      {!me ? (
        <div className="empty-state">Loading…</div>
      ) : !approver ? (
        <div className="empty-state">Agents are for approvers and above.</div>
      ) : agents.error ? (
        <div className="empty-state" role="alert">
          {agents.error}
        </div>
      ) : !agents.data ? (
        <div className="empty-state">Loading…</div>
      ) : (
        <>
          <div className="tiles">
            <Tile label="Agent blocks" value={String(count.data?.blocked ?? "—")} hint={`last ${hours} h`} testId="blocks" />
            <Tile label="Needs review" value={String(count.data?.reviews ?? "—")} hint={`last ${hours} h`} testId="reviews" />
            <Tile label="Agents" value={String(rows.filter((a) => a.status === "active").length)} hint="active" testId="agents" />
            <Tile
              label="Approved this month"
              value={joined(sumByCurrency(owners.map((o) => o.approved)))}
              hint="by agents, per currency"
              testId="approved"
            />
          </div>

          <div className="row-between">
            <div className="row" style={{ gap: 6 }}>
              {FILTERS.map((f) => (
                <button key={f} type="button" className="btn-chip" aria-pressed={filter === f} onClick={() => setFilter(f)}>
                  {f[0].toUpperCase() + f.slice(1)}
                </button>
              ))}
            </div>
            {admin ? (
              <button type="button" className="btn-primary" onClick={() => setRegistering(true)}>
                Register agent
              </button>
            ) : null}
          </div>

          {rows.length === 0 ? (
            <div className="empty-state">No agents registered.</div>
          ) : (
            <div className="panel table-scroll" style={{ padding: 8 }}>
              <table className="data-table" aria-label="Agents">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Owner</th>
                    <th>Kind</th>
                    <th>Principal</th>
                    <th>Status</th>
                    <th>Mandate</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((a) => (
                    <tr
                      key={a.id}
                      tabIndex={0}
                      aria-selected={a.id === selected}
                      style={{ cursor: "pointer" }}
                      onClick={() => setSelected(a.id)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          setSelected(a.id);
                        }
                      }}
                    >
                      <td>{a.name}</td>
                      <td>{a.owner}</td>
                      <td>{a.kind}</td>
                      <td className="muted">{a.principal_email ?? "—"}</td>
                      <td>
                        <StatusBadge status={a.status} />
                      </td>
                      <td>
                        <MandateCell mandate={a.mandate} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {agent ? <AgentPanel key={agent.id} agent={agent} me={me} admin={admin} controller={controller} onDone={done} /> : null}

          <h2>Spend by owner</h2>
          {spend.error ? (
            <div className="empty-state" role="alert">
              {spend.error}
            </div>
          ) : owners.length === 0 ? (
            <div className="empty-state">No agent spend this month.</div>
          ) : (
            <div className="panel table-scroll" style={{ padding: 8 }}>
              <table className="data-table" aria-label="Spend by owner">
                <thead>
                  <tr>
                    <th>Owner</th>
                    <th>Agents</th>
                    <th>Approved</th>
                    <th>Paid</th>
                    <th>Blocked</th>
                    <th>Reviews</th>
                  </tr>
                </thead>
                <tbody>
                  {owners.map((o) => (
                    <tr key={o.owner}>
                      <td>{o.owner}</td>
                      <td>{o.agents}</td>
                      <td>{joined(o.approved)}</td>
                      <td>{joined(o.exported)}</td>
                      <td>{o.blocked}</td>
                      <td>{o.reviewed}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {admin ? <LogImport onDone={done} /> : null}
        </>
      )}
      {registering ? (
        <RegisterDialog
          onClose={() => setRegistering(false)}
          onDone={(message) => {
            setRegistering(false);
            done(message);
          }}
        />
      ) : null}
      {notice ? (
        <div className="toast" role="status">
          {notice}
        </div>
      ) : null}
    </section>
  );
}

function AgentPanel({
  agent,
  me,
  admin,
  controller,
  onDone,
}: {
  agent: Agent;
  me: Me;
  admin: boolean;
  controller: boolean;
  onDone: (message: string) => void;
}) {
  const id = encodeURIComponent(agent.id);
  const mandates = usePoll<MandateList>(`/api/trust/agents/${id}/mandates`);
  const ledger = usePoll<AgentLedger>(`/api/trust/agents/${id}/ledger`);
  const [proposing, setProposing] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const rows = mandates.data?.mandates ?? [];
  const current = rows.find((m) => m.status === "active") ?? null;
  const exceptions = ledger.data?.exceptions ?? [];
  // As the server folds (`_fold`): trimmed, casefolded.
  const self = (m: Mandate) => m.proposed_by.trim().toLowerCase() === me.email.trim().toLowerCase();

  function finished(message: string) {
    setProposing(false);
    setPending(null);
    mandates.reload();
    ledger.reload();
    onDone(message);
  }

  return (
    <section className="panel stack" style={{ padding: 16, gap: 12 }} aria-label={`Agent ${agent.name}`}>
      <div className="row-between">
        <h2 style={{ margin: 0 }}>{agent.name}</h2>
        {admin && agent.status === "active" ? (
          <div className="row" style={{ gap: 6 }}>
            <button type="button" className="btn-secondary" onClick={() => setProposing(true)}>
              Propose mandate
            </button>
            {current ? (
              <button type="button" className="btn-secondary" onClick={() => setPending({ kind: "revoke-mandate", mandate: current })}>
                Revoke mandate
              </button>
            ) : null}
            <button type="button" className="btn-secondary" onClick={() => setPending({ kind: "revoke-agent" })}>
              Revoke agent
            </button>
          </div>
        ) : null}
      </div>
      {agent.description ? <p className="muted" style={{ margin: 0 }}>{agent.description}</p> : null}
      {agent.status === "proposed" ? (
        controller ? (
          <div className="row" style={{ gap: 6 }}>
            <button type="button" className="btn-primary" onClick={() => setPending({ kind: "approve-agent" })}>
              Approve agent
            </button>
            <button type="button" className="btn-secondary" onClick={() => setPending({ kind: "reject-agent" })}>
              Reject agent
            </button>
          </div>
        ) : (
          <p className="muted" style={{ margin: 0 }}>
            A Controller approves new agents. Until then nothing it does is allowed and it takes no mandate.
          </p>
        )
      ) : null}
      {agent.status === "rejected" && agent.revoke_reason ? (
        <p className="muted" style={{ margin: 0 }}>Rejected: {agent.revoke_reason}</p>
      ) : null}

      <h3 style={{ margin: 0 }}>Mandates</h3>
      {mandates.error ? (
        <div className="empty-state" role="alert">
          {mandates.error}
        </div>
      ) : rows.length === 0 ? (
        <div className="empty-state">No mandate yet: every action this agent takes is outside its mandate.</div>
      ) : (
        <div className="table-scroll">
          <table className="data-table" aria-label="Mandates">
            <thead>
              <tr>
                <th>Version</th>
                <th>Status</th>
                <th>Valid</th>
                <th>Scope</th>
                <th>Proposed by</th>
                <th>Approved by</th>
                <th>Signature</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {[...rows].reverse().map((m) => (
                <tr key={m.id}>
                  <td>v{m.version}</td>
                  <td>
                    <span className={m.status === "active" && !m.expired ? "badge badge-ok" : m.status === "proposed" ? "badge" : "badge badge-neutral"}>
                      {m.expired ? "expired" : m.status}
                    </span>
                  </td>
                  <td>
                    {day(m.valid_from)} → {day(m.valid_to)}
                  </td>
                  <td style={{ fontSize: 12.5 }}>{scopeSummary(m)}</td>
                  <td>{m.proposed_by}</td>
                  <td>{m.approved_by ?? "—"}</td>
                  <td>{m.verified ? "verified" : <span className="badge badge-warn">unverified</span>}</td>
                  <td>
                    {admin && m.status === "proposed" ? (
                      <span className="stack" style={{ gap: 2 }}>
                        <button
                          type="button"
                          className="btn-chip"
                          aria-label={`Approve v${m.version}`}
                          disabled={self(m)}
                          onClick={() => setPending({ kind: "approve", mandate: m })}
                        >
                          Approve
                        </button>
                        {self(m) ? <span className="faint" style={{ fontSize: 12 }}>A different admin approves</span> : null}
                      </span>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3 style={{ margin: 0 }}>This month</h3>
      {ledger.error ? (
        <div className="empty-state" role="alert">
          {ledger.error}
        </div>
      ) : !ledger.data ? (
        <div className="empty-state">Loading…</div>
      ) : (
        <>
          <div className="tiles">
            <Tile label="Approved" value={joined(Object.fromEntries(Object.entries(ledger.data.spend).map(([c, s]) => [c, s.approved])))} />
            <Tile label="Paid" value={joined(Object.fromEntries(Object.entries(ledger.data.spend).map(([c, s]) => [c, s.exported])))} />
            <Tile label="Blocked" value={String(exceptions.filter((a) => a.decision === "BLOCK").length)} />
            <Tile label="Reviews" value={String(exceptions.filter((a) => a.decision !== "BLOCK").length)} />
          </div>
          {ledger.data.period ? (
            <p className="muted" style={{ margin: 0 }}>
              Per {ledger.data.period.period} cap: {money(ledger.data.period.used, ledger.data.period.currency)} of{" "}
              {money(ledger.data.period.cap, ledger.data.period.currency)} used.
            </p>
          ) : null}
          {Object.entries(ledger.data.spend).map(([currency, s]) => (
            <p key={currency} className="muted" style={{ margin: 0, fontSize: 12.5 }}>
              {currency || "No currency"}: by vendor{" "}
              {Object.entries(s.by_vendor).map(([v, a]) => `${v} ${amountIn(a, currency)}`).join(", ") || "—"}; by kind{" "}
              {Object.entries(s.by_category).map(([k, a]) => `${k} ${amountIn(a, currency)}`).join(", ") || "—"}
            </p>
          ))}
          {ledger.data.actions.length === 0 ? (
            <div className="empty-state">No actions this month.</div>
          ) : (
            <div className="table-scroll">
              <table className="data-table" aria-label="Recent actions">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Action</th>
                    <th>Subject</th>
                    <th>Amount</th>
                    <th>Decision</th>
                    <th>Why</th>
                  </tr>
                </thead>
                <tbody>
                  {ledger.data.actions.slice(0, 50).map((a) => (
                    <tr key={a.id}>
                      <td className="faint">{new Date(a.occurred_at).toLocaleString()}</td>
                      <td>{a.action}</td>
                      <td className="muted">{a.subject_ref}</td>
                      <td>{a.amount ? amountIn(a.amount, a.currency ?? "") : "—"}</td>
                      <td>
                        <span className={decisionBadge(a.decision)}>{a.decision}</span>
                      </td>
                      <td style={{ fontSize: 12.5 }}>{a.explanation}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {proposing ? <AgentMandateDialog agent={agent} onClose={() => setProposing(false)} onDone={finished} /> : null}
      {pending ? <ConfirmDialog agent={agent} pending={pending} onClose={() => setPending(null)} onDone={finished} /> : null}
    </section>
  );
}

/** Approve, revoke a mandate or revoke the agent. A revoke needs a reason. */
function ConfirmDialog({
  agent,
  pending,
  onClose,
  onDone,
}: {
  agent: Agent;
  pending: Pending;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [reason, setReason] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const base = `/api/trust/agents/${encodeURIComponent(agent.id)}`;
  const spec =
    pending.kind === "approve-agent"
      ? {
          title: "Approve agent",
          text: `Approving ${agent.name} lets an admin propose its mandate. It still does nothing until a mandate is approved.`,
          path: `${base}/approve`,
          button: "Approve agent",
          done: `${agent.name} approved.`,
        }
      : pending.kind === "reject-agent"
        ? {
            title: "Reject agent",
            text: `Rejecting ${agent.name} releases its key or user so it can be registered again correctly.`,
            path: `${base}/reject`,
            button: "Reject agent",
            done: `${agent.name} rejected.`,
          }
      : pending.kind === "approve"
      ? {
          title: "Approve mandate",
          text: `Approving v${pending.mandate.version} signs it and replaces the version in force. You are the second person on it.`,
          path: `${base}/mandates/${encodeURIComponent(pending.mandate.id)}/approve`,
          button: "Approve",
          done: `Mandate v${pending.mandate.version} approved.`,
        }
      : pending.kind === "revoke-mandate"
        ? {
            title: "Revoke mandate",
            text: `After revoking v${pending.mandate.version}, every action ${agent.name} takes is outside its mandate.`,
            path: `${base}/mandates/${encodeURIComponent(pending.mandate.id)}/revoke`,
            button: "Revoke mandate",
            done: `Mandate v${pending.mandate.version} revoked.`,
          }
        : {
            title: "Revoke agent",
            text: `Revoking ${agent.name} closes its mandates. It cannot be undone; register it again to restore it.`,
            path: `${base}/revoke`,
            button: "Revoke agent",
            done: `${agent.name} revoked.`,
          };
  const needsReason = pending.kind !== "approve" && pending.kind !== "approve-agent";

  async function submit() {
    setBusy(true);
    setProblem(null);
    try {
      await api(spec.path, {
        method: "POST",
        body: needsReason ? JSON.stringify({ reason: reason.trim() }) : undefined,
      });
      onDone(spec.done);
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dialog-scrim" role="presentation" onClick={onClose}>
      <div
        className="panel dialog"
        role="dialog"
        aria-modal="true"
        aria-label={spec.title}
        onClick={(event) => event.stopPropagation()}
      >
        <h2 style={{ margin: "0 0 4px" }}>{spec.title}</h2>
        <p style={{ margin: 0, fontSize: 12.5 }}>{spec.text}</p>
        {needsReason ? (
          <label className="stack" style={{ gap: 4 }}>
            <span className="eyebrow">Reason (required)</span>
            <textarea
              className="input-field"
              rows={3}
              autoFocus
              maxLength={500}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
        ) : null}
        {problem ? (
          <p role="alert" style={{ margin: 0, color: "var(--error)", fontSize: 12.5 }}>
            {problem}
          </p>
        ) : null}
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button type="button" className="btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn-primary" disabled={busy || (needsReason && !reason.trim())} onClick={submit}>
            {spec.button}
          </button>
        </div>
      </div>
    </div>
  );
}

type PrincipalKind = "api_key" | "user" | "external";

function RegisterDialog({ onClose, onDone }: { onClose: () => void; onDone: (message: string) => void }) {
  const keys = usePoll<ApiKeyList>("/api/integrations/api-keys");
  const [name, setName] = useState("");
  const [owner, setOwner] = useState("");
  const [description, setDescription] = useState("");
  const [kind, setKind] = useState<PrincipalKind>("api_key");
  const [keyId, setKeyId] = useState("");
  const [userId, setUserId] = useState("");
  const [email, setEmail] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const live = (keys.data?.keys ?? []).filter((k) => !k.revoked_at);

  // Exactly one principal shape is sent.
  const principal =
    kind === "api_key" ? (keyId ? { api_key_id: keyId } : undefined) : kind === "user" ? (userId.trim() && email.trim() ? { user_id: userId.trim(), email: email.trim() } : undefined) : null;
  const valid = Boolean(name.trim() && owner.trim() && principal !== undefined);

  async function submit() {
    setBusy(true);
    setProblem(null);
    try {
      await api("/api/trust/agents", {
        method: "POST",
        body: JSON.stringify({
          name: name.trim(),
          owner: owner.trim(),
          description: description.trim() || null,
          principal,
        }),
      });
      onDone(`${name.trim()} registered.`);
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dialog-scrim" role="presentation" onClick={onClose}>
      <div
        className="panel dialog"
        role="dialog"
        aria-modal="true"
        aria-label="Register agent"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 style={{ margin: "0 0 4px" }}>Register agent</h2>
        <p style={{ margin: 0, fontSize: 12.5 }}>
          An agent acts only within a mandate one administrator proposes and a different one approves.
        </p>
        <label className="stack" style={{ gap: 4 }}>
          <span className="eyebrow">Name</span>
          <input className="input-field" maxLength={128} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="eyebrow">Owner email</span>
          <input className="input-field" type="email" value={owner} onChange={(e) => setOwner(e.target.value)} />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="eyebrow">Description (optional)</span>
          <input className="input-field" maxLength={500} value={description} onChange={(e) => setDescription(e.target.value)} />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="eyebrow">Acts as</span>
          <select className="input-field" value={kind} onChange={(e) => setKind(e.target.value as PrincipalKind)}>
            <option value="api_key">API key</option>
            <option value="user">User</option>
            <option value="external">External (logs only)</option>
          </select>
        </label>
        {kind === "api_key" ? (
          <label className="stack" style={{ gap: 4 }}>
            <span className="eyebrow">API key</span>
            <select className="input-field" value={keyId} onChange={(e) => setKeyId(e.target.value)}>
              <option value="">{live.length ? "Choose a key" : "No live API keys"}</option>
              {live.map((k) => (
                <option key={k.id} value={k.id}>
                  {k.name} ({k.role})
                </option>
              ))}
            </select>
          </label>
        ) : kind === "user" ? (
          <>
            <label className="stack" style={{ gap: 4 }}>
              <span className="eyebrow">User id</span>
              <input className="input-field" maxLength={64} value={userId} onChange={(e) => setUserId(e.target.value)} />
            </label>
            <label className="stack" style={{ gap: 4 }}>
              <span className="eyebrow">User email</span>
              <input className="input-field" type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
            </label>
          </>
        ) : null}
        {problem ? (
          <p role="alert" style={{ margin: 0, color: "var(--error)", fontSize: 12.5 }}>
            {problem}
          </p>
        ) : null}
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button type="button" className="btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn-primary" disabled={busy || !valid} onClick={submit}>
            Register
          </button>
        </div>
      </div>
    </div>
  );
}

type ImportResult = { rows: number; by_decision: Record<string, number>; errors: { row: number; message: string }[] };

function LogImport({ onDone }: { onDone: (message: string) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [problem, setProblem] = useState<{ text: string; off: boolean } | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!file) return;
    setBusy(true);
    setProblem(null);
    setResult(null);
    const form = new FormData();
    form.append("file", file);
    try {
      const out = await api<ImportResult>("/api/trust/agents/actions/import", { method: "POST", body: form });
      setResult(out);
      onDone(`${out.rows} logged action(s) read.`);
    } catch (caught) {
      const error = caught instanceof ApiError ? caught : null;
      setProblem({ text: error ? error.friendly : String(caught), off: error?.status === 409 });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel stack" style={{ padding: 16, gap: 10 }} aria-label="Import an action log">
      <h2 style={{ margin: 0 }}>Import an action log</h2>
      <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
        Columns: agent, action, subject_ref, occurred_at, and optionally vendor, category, amount, currency. Each row is judged
        against the mandate in force when it happened.
      </p>
      <div className="row" style={{ gap: 8 }}>
        <label className="stack" style={{ gap: 4 }}>
          <span className="eyebrow">Action log (CSV or XLSX)</span>
          <input type="file" accept=".csv,.xlsx" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
        <button type="button" className="btn-secondary" disabled={busy || !file} onClick={submit}>
          Import
        </button>
      </div>
      {problem ? (
        <p role="alert" style={{ margin: 0, color: "var(--error)", fontSize: 12.5 }}>
          {problem.text}
          {problem.off ? (
            <>
              {" "}
              <Link href="/trust-engines">Trust engines</Link>
            </>
          ) : null}
        </p>
      ) : null}
      {result ? (
        <table className="data-table" aria-label="Import result">
          <tbody>
            <tr>
              <th>Rows</th>
              <td>{result.rows}</td>
            </tr>
            {Object.entries(result.by_decision).map(([decision, n]) => (
              <tr key={decision}>
                <th>{decision}</th>
                <td>{n}</td>
              </tr>
            ))}
            {result.errors.map((e) => (
              <tr key={e.row}>
                <th>Row {e.row}</th>
                <td>{e.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </section>
  );
}
