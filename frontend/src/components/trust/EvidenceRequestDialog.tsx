"use client";

import { useState } from "react";
import { api, ApiError, type EvidenceRequest, type TrustFindingDetail } from "@/lib/api";

/** Ask someone for the document that would settle a finding: an internal
 *  tracker entry with an owner and a due date. Nothing is emailed; the
 *  finding moves to "needs vendor input" until proof is attached. */
export function EvidenceRequestDialog({
  finding,
  onCancel,
  onDone,
}: {
  finding: TrustFindingDetail;
  onCancel: () => void;
  onDone: (request: EvidenceRequest) => void;
}) {
  const [owner, setOwner] = useState("");
  const [due, setDue] = useState("");
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ready = !busy && owner.trim().length > 0 && due.length > 0;

  async function submit() {
    setBusy(true);
    setProblem(null);
    try {
      onDone(
        await api<EvidenceRequest>(`/api/trust/findings/${encodeURIComponent(finding.id)}/evidence-request`, {
          method: "POST",
          body: JSON.stringify({ owner: owner.trim(), due_date: due, note: note.trim() || null }),
        }),
      );
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
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
        aria-label="Request evidence"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 style={{ margin: "0 0 4px" }}>Request evidence</h2>
        <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
          {finding.type} · {finding.summary ?? "—"}
        </p>
        <label className="stack" style={{ gap: 4 }}>
          <span className="eyebrow">Owner</span>
          <input
            className="input-field"
            type="email"
            autoFocus
            placeholder="who chases the document"
            value={owner}
            onChange={(event) => setOwner(event.target.value)}
          />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="eyebrow">Due date</span>
          <input className="input-field" type="date" value={due} onChange={(event) => setDue(event.target.value)} />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="eyebrow">Note (optional)</span>
          <textarea
            className="input-field"
            rows={3}
            maxLength={500}
            value={note}
            onChange={(event) => setNote(event.target.value)}
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
            Send request
          </button>
        </div>
      </div>
    </div>
  );
}
