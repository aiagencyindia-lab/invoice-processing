"use client";

import { useState } from "react";
import { api, ApiError, type Agent } from "@/lib/api";

/** The nine governed kinds (trust/agents ACTIONS). */
export const AGENT_ACTIONS = [
  "vendor.create",
  "vendor.patch",
  "bank_change.propose",
  "bank_change.decide",
  "invoice.approve",
  "invoice.reject",
  "invoice.export",
  "po.create",
  "document.upload",
] as const;

/** A datetime-local value `days` from now, in the browser's local time. */
function local(days: number): string {
  const at = new Date(Date.now() + days * 86_400_000);
  at.setSeconds(0, 0);
  return new Date(at.getTime() - at.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

/** Propose the agent's next mandate version. It takes effect only when a
 *  different admin approves it (two-person rule). */
export function AgentMandateDialog({
  agent,
  onClose,
  onDone,
}: {
  agent: Agent;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [actions, setActions] = useState<string[]>([]);
  const [currency, setCurrency] = useState("INR");
  const [counterparties, setCounterparties] = useState("");
  const [perAction, setPerAction] = useState("");
  const [period, setPeriod] = useState("month");
  const [perPeriod, setPerPeriod] = useState("");
  const [perVendor, setPerVendor] = useState("");
  const [categories, setCategories] = useState<{ kind: string; amount: string }[]>([]);
  const [from, setFrom] = useState(local(0));
  const [to, setTo] = useState(local(90));
  const [reason, setReason] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function toggle(kind: string) {
    setActions((now) => (now.includes(kind) ? now.filter((a) => a !== kind) : [...now, kind]));
  }

  async function submit() {
    setBusy(true);
    setProblem(null);
    // Amounts as strings: the API refuses a JSON number (it has been a float).
    const caps: Record<string, unknown> = {};
    if (perAction.trim()) caps.per_action = perAction.trim();
    if (perPeriod.trim()) caps.per_period = { period, amount: perPeriod.trim() };
    if (perVendor.trim()) caps.per_vendor = perVendor.trim();
    const byKind = categories.filter((c) => c.kind.trim() && c.amount.trim());
    if (byKind.length) caps.per_category = Object.fromEntries(byKind.map((c) => [c.kind.trim(), c.amount.trim()]));
    const names = counterparties
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    try {
      const row = await api<{ version: number }>(`/api/trust/agents/${encodeURIComponent(agent.id)}/mandates`, {
        method: "POST",
        body: JSON.stringify({
          scope: {
            actions: AGENT_ACTIONS.filter((a) => actions.includes(a)),
            currency: currency.trim().toUpperCase(),
            counterparties: names.length ? names : null,
            caps,
          },
          // The browser's local time, sent as UTC.
          valid_from: new Date(from).toISOString(),
          valid_to: new Date(to).toISOString(),
          reason: reason.trim() || undefined,
        }),
      });
      onDone(`Mandate v${row.version} proposed; a different admin approves it.`);
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    } finally {
      setBusy(false);
    }
  }

  const valid = actions.length > 0 && /^[A-Za-z]{3}$/.test(currency.trim()) && Boolean(from && to);

  return (
    <div className="dialog-scrim" role="presentation" onClick={onClose}>
      <div
        className="panel dialog"
        role="dialog"
        aria-modal="true"
        aria-label="Propose mandate"
        onClick={(event) => event.stopPropagation()}
        style={{ maxHeight: "90vh", overflowY: "auto" }}
      >
        <h2 style={{ margin: "0 0 4px" }}>Propose a mandate for {agent.name}</h2>
        <p style={{ margin: 0, fontSize: 12.5 }}>
          A mandate takes effect only when a different administrator approves it.
        </p>
        <fieldset className="stack" style={{ gap: 4, border: 0, padding: 0, margin: 0 }}>
          <legend className="eyebrow">Actions</legend>
          {AGENT_ACTIONS.map((kind) => (
            <label key={kind} className="row" style={{ gap: 6, fontSize: 13 }}>
              <input
                type="checkbox"
                autoFocus={kind === AGENT_ACTIONS[0]}
                checked={actions.includes(kind)}
                onChange={() => toggle(kind)}
              />
              {kind}
            </label>
          ))}
        </fieldset>
        <label className="stack" style={{ gap: 4 }}>
          <span className="eyebrow">Currency</span>
          <input
            className="input-field"
            maxLength={3}
            value={currency}
            onChange={(event) => setCurrency(event.target.value)}
          />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="eyebrow">Counterparties (one vendor name or id per line; empty = any vendor)</span>
          <textarea
            className="input-field"
            rows={3}
            value={counterparties}
            onChange={(event) => setCounterparties(event.target.value)}
          />
        </label>
        <fieldset className="stack" style={{ gap: 6, border: 0, padding: 0, margin: 0 }}>
          <legend className="eyebrow">Caps (empty = none)</legend>
          <label className="row" style={{ gap: 6 }}>
            <span style={{ minWidth: 110 }}>Per action</span>
            <input className="input-field" inputMode="decimal" value={perAction} onChange={(e) => setPerAction(e.target.value)} />
          </label>
          <div className="row" style={{ gap: 6 }}>
            <label className="row" style={{ gap: 6 }}>
              <span style={{ minWidth: 110 }}>Per period</span>
              <select className="input-field" value={period} onChange={(e) => setPeriod(e.target.value)} aria-label="Period">
                <option value="day">day</option>
                <option value="month">month</option>
              </select>
            </label>
            <input
              className="input-field"
              inputMode="decimal"
              aria-label="Per period amount"
              value={perPeriod}
              onChange={(e) => setPerPeriod(e.target.value)}
            />
          </div>
          <label className="row" style={{ gap: 6 }}>
            <span style={{ minWidth: 110 }}>Per vendor</span>
            <input className="input-field" inputMode="decimal" value={perVendor} onChange={(e) => setPerVendor(e.target.value)} />
          </label>
          {categories.map((row, index) => (
            <div key={index} className="row" style={{ gap: 6 }}>
              <input
                className="input-field"
                aria-label="Vendor kind"
                placeholder="vendor kind"
                value={row.kind}
                onChange={(e) =>
                  setCategories((all) => all.map((c, i) => (i === index ? { ...c, kind: e.target.value } : c)))
                }
              />
              <input
                className="input-field"
                inputMode="decimal"
                aria-label="Kind amount"
                placeholder="amount"
                value={row.amount}
                onChange={(e) =>
                  setCategories((all) => all.map((c, i) => (i === index ? { ...c, amount: e.target.value } : c)))
                }
              />
              <button
                type="button"
                className="btn-chip"
                aria-label="Remove kind cap"
                onClick={() => setCategories((all) => all.filter((_, i) => i !== index))}
              >
                ×
              </button>
            </div>
          ))}
          <div>
            <button
              type="button"
              className="btn-chip"
              onClick={() => setCategories((all) => [...all, { kind: "", amount: "" }])}
            >
              + Per vendor kind
            </button>
          </div>
        </fieldset>
        <div className="row" style={{ gap: 8 }}>
          <label className="stack" style={{ gap: 4 }}>
            <span className="eyebrow">Valid from</span>
            <input className="input-field" type="datetime-local" value={from} onChange={(e) => setFrom(e.target.value)} />
          </label>
          <label className="stack" style={{ gap: 4 }}>
            <span className="eyebrow">Valid to</span>
            <input className="input-field" type="datetime-local" value={to} onChange={(e) => setTo(e.target.value)} />
          </label>
        </div>
        <label className="stack" style={{ gap: 4 }}>
          <span className="eyebrow">Reason (optional)</span>
          <textarea
            className="input-field"
            rows={2}
            maxLength={500}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
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
            Propose
          </button>
        </div>
      </div>
    </div>
  );
}
