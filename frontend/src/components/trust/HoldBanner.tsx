import Link from "next/link";
import type { HoldDetail } from "@/lib/api";
import { ENGINE_LABELS } from "@/lib/trust";

/** Why the trust layer holds this payment, or which enforcing engines have not
 *  looked at it yet. Nothing at all when the invoice is free to pay: a green
 *  "no holds" bar on every invoice would train people to stop reading it. */
export function HoldBanner({ hold, isAdmin }: { hold: HoldDetail | null | undefined; isAdmin: boolean }) {
  if (!hold || (!hold.held && !hold.waiting.length)) return null;
  return (
    <div
      className="panel stack"
      role="alert"
      aria-label="Payment hold"
      style={{ padding: "12px 14px", gap: 6, borderLeft: "3px solid var(--warning)" }}
    >
      <strong style={{ fontSize: 13 }}>⚠ {hold.held ? "Payment on hold" : "Waiting on trust checks"}</strong>
      {hold.message ? (
        <span className="muted" style={{ fontSize: 12.5 }}>
          {hold.message}
        </span>
      ) : null}
      {hold.holds.length ? (
        <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5 }}>
          {hold.holds.map((item) => (
            <li key={item.finding_id}>
              <Link href={`/findings/${encodeURIComponent(item.finding_id)}`}>{item.type}</Link> ·{" "}
              {ENGINE_LABELS[item.engine] ?? item.engine} · {item.summary}
            </li>
          ))}
        </ul>
      ) : null}
      {hold.waiting.length ? (
        <span style={{ fontSize: 12.5 }}>
          Not checked yet by {hold.waiting.map((engine) => ENGINE_LABELS[engine] ?? engine).join(", ")}.
        </span>
      ) : null}
      {isAdmin && hold.held ? (
        <span className="muted" style={{ fontSize: 12 }}>
          An administrator can override the hold at approval, with a reason.
        </span>
      ) : null}
    </div>
  );
}
