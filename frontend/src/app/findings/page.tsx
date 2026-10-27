"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { FindingRow } from "@/components/trust/FindingRow";
import { Tile } from "@/components/Tile";
import {
  api,
  ApiError,
  can,
  findingsQuery,
  type Me,
  type TrustFindingFilters,
  type TrustFindingPage,
  type TrustFindingSummary,
} from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { ENGINE_LABELS, STATUS_LABELS, vendorLabel } from "@/lib/trust";

const OPEN = ["detected", "triaged", "needs_vendor_input"];
const PAGE = 50;

function FindingsView() {
  const params = useSearchParams();
  const router = useRouter();
  const { data: me } = usePoll<Me>("/api/me");
  const [notice, setNotice] = useState<string | null>(null);

  // The URL is the filter state, so a view can be bookmarked or sent to a
  // colleague; the Payment Trust queue is just this page with hold=true.
  const filters: TrustFindingFilters = {
    status: params.get("status") ?? undefined,
    open: params.get("open") === "true",
    type: params.get("type") ?? undefined,
    engine: params.get("engine") ?? undefined,
    vendor: params.get("vendor") ?? undefined,
    hold: params.get("hold") === "true",
    since: params.get("since") ?? undefined,
    until: params.get("until") ?? undefined,
    offset: Number(params.get("offset") || 0) || undefined,
    limit: PAGE,
  };
  const query = findingsQuery(filters);
  const { data, error, reload } = usePoll<TrustFindingPage>(`/api/trust/findings?${query}`);
  const { data: summary, reload: reloadSummary } = usePoll<TrustFindingSummary>("/api/trust/findings/summary");

  function set(changes: Record<string, string | boolean | number | undefined>) {
    const next = new URLSearchParams(params.toString());
    Object.entries({ offset: undefined, ...changes }).forEach(([key, value]) => {
      if (value === undefined || value === "" || value === false || value === 0) next.delete(key);
      else next.set(key, String(value));
    });
    router.replace(`/findings?${next.toString()}`);
  }

  async function triage(id: string) {
    setNotice(null);
    try {
      await api(`/api/trust/findings/${encodeURIComponent(id)}/transition`, {
        method: "POST",
        body: JSON.stringify({ to_status: "triaged", expect: "detected" }),
      });
      setNotice("Finding triaged.");
    } catch (caught) {
      const moved = caught instanceof ApiError && caught.status === 409 ? (caught.detail as { current?: string; message?: string }) : null;
      setNotice(
        moved?.current
          ? `This finding moved to ${STATUS_LABELS[moved.current] ?? moved.current}; reloaded.`
          : caught instanceof ApiError
            ? caught.friendly
            : String(caught),
      );
    }
    reload();
    reloadSummary();
  }

  const open = OPEN.reduce((n, s) => n + (summary?.by_status[s] ?? 0), 0);
  const findings = data?.findings ?? [];
  const total = data?.total ?? 0;
  const offset = filters.offset ?? 0;
  const dateOf = (value: string | undefined) => (value ? value.slice(0, 10) : "");

  return (
    <section className="stack" style={{ gap: 16 }}>
      <div className="row-between">
        <h1>Findings</h1>
        <label className="row" style={{ gap: 6, alignItems: "center", fontSize: 13 }}>
          <input type="checkbox" checked={!!filters.hold} onChange={(event) => set({ hold: event.target.checked })} />
          Payment Trust queue
        </label>
      </div>

      {summary ? (
        <div className="tiles">
          <Tile label="Open" value={String(open)} hint="detected, triaged, needs vendor input" />
          <Tile label="Holds open" value={String(summary.hold_open)} hint="placed and not lifted" />
          <Tile label="Recovered" value={String(summary.by_status.recovered ?? 0)} />
          <Tile label="Prevented" value={String(summary.by_status.prevented ?? 0)} />
        </div>
      ) : null}

      <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "end" }}>
        <label className="stack" style={{ gap: 2, fontSize: 12.5 }}>
          Status
          <select className="input-field" value={filters.status ?? ""} onChange={(event) => set({ status: event.target.value })}>
            <option value="">Any</option>
            {Object.entries(STATUS_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="stack" style={{ gap: 2, fontSize: 12.5 }}>
          Engine
          <select className="input-field" value={filters.engine ?? ""} onChange={(event) => set({ engine: event.target.value })}>
            <option value="">Any</option>
            {Object.entries(ENGINE_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="stack" style={{ gap: 2, fontSize: 12.5 }}>
          Vendor
          <select className="input-field" value={filters.vendor ?? ""} onChange={(event) => set({ vendor: event.target.value })}>
            <option value="">Any</option>
            {(summary?.vendors ?? []).map((v) => (
              <option key={v.node} value={v.node}>
                {vendorLabel(v)} ({v.count})
              </option>
            ))}
          </select>
        </label>
        <label className="stack" style={{ gap: 2, fontSize: 12.5 }}>
          Type
          <select className="input-field" value={filters.type ?? ""} onChange={(event) => set({ type: event.target.value })}>
            <option value="">Any</option>
            {Object.keys(summary?.by_type ?? {}).map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </label>
        <label className="stack" style={{ gap: 2, fontSize: 12.5 }}>
          Since
          <input type="date" className="input-field" value={dateOf(filters.since)} onChange={(event) => set({ since: event.target.value })} />
        </label>
        <label className="stack" style={{ gap: 2, fontSize: 12.5 }}>
          Until
          <input type="date" className="input-field" value={dateOf(filters.until)} onChange={(event) => set({ until: event.target.value })} />
        </label>
        <label className="row" style={{ gap: 6, alignItems: "center", fontSize: 12.5, paddingBottom: 8 }}>
          <input type="checkbox" checked={!!filters.open} onChange={(event) => set({ open: event.target.checked })} />
          Open only
        </label>
      </div>

      {error ? <div className="empty-state">{error}</div> : null}
      {data && findings.length === 0 ? <div className="empty-state">No findings match these filters.</div> : null}
      {findings.length ? (
        <div className="panel table-scroll" style={{ padding: 8 }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>Type</th>
                <th>Summary</th>
                <th>Vendor</th>
                <th style={{ textAlign: "right" }}>Impact</th>
                <th>Status</th>
                <th>Engine</th>
                <th>Confidence</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {findings.map((f) => (
                <FindingRow
                  key={f.id}
                  finding={f}
                  onTriage={f.status === "detected" && can(me, "clerk") ? () => triage(f.id) : undefined}
                />
              ))}
            </tbody>
          </table>
          <div className="row-between" style={{ padding: "8px 4px", fontSize: 12.5 }}>
            <span className="muted">
              {offset + 1}–{Math.min(offset + PAGE, total)} of {total}
            </span>
            <span className="row" style={{ gap: 8 }}>
              <button type="button" className="btn-secondary" disabled={offset === 0} onClick={() => set({ offset: Math.max(0, offset - PAGE) })}>
                ‹ Previous
              </button>
              <button type="button" className="btn-secondary" disabled={offset + PAGE >= total} onClick={() => set({ offset: offset + PAGE })}>
                Next ›
              </button>
            </span>
          </div>
        </div>
      ) : null}

      {notice ? (
        <div className="toast" role="status">
          {notice}
        </div>
      ) : null}
    </section>
  );
}

export default function FindingsPage() {
  // useSearchParams needs a Suspense boundary for this route to stay static.
  return (
    <Suspense fallback={<div className="empty-state">Loading…</div>}>
      <FindingsView />
    </Suspense>
  );
}
