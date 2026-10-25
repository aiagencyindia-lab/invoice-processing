"use client";

import Link from "next/link";
import type { TrustFinding } from "@/lib/api";
import { ENGINE_LABELS, STATUS_LABELS, money, statusBadge } from "@/lib/trust";

/** One queue row. `onTriage` is given only when the viewer may triage and the
 *  finding is still `detected`; the page decides both. */
export function FindingRow({ finding, onTriage }: { finding: TrustFinding; onTriage?: () => void }) {
  const f = finding;
  return (
    <tr data-finding={f.id}>
      <td>
        <Link href={`/findings/${encodeURIComponent(f.id)}`}>{f.type}</Link>
        {f.hold ? (
          <span className="badge badge-warn" style={{ marginLeft: 6 }}>
            hold
          </span>
        ) : null}
      </td>
      <td>{f.summary ?? "—"}</td>
      <td>{f.vendor?.name ?? "—"}</td>
      <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{money(f.impact, f.currency)}</td>
      <td>
        <span className={`badge ${statusBadge(f.status)}`}>{STATUS_LABELS[f.status] ?? f.status}</span>
      </td>
      <td>{f.engine ? (ENGINE_LABELS[f.engine] ?? f.engine) : "—"}</td>
      <td>{f.confidence === null ? "—" : `${Math.round(f.confidence * 100)}%`}</td>
      <td>
        {onTriage ? (
          <button type="button" className="btn-secondary" onClick={onTriage}>
            Triage
          </button>
        ) : null}
      </td>
    </tr>
  );
}
