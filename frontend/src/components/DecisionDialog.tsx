"use client";

import { useState } from "react";
import { REASON_CATEGORIES } from "@/lib/api";

/** Why this decision, in one small form.
 *
 *  It replaces a `window.prompt`, which could ask for one line of free text
 *  and nothing else. The category is what the system can act on -- only
 *  "policy exception" describes a rule the rulebook is missing -- and the note
 *  is what a person reads, and what a rule writer would be given to work from.
 */
export function DecisionDialog({
  action,
  onCancel,
  onConfirm,
}: {
  action: "approve" | "reject";
  onCancel: () => void;
  onConfirm: (reason: string, category: string) => void;
}) {
  const [category, setCategory] = useState("policy_exception");
  const [reason, setReason] = useState("");
  const rejecting = action === "reject";
  // The API refuses a reasonless rejection, so the button does too rather than
  // letting someone press it and read an error.
  const ready = !rejecting || reason.trim().length > 0;

  return (
    <div className="dialog-scrim" role="presentation" onClick={onCancel}>
      <div
        className="panel dialog"
        role="dialog"
        aria-modal="true"
        aria-label={rejecting ? "Reject invoice" : "Approve invoice"}
        onClick={(event) => event.stopPropagation()}
      >
        <h2 style={{ margin: "0 0 4px" }}>
          {rejecting ? "Reject this invoice" : "Approve this invoice"}
        </h2>
        <label className="stack" style={{ gap: 4 }}>
          <span className="eyebrow">Why</span>
          <select
            className="input-field"
            value={category}
            autoFocus
            onChange={(event) => setCategory(event.target.value)}
          >
            {REASON_CATEGORIES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="eyebrow">
            Note {rejecting ? "(required)" : "(optional)"}
          </span>
          <textarea
            className="input-field"
            rows={3}
            value={reason}
            placeholder="What a colleague would need to know in six months."
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button type="button" className="btn-secondary" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={!ready}
            onClick={() => onConfirm(reason.trim(), category)}
          >
            {rejecting ? "Reject" : "Approve"}
          </button>
        </div>
      </div>
    </div>
  );
}
