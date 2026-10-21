"use client";

import { useState } from "react";
import { api, ApiError, type EngineReadiness } from "@/lib/api";
import { ENGINE_LABELS } from "@/lib/trust";
import { percent } from "./DimensionMeter";

/** The progression an operator walks an engine through; never automatic. */
const MODES = ["off", "shadow", "advisory", "enforcing"] as const;

function modeBadge(mode: string): string {
  if (mode === "enforcing") return "badge badge-warn";
  if (mode === "off") return "badge badge-neutral";
  return "badge";
}

/** Per-engine evidence for moving a mode, and the Promote / Demote that does
 *  it. A move needs a reason: it is audited, and promoting to enforcing holds
 *  every open hold-type finding of that engine at once. */
export function ReadinessTable({ engines, onChanged }: { engines: EngineReadiness[]; onChanged: (message: string) => void }) {
  const [moving, setMoving] = useState<{ engine: string; from: string; to: string } | null>(null);
  const [reason, setReason] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function open(engine: string, from: string, to: string) {
    setMoving({ engine, from, to });
    setReason("");
    setProblem(null);
  }

  async function submit() {
    if (!moving) return;
    setBusy(true);
    setProblem(null);
    try {
      await api(`/api/trust/engines/${encodeURIComponent(moving.engine)}/mode`, {
        method: "POST",
        body: JSON.stringify({ mode: moving.to, reason: reason.trim() }),
      });
      onChanged(`${ENGINE_LABELS[moving.engine] ?? moving.engine} is now ${moving.to}.`);
      setMoving(null);
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="panel table-scroll" style={{ padding: 8 }}>
        <table className="data-table" aria-label="Engine readiness">
          <thead>
            <tr>
              <th>Engine</th>
              <th>Mode</th>
              <th>Since</th>
              <th>Findings</th>
              <th>Would have held</th>
              <th>Held</th>
              <th>Open holds</th>
              <th>Decided</th>
              <th>Precision</th>
              <th>Failures</th>
              <th>Recommendation</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {engines.map((row) => {
              const at = MODES.indexOf(row.mode as (typeof MODES)[number]);
              const up = at >= 0 && at < MODES.length - 1 ? MODES[at + 1] : null;
              const down = at > 0 ? MODES[at - 1] : null;
              const label = ENGINE_LABELS[row.engine] ?? row.engine;
              return (
                <tr key={row.engine}>
                  <td>{label}</td>
                  <td>
                    <span className={modeBadge(row.mode)}>{row.mode}</span>
                  </td>
                  <td>
                    {row.since ? new Date(row.since).toLocaleDateString() : "—"}
                    {row.days_in_mode !== null ? ` · ${row.days_in_mode} d` : ""}
                  </td>
                  <td>{row.findings}</td>
                  <td>{row.would_have_held}</td>
                  <td>{row.held}</td>
                  <td>{row.open_holds}</td>
                  <td>{row.triage.decided}</td>
                  <td>{percent(row.triage.precision)}</td>
                  <td>{row.failures}</td>
                  <td data-recommendation={row.engine}>{row.recommendation}</td>
                  <td>
                    <div className="row" style={{ gap: 4 }}>
                      {up ? (
                        <button
                          type="button"
                          className="btn-chip"
                          aria-label={`Promote ${label} to ${up}`}
                          onClick={() => open(row.engine, row.mode, up)}
                        >
                          ▲ {up}
                        </button>
                      ) : null}
                      {down ? (
                        <button
                          type="button"
                          className="btn-chip"
                          aria-label={`Demote ${label} to ${down}`}
                          onClick={() => open(row.engine, row.mode, down)}
                        >
                          ▼ {down}
                        </button>
                      ) : null}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {moving ? (
        <div className="dialog-scrim" role="presentation" onClick={() => setMoving(null)}>
          <div
            className="panel dialog"
            role="dialog"
            aria-modal="true"
            aria-label="Change engine mode"
            onClick={(event) => event.stopPropagation()}
          >
            <h2 style={{ margin: "0 0 4px" }}>
              {ENGINE_LABELS[moving.engine] ?? moving.engine}: {moving.from} → {moving.to}
            </h2>
            {moving.to === "enforcing" ? (
              <p style={{ margin: 0, fontSize: 12.5 }}>
                Enforcing holds payment on every open hold-type finding of this engine, starting now.
              </p>
            ) : null}
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
            {problem ? (
              <p role="alert" style={{ margin: 0, color: "var(--error)", fontSize: 12.5 }}>
                {problem}
              </p>
            ) : null}
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button type="button" className="btn-secondary" onClick={() => setMoving(null)}>
                Cancel
              </button>
              <button type="button" className="btn-primary" disabled={busy || !reason.trim()} onClick={submit}>
                Set {moving.to}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
