"use client";

import { useState } from "react";
import { api, ApiError, type TrustFinding, type TrustFindingDetail, type TransitionRequest } from "@/lib/api";
import { LEDGER_STATUSES, REASON_REQUIRED, STATUS_LABELS, money } from "@/lib/trust";

/** Move a finding to `toStatus`, with the reason the ledger will keep.
 *
 *  `expect` carries the status the viewer saw, so a decision taken on a stale
 *  page is refused (409) rather than applied over someone else's. */
export function TransitionDialog({
  finding,
  toStatus,
  onCancel,
  onDone,
  onMoved,
}: {
  finding: TrustFindingDetail;
  toStatus: string;
  onCancel: () => void;
  onDone: (updated: TrustFinding) => void;
  /** The finding is no longer in the status the page showed. */
  onMoved: (current: string) => void;
}) {
  const [reason, setReason] = useState("");
  const [amount, setAmount] = useState(LEDGER_STATUSES.has(toStatus) ? (finding.impact ?? "") : "");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const needsReason = REASON_REQUIRED.has(toStatus);
  const ready = !busy && (!needsReason || reason.trim().length > 0);
  const label = STATUS_LABELS[toStatus] ?? toStatus;

  async function submit() {
    setBusy(true);
    setProblem(null);
    const body: TransitionRequest = { to_status: toStatus, expect: finding.status };
    if (reason.trim()) body.reason = reason.trim();
    // Blank leaves the server to prefill the ledger from the impact.
    if (amount.trim()) body.amount = amount.trim();
    try {
      onDone(
        await api<TrustFinding>(`/api/trust/findings/${encodeURIComponent(finding.id)}/transition`, {
          method: "POST",
          body: JSON.stringify(body),
        }),
      );
    } catch (caught) {
      const current = (caught as ApiError)?.detail as { current?: string } | undefined;
      if (caught instanceof ApiError && caught.status === 409 && current?.current) {
        onMoved(current.current);
      } else {
        setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dialog-scrim" role="presentation" onClick={onCancel}>
      <div
        className="panel dialog"
        role="dialog"
        aria-modal="true"
        aria-label={`Mark finding ${label}`}
        onClick={(event) => event.stopPropagation()}
      >
        <h2 style={{ margin: "0 0 4px" }}>Mark as {label}</h2>
        <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
          {finding.type} · {finding.summary ?? "—"} · {money(finding.impact, finding.currency)}
        </p>
        {LEDGER_STATUSES.has(toStatus) ? (
          <label className="stack" style={{ gap: 4 }}>
            <span className="eyebrow">Amount ({finding.currency ?? "INR"})</span>
            <input
              className="input-field"
              inputMode="decimal"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
            />
          </label>
        ) : null}
        <label className="stack" style={{ gap: 4 }}>
          <span className="eyebrow">Reason {needsReason ? "(required)" : "(optional)"}</span>
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
          <button type="button" className="btn-secondary" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="btn-primary" disabled={!ready} onClick={submit}>
            {label}
          </button>
        </div>
      </div>
    </div>
  );
}
