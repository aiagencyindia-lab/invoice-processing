"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";

/** "Was this explanation correct? useful?" -- two yes/no pairs and a note,
 *  posted once. The Why engine aggregates them per finding type. */
export function RatingButtons({ findingId }: { findingId: string }) {
  const [correct, setCorrect] = useState<boolean | null>(null);
  const [useful, setUseful] = useState<boolean | null>(null);
  const [note, setNote] = useState("");
  const [state, setState] = useState<{ done: boolean; problem: string | null }>({ done: false, problem: null });

  async function send() {
    try {
      await api(`/api/trust/findings/${encodeURIComponent(findingId)}/rating`, {
        method: "POST",
        body: JSON.stringify({ correct, useful, note: note.trim() || undefined }),
      });
      setState({ done: true, problem: null });
    } catch (caught) {
      setState({ done: false, problem: caught instanceof ApiError ? caught.friendly : String(caught) });
    }
  }

  if (state.done) return <p className="muted">Thanks — your rating was recorded.</p>;

  const pair = (label: string, value: boolean | null, set: (v: boolean) => void) => (
    <div className="row" style={{ gap: 6, alignItems: "center" }} role="group" aria-label={label}>
      <span style={{ fontSize: 12.5 }}>{label}</span>
      <button type="button" className={value === true ? "btn-chip is-active" : "btn-chip"} onClick={() => set(true)}>
        ✓ Yes
      </button>
      <button type="button" className={value === false ? "btn-chip is-active" : "btn-chip"} onClick={() => set(false)}>
        ✗ No
      </button>
    </div>
  );

  return (
    <div className="stack" style={{ gap: 8 }}>
      {pair("Correct?", correct, setCorrect)}
      {pair("Useful?", useful, setUseful)}
      <input
        className="input-field"
        placeholder="Note (optional)"
        value={note}
        onChange={(event) => setNote(event.target.value)}
      />
      {state.problem ? (
        <p role="alert" style={{ margin: 0, color: "var(--error)", fontSize: 12.5 }}>
          {state.problem}
        </p>
      ) : null}
      <div>
        <button type="button" className="btn-secondary" disabled={correct === null || useful === null} onClick={send}>
          Rate this explanation
        </button>
      </div>
    </div>
  );
}
