"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { DimensionMeter, percent } from "@/components/trust/DimensionMeter";
import { Tile } from "@/components/Tile";
import type { TrustPosture } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { money } from "@/lib/trust";

const WINDOWS: { days: number; label: string }[] = [
  { days: 30, label: "30 days" },
  { days: 90, label: "90 days" },
  { days: 365, label: "12 months" },
  { days: 0, label: "All time" },
];

/** A date, not a timestamp: the window is whole days, and a path that changed
 *  every millisecond would refetch on every render. */
function sinceDate(days: number): string {
  return new Date(Date.now() - days * 86_400_000).toISOString().slice(0, 10);
}

function PostureView() {
  const params = useSearchParams();
  const router = useRouter();
  const raw = Number(params.get("days") ?? "90");
  const days = WINDOWS.some((w) => w.days === raw) ? raw : 90;
  const since = days ? sinceDate(days) : null;
  const { data: latest, error } = usePoll<TrustPosture>(
    since ? `/api/trust/posture?since=${since}` : "/api/trust/posture",
  );
  // usePoll keeps the last answer through a failed fetch; an error wins.
  const data = error ? null : latest;

  const types = Object.entries(data?.by_type ?? {}).sort((a, b) => b[1].count - a[1].count);
  const buckets = Object.entries(data?.provenance_distribution ?? {});
  const bucketMax = Math.max(1, ...buckets.map(([, count]) => count));

  return (
    <div className="stack" style={{ gap: 24 }}>
      <div className="row-between">
        <h1>Trust posture</h1>
        <div role="group" aria-label="Window" style={{ display: "flex", gap: 6 }}>
          {WINDOWS.map((w) => (
            <button
              key={w.days}
              type="button"
              className="btn-chip"
              aria-pressed={w.days === days}
              style={w.days === days ? { borderColor: "var(--ink)", background: "var(--surface-alt)" } : undefined}
              onClick={() => router.replace(`/trust-posture?days=${w.days}`)}
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

      {data ? (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12 }}>
            <Tile label="Found" value={money(data.found)} testId="found" />
            <Tile label="Prevented" value={money(data.prevented)} testId="prevented" />
            <Tile label="Recovered" value={money(data.recovered)} testId="recovered" />
            <Tile label="Written off" value={money(data.written_off)} testId="written_off" />
            <Tile label="Open exposure" value={money(data.open_exposure)} testId="open_exposure" href="/findings?open=true" />
            <Tile
              label="Holds"
              value={String(data.holds.open)}
              hint={`${data.holds.placed} placed · ${data.holds.lifted} lifted`}
              href="/findings?hold=true"
              testId="holds"
            />
          </div>

          <div className="panel stack" style={{ padding: 16, gap: 6 }}>
            <div className="eyebrow">Caught before payment</div>
            <progress
              max={1}
              value={data.pre_payment_share ?? 0}
              aria-label="Share of findings caught before payment"
              style={{ width: "100%" }}
            />
            <span className="muted" style={{ fontSize: 12.5 }}>
              {data.pre_payment_share === null
                ? "No decided findings in this window."
                : `${percent(data.pre_payment_share)} of findings were caught before payment.`}
            </span>
          </div>

          <div className="panel stack" style={{ padding: 16, gap: 10 }} aria-label="Verification coverage">
            <div className="eyebrow">Verification coverage</div>
            {(["identity", "reality", "authenticity"] as const).map((name) => (
              <DimensionMeter key={name} name={name} score={data.verification_coverage[name]} />
            ))}
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 16 }}>
            <div className="panel table-scroll" style={{ padding: 8 }}>
              <table className="data-table" aria-label="By type">
                <thead>
                  <tr>
                    <th>Type</th>
                    <th>Count</th>
                    <th>Impact</th>
                  </tr>
                </thead>
                <tbody>
                  {types.length ? (
                    types.map(([type, row]) => (
                      <tr key={type}>
                        <td>
                          <Link href={`/findings?type=${encodeURIComponent(type)}`}>{type}</Link>
                        </td>
                        <td>{row.count}</td>
                        <td>{money(row.impact)}</td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={3} className="muted">
                        No findings in this window.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            <div className="panel table-scroll" style={{ padding: 8 }}>
              <table className="data-table" aria-label="By vendor">
                <thead>
                  <tr>
                    <th>Vendor</th>
                    <th>Count</th>
                    <th>Impact</th>
                  </tr>
                </thead>
                <tbody>
                  {data.by_vendor.length ? (
                    data.by_vendor.map((row) => (
                      <tr key={row.node}>
                        <td>
                          <Link href={`/findings?vendor=${encodeURIComponent(row.node)}`}>{row.name ?? row.node}</Link>
                        </td>
                        <td>{row.count}</td>
                        <td>{money(row.impact)}</td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={3} className="muted">
                        No findings in this window.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div className="panel stack" style={{ padding: 16, gap: 6 }} aria-label="Document authenticity">
            <div className="eyebrow">Document authenticity scores</div>
            {buckets.length ? (
              buckets.map(([bucket, count]) => (
                <div key={bucket} className="row" style={{ gap: 8, alignItems: "center", fontSize: 12.5 }}>
                  <span style={{ width: 72, fontVariantNumeric: "tabular-nums" }}>{bucket}</span>
                  <span style={{ flex: 1 }}>
                    <span
                      style={{
                        display: "block",
                        height: 8,
                        borderRadius: 4,
                        background: "var(--ink)",
                        width: `${Math.round((count / bucketMax) * 100)}%`,
                      }}
                    />
                  </span>
                  <span style={{ width: 40, textAlign: "right" }}>{count}</span>
                </div>
              ))
            ) : (
              <span className="muted" style={{ fontSize: 12.5 }}>
                No documents scored yet.
              </span>
            )}
          </div>
        </>
      ) : null}
    </div>
  );
}

export default function TrustPosturePage() {
  // useSearchParams needs a Suspense boundary for the static build.
  return (
    <Suspense fallback={<div className="empty-state">Loading…</div>}>
      <PostureView />
    </Suspense>
  );
}
