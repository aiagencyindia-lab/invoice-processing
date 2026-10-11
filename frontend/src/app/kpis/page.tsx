"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { Tile } from "@/components/Tile";
import { REASON_LABELS, type KpiReason, type Kpis } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

const WINDOWS: { days: number; label: string }[] = [
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
  { days: 90, label: "90 days" },
  { days: 0, label: "All time" },
];

function percent(rate: number | null | undefined): string {
  // null is "no denominator", which is not 0%.
  if (rate === null || rate === undefined) return "—";
  return `${Math.round(rate * 100)}%`;
}

function hours(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value < 1) return `${Math.round(value * 60)} min`;
  if (value < 48) return `${value.toFixed(1)} h`;
  return `${(value / 24).toFixed(1)} d`;
}

function money(value: number | null | undefined, digits = 4): string {
  if (value === null || value === undefined) return "—";
  return `$${value.toFixed(digits)}`;
}

function KpisView() {
  const params = useSearchParams();
  const router = useRouter();
  // Unknown or empty ?days= falls back to 30 rather than querying an odd window.
  const raw = Number(params.get("days") || "30");
  const days = WINDOWS.some((w) => w.days === raw) ? raw : 30;
  const { data: latest, error } = usePoll<Kpis>(`/api/kpis?days=${days}`);
  // usePoll keeps the previous window's data while a new path loads or after a
  // failed fetch; the response names its own window, so only trust a match.
  const data = latest && !error && latest.window.days === days ? latest : null;

  const reasons = data
    ? (Object.keys(data.exceptions) as KpiReason[]).filter(
        (reason) => data.exceptions[reason] > 0,
      )
    : [];
  const exceptions = data ? data.received - data.touchless : 0;

  return (
    <div className="stack" style={{ gap: 24 }}>
      <div className="row-between">
        <h1>KPIs</h1>
        <div role="group" aria-label="Window" style={{ display: "flex", gap: 6 }}>
          {WINDOWS.map((w) => (
            <button
              key={w.days}
              type="button"
              className="btn-chip"
              aria-pressed={w.days === days}
              style={w.days === days ? { borderColor: "var(--ink)", background: "var(--surface-alt)" } : undefined}
              onClick={() => router.replace(`/kpis?days=${w.days}`)}
            >
              {w.label}
            </button>
          ))}
        </div>
      </div>

      {error ? (
        <div className="empty-state" role="alert">
          {error}
        </div>
      ) : null}
      {!error && !data ? <div className="empty-state">Loading…</div> : null}
      {data && data.received === 0 ? (
        <div className="empty-state">No invoices received in this window.</div>
      ) : null}

      {data && data.received > 0 ? (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
              gap: 16,
            }}
          >
            <Tile
              label="Received"
              value={String(data.received)}
              hint={`${data.decided} decided`}
              testId="received"
            />
            <Tile
              label="Touchless rate"
              value={percent(data.touchless_rate)}
              hint={`${data.touchless} cleared without a person`}
              testId="touchless"
            />
            <Tile
              label="Exception rate"
              value={percent(data.exception_rate)}
              hint={`${exceptions} needed a person`}
              testId="exceptions"
            />
            <Tile
              label="Median cycle time"
              value={hours(data.cycle_hours.median)}
              hint={
                `p90 ${hours(data.cycle_hours.p90)}` +
                (data.cycle_hours.skipped
                  ? ` · ${data.cycle_hours.skipped} skipped (decided before received)`
                  : "")
              }
              testId="cycle"
            />
            <Tile
              label="Open"
              value={String(data.open.count)}
              hint={
                data.open.oldest_hours === null
                  ? "nothing waiting"
                  : `oldest waiting ${hours(data.open.oldest_hours)}`
              }
              testId="open"
            />
            <Tile
              label="Three-way hold rate"
              value={percent(data.three_way.hold_rate)}
              hint={`${data.three_way.held} held of ${data.three_way.po_invoices} PO-backed`}
              testId="three-way"
            />
            <Tile
              label="AI cost per invoice"
              value={money(data.ai_cost.per_invoice_usd)}
              hint={`${money(data.ai_cost.total_usd, 2)} total model spend`}
              testId="cost"
            />
          </div>

          <section className="stack" style={{ gap: 10 }}>
            <h2>Why invoices needed a person</h2>
            {reasons.length === 0 ? (
              <div className="empty-state">Every invoice in this window cleared untouched.</div>
            ) : (
              <div className="panel table-scroll" style={{ padding: 8 }}>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Reason</th>
                      <th>Invoices</th>
                      <th>Share</th>
                    </tr>
                  </thead>
                  <tbody>
                    {reasons.map((reason) => (
                      <tr key={reason} data-reason={reason}>
                        <td>
                          <Link href={`/invoices?reason=${reason}&days=${days}`}>
                            {REASON_LABELS[reason]}
                          </Link>
                        </td>
                        <td>{data.exceptions[reason]}</td>
                        <td className="muted">
                          {exceptions
                            ? `${Math.round((data.exceptions[reason] / exceptions) * 100)}%`
                            : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      ) : null}
    </div>
  );
}

export default function KpisPage() {
  // useSearchParams needs a Suspense boundary for this route to stay static.
  return (
    <Suspense fallback={<div className="empty-state">Loading…</div>}>
      <KpisView />
    </Suspense>
  );
}
