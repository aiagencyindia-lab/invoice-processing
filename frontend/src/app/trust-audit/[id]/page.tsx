"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { Tile } from "@/components/Tile";
import {
  api,
  ApiError,
  can,
  downloadFile,
  type AuditReport,
  type AuditReportRow,
  type AuditRun,
  type AuditSample,
  type ContractRow,
  type Me,
  type TrustAuditDetail,
  type TrustFindingSummary,
} from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { ENGINE_LABELS, STATUS_LABELS, money } from "@/lib/trust";

const TABS = ["Overview", "Sampling", "Report", "Ledger", "Contracts"] as const;
type Tab = (typeof TABS)[number];
const ACTIVE = new Set(["queued", "running"]);
const ENGINES = ["leakage", "payment", "provenance", "gst_itc"];

function friendly(caught: unknown): string {
  return caught instanceof ApiError ? caught.friendly : String(caught);
}

function RowTable({ rows, first }: { rows: AuditReportRow[]; first: "type" | "vendor" }) {
  if (!rows.length) return <div className="empty-state">Nothing in scope.</div>;
  return (
    <div className="panel table-scroll" style={{ padding: 8 }}>
      <table className="data-table">
        <thead>
          <tr>
            <th>{first === "type" ? "Type" : "Vendor"}</th>
            <th>Count</th>
            <th>Impact</th>
            <th>Recovered</th>
            <th>Prevented</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              <td>{first === "type" ? `${row.type}${row.engine ? ` · ${ENGINE_LABELS[row.engine] ?? row.engine}` : ""}` : (row.vendor ?? "—")}</td>
              <td>{row.count}</td>
              <td>{money(row.impact)}</td>
              <td>{money(row.recovered)}</td>
              <td>{money(row.prevented)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function TrustAuditDetailPage() {
  const { id } = useParams<{ id: string }>();
  const path = `/api/trust/audits/${encodeURIComponent(id)}`;
  const { data: me } = usePoll<Me>("/api/me");
  const { data: audit, error, reload } = usePoll<TrustAuditDetail>(path);
  const [tab, setTab] = useState<Tab>("Overview");
  const [notice, setNotice] = useState<string | null>(null);
  const [size, setSize] = useState("20");
  const [engines, setEngines] = useState<string[]>(ENGINES);
  // Vendor node ids read as names through the same mapping the form used.
  const { data: summary } = usePoll<TrustFindingSummary>("/api/trust/findings/summary");

  // Poll the latest run every 3 s while it is queued or running; once it has
  // finished the audit itself carries the result.
  // Runs are not promised in order: the active one is the queued/running one,
  // the latest the most recently started.
  const activeRun = audit?.runs.find((r) => ACTIVE.has(r.status)) ?? null;
  const latest =
    activeRun ??
    (audit?.runs.length
      ? [...audit.runs].sort((a, b) => (a.started_at ?? "").localeCompare(b.started_at ?? "")).at(-1) ?? null
      : null);
  const { data: polled } = usePoll<AuditRun>(activeRun ? `${path}/runs/${encodeURIComponent(activeRun.id)}` : null, 3000);
  const run = polled && activeRun && polled.id === activeRun.id ? polled : latest;
  // The run finished since the audit was fetched: refetch so the audit's own
  // copy, and the samples and report that depend on it, are current.
  const finished = !!(polled && activeRun && polled.id === activeRun.id && !ACTIVE.has(polled.status));
  useEffect(() => {
    if (finished) reload();
  }, [finished, reload]);

  const wantReport = tab === "Report" || tab === "Ledger";
  const { data: report, error: reportError } = usePoll<AuditReport>(wantReport ? `${path}/report?format=json` : null);
  const { data: contracts, error: contractsError } = usePoll<ContractRow[]>(tab === "Contracts" ? "/api/trust/contracts" : null);

  if (error) return <div className="empty-state">{error}</div>;
  if (!audit) return <div className="empty-state">Loading…</div>;

  async function post<T>(sub: string, body: unknown, method = "POST"): Promise<T | null> {
    setNotice(null);
    try {
      const out = await api<T>(`${path}${sub}`, { method, body: JSON.stringify(body) });
      reload();
      return out;
    } catch (caught) {
      setNotice(friendly(caught));
      return null;
    }
  }

  async function download(sub: string, filename: string) {
    setNotice(null);
    try {
      await downloadFile(`${path}${sub}`, filename);
    } catch (caught) {
      setNotice(friendly(caught));
    }
  }

  const canRun = can(me, "approver");
  const canVerdict = can(me, "clerk");
  const progress = run?.progress;

  return (
    <section className="stack" style={{ gap: 16 }}>
      <Link href="/trust-audit" style={{ fontSize: 12.5 }}>
        ‹ Trust audit
      </Link>
      <header className="row-between" style={{ flexWrap: "wrap", gap: 8 }}>
        <div>
          <h1 style={{ margin: 0 }}>{audit.name}</h1>
          <span className="muted" style={{ fontSize: 12.5 }}>
            <span className={`badge ${audit.status === "active" ? "badge-ok" : "badge-neutral"}`}>{audit.status}</span>{" "}
            {audit.period_from ?? "…"} – {audit.period_to ?? "…"} · approver {audit.approver ?? "—"}
          </span>
        </div>
      </header>

      <div role="tablist" className="row" style={{ gap: 6 }}>
        {TABS.map((name) => (
          <button
            key={name}
            type="button"
            role="tab"
            aria-selected={tab === name}
            className={tab === name ? "btn-chip is-active" : "btn-chip"}
            onClick={() => setTab(name)}
          >
            {name}
          </button>
        ))}
      </div>

      {tab === "Overview" ? (
        <div className="stack" style={{ gap: 12 }}>
          <dl className="kv panel" style={{ padding: 16 }}>
            <dt>Vendors</dt>
            <dd>
              {audit.vendors.length
                ? audit.vendors.map((node) => summary?.vendors.find((v) => v.node === node)?.name ?? node).join(", ")
                : "all"}
            </dd>
            <dt>Categories</dt>
            <dd>{audit.categories.length ? audit.categories.join(", ") : "all"}</dd>
            <dt>Terms</dt>
            <dd>
              {audit.terms.recovered_pct}% of recovered · {audit.terms.prevented_pct}% of prevented
              {audit.terms.cap ? ` · cap ${money(audit.terms.cap)}` : ""}
              {audit.terms.minimum ? ` · minimum ${money(audit.terms.minimum)}` : ""}
            </dd>
          </dl>
          <div className="panel stack" style={{ padding: 16, gap: 10 }}>
            <div className="row-between">
              <div className="eyebrow">Batch run</div>
              {canRun ? (
                <span className="row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                  {ENGINES.map((engine) => (
                    <label key={engine} className="row" style={{ gap: 4, alignItems: "center", fontSize: 12.5 }}>
                      <input
                        type="checkbox"
                        checked={engines.includes(engine)}
                        onChange={(event) =>
                          setEngines(event.target.checked ? [...engines, engine] : engines.filter((e) => e !== engine))
                        }
                      />
                      {ENGINE_LABELS[engine] ?? engine}
                    </label>
                  ))}
                  <button
                    type="button"
                    className="btn-primary"
                    disabled={!!activeRun || engines.length === 0}
                    onClick={() => post<AuditRun>("/runs", { engines }).then((out) => out && setNotice("Run queued."))}
                  >
                    Run batch
                  </button>
                </span>
              ) : null}
            </div>
            {run ? (
              <>
                <div className="row" style={{ gap: 8, alignItems: "center" }}>
                  <span className={`badge ${run.status === "done" ? "badge-ok" : run.status === "failed" ? "badge-error" : "badge-warn"}`}>
                    {run.status}
                  </span>
                  {progress ? (
                    <>
                      <progress value={progress.engines_done} max={progress.engines_total || 1} />
                      <span className="muted" style={{ fontSize: 12.5 }}>
                        {progress.engines_done}/{progress.engines_total} engines
                        {progress.engine ? ` · ${ENGINE_LABELS[progress.engine] ?? progress.engine}` : ""}
                      </span>
                    </>
                  ) : null}
                </div>
                {progress ? (
                  <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                    {Object.entries(progress.per_engine).map(([engine, state]) => (
                      <span
                        key={engine}
                        className={`badge ${typeof state === "string" ? (state === "failed" ? "badge-error" : "badge-neutral") : "badge-ok"}`}
                      >
                        {ENGINE_LABELS[engine] ?? engine}:{" "}
                        {typeof state === "string" ? state : `${state.findings} (${state.mode})`}
                      </span>
                    ))}
                  </div>
                ) : null}
                {run.error ? (
                  <p role="alert" style={{ margin: 0, color: "var(--error)", fontSize: 12.5 }}>
                    {run.error}
                  </p>
                ) : null}
                {run.started_at ? (
                  <Link href={`/findings?since=${encodeURIComponent(run.started_at)}`} style={{ fontSize: 12.5 }}>
                    {progress?.findings_so_far ?? 0} findings so far ›
                  </Link>
                ) : null}
              </>
            ) : (
              <span className="muted" style={{ fontSize: 12.5 }}>
                No run yet.
              </span>
            )}
          </div>
        </div>
      ) : null}

      {tab === "Sampling" ? (
        <div className="stack" style={{ gap: 12 }}>
          {canRun ? (
            <div className="row" style={{ gap: 8, alignItems: "end" }}>
              <label className="stack" style={{ gap: 2, fontSize: 12.5 }}>
                Sample size
                <input type="number" min={1} max={200} className="input-field" value={size} onChange={(event) => setSize(event.target.value)} />
              </label>
              <button
                type="button"
                className="btn-primary"
                onClick={() => post<AuditSample>("/samples", { size: Math.min(200, Math.max(1, Math.trunc(Number(size)) || 1)) })}
              >
                Draw sample
              </button>
            </div>
          ) : null}
          {audit.samples.length === 0 ? <div className="empty-state">No sample drawn yet.</div> : null}
          {audit.samples.map((sample) => (
            <div key={sample.id} className="panel stack" style={{ padding: 16, gap: 10 }}>
              <div className="tiles">
                <Tile label="Sampled" value={String(sample.size)} hint={sample.seed ? `seed ${sample.seed}` : undefined} />
                <Tile label="Validated" value={`${sample.validated}/${sample.size}`} />
                <Tile label="Precision" value={sample.precision === null ? "—" : `${Math.round(sample.precision * 100)}%`} hint={`${sample.true} ✓ · ${sample.false} ✗`} />
              </div>
              <table className="data-table">
                <tbody>
                  {sample.finding_ids.map((fid) => {
                    const verdict = sample.verdicts[fid] ?? null;
                    const set = (value: boolean) =>
                      post<AuditSample>(`/samples/${encodeURIComponent(sample.id)}`, { verdicts: { [fid]: value } }, "PATCH");
                    return (
                      <tr key={fid}>
                        <td>
                          <Link href={`/findings/${encodeURIComponent(fid)}`}>{fid}</Link>
                        </td>
                        <td>
                          {canVerdict ? (
                            <span className="row" style={{ gap: 6 }}>
                              <button type="button" className={verdict === true ? "btn-chip is-active" : "btn-chip"} onClick={() => set(true)} aria-label={`Mark ${fid} true`}>
                                ✓
                              </button>
                              <button type="button" className={verdict === false ? "btn-chip is-active" : "btn-chip"} onClick={() => set(false)} aria-label={`Mark ${fid} false`}>
                                ✗
                              </button>
                            </span>
                          ) : verdict === null ? (
                            "—"
                          ) : verdict ? (
                            "✓"
                          ) : (
                            "✗"
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      ) : null}

      {tab === "Report" ? (
        report ? (
          <div className="stack" style={{ gap: 12 }}>
            <div className="row" style={{ gap: 8 }}>
              <button type="button" className="btn-secondary" onClick={() => window.print()}>
                Print view
              </button>
              <button type="button" className="btn-secondary" onClick={() => download("/report?format=json", `audit-${id}-report.json`)}>
                Download JSON
              </button>
            </div>
            <h2 style={{ margin: 0 }}>Executive summary</h2>
            <div className="tiles">
              <Tile label="Findings" value={String(report.summary.findings)} />
              <Tile label="Detected impact" value={money(report.summary.detected_impact, report.summary.currency)} />
              <Tile label="Accepted impact" value={money(report.summary.accepted_impact, report.summary.currency)} />
              <Tile label="Recovered" value={money(report.summary.recovered, report.summary.currency)} />
              <Tile label="Prevented" value={money(report.summary.prevented, report.summary.currency)} />
              <Tile label="Written off" value={money(report.summary.written_off, report.summary.currency)} />
              <Tile label="At-risk holds" value={money(report.summary.at_risk_holds, report.summary.currency)} />
            </div>
            <h2 style={{ margin: 0 }}>By type</h2>
            <RowTable rows={report.by_type} first="type" />
            <h2 style={{ margin: 0 }}>By vendor</h2>
            <RowTable rows={report.by_vendor} first="vendor" />
            <h2 style={{ margin: 0 }}>Top recoveries</h2>
            {report.top_recoveries.length ? (
              <div className="panel table-scroll" style={{ padding: 8 }}>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Finding</th>
                      <th>Vendor</th>
                      <th>Impact</th>
                      <th>Status</th>
                      <th>Summary</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.top_recoveries.map((f) => (
                      <tr key={f.id}>
                        <td>
                          <Link href={`/findings/${encodeURIComponent(f.id)}`}>{f.type}</Link>
                        </td>
                        <td>{f.vendor ?? "—"}</td>
                        <td>{money(f.impact, report.summary.currency)}</td>
                        <td>{STATUS_LABELS[f.status] ?? f.status}</td>
                        <td>{f.summary ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="empty-state">No recoveries yet.</div>
            )}
            <h2 style={{ margin: 0 }}>Prevention</h2>
            {report.prevention.length ? (
              <div className="panel table-scroll" style={{ padding: 8 }}>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Engine</th>
                      <th>Types</th>
                      <th>Count</th>
                      <th>Impact</th>
                      <th>Mode now → suggested</th>
                      <th>Thresholds</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.prevention.map((p) => (
                      <tr key={p.engine}>
                        <td>{ENGINE_LABELS[p.engine] ?? p.engine}</td>
                        <td>{p.finding_types.join(", ")}</td>
                        <td>{p.count}</td>
                        <td>{money(p.impact, report.summary.currency)}</td>
                        <td>
                          {p.mode_now} → {p.mode_suggested}
                        </td>
                        <td>{p.thresholds_hint.join(", ")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="empty-state">Nothing to prevent yet.</div>
            )}
            <h2 style={{ margin: 0 }}>ITC exposure</h2>
            <div className="tiles">
              <Tile label="Findings" value={String(report.itc_exposure.count)} />
              <Tile label="Exposure" value={money(report.itc_exposure.impact, report.summary.currency)} />
            </div>
            <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
              Report v{report.report_version} · generated {new Date(report.generated_at).toLocaleString()}
            </p>
          </div>
        ) : (
          <div className="empty-state">{reportError ?? "Loading the report…"}</div>
        )
      ) : null}

      {tab === "Ledger" ? (
        report ? (
          <div className="stack" style={{ gap: 12 }}>
            <div className="row" style={{ gap: 8 }}>
              <button type="button" className="btn-secondary" onClick={() => download("/ledger?format=csv", `audit-${id}-ledger.csv`)}>
                Download CSV
              </button>
              <button type="button" className="btn-secondary" onClick={() => download("/ledger?format=json", `audit-${id}-ledger.json`)}>
                Download JSON
              </button>
            </div>
            <div className="tiles">
              <Tile label="Recovered" value={money(report.ledger.totals.recovered, report.summary.currency)} />
              <Tile label="Prevented" value={money(report.ledger.totals.prevented, report.summary.currency)} />
              <Tile label="Written off" value={money(report.ledger.totals.written_off, report.summary.currency)} />
              <Tile label="Claimed" value={money(report.ledger.totals.claimed, report.summary.currency)} />
            </div>
            <dl className="kv panel" style={{ padding: 16 }}>
              <dt>Fee on recovered</dt>
              <dd>{money(report.ledger.fee.fee_recovered, report.summary.currency)}</dd>
              <dt>Fee on prevented</dt>
              <dd>{money(report.ledger.fee.fee_prevented, report.summary.currency)}</dd>
              <dt>Gross</dt>
              <dd>{money(report.ledger.fee.gross, report.summary.currency)}</dd>
              <dt>After cap</dt>
              <dd>{money(report.ledger.fee.capped, report.summary.currency)}</dd>
              <dt>Due</dt>
              <dd>
                <strong>{money(report.ledger.fee.due, report.summary.currency)}</strong>
              </dd>
            </dl>
            {report.ledger.rows.length ? (
              <div className="panel table-scroll" style={{ padding: 8 }}>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Finding</th>
                      <th>Vendor</th>
                      <th>Status</th>
                      <th>Amount</th>
                      <th>Attested by</th>
                      <th>When</th>
                      <th>Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.ledger.rows.map((row, index) => (
                      <tr key={index}>
                        <td>
                          <Link href={`/findings/${encodeURIComponent(row.finding_id)}`}>{row.type}</Link>
                        </td>
                        <td>{row.vendor ?? "—"}</td>
                        <td>{STATUS_LABELS[row.to_status] ?? row.to_status}</td>
                        <td>{money(row.amount, report.summary.currency)}</td>
                        <td>{row.attested_by}</td>
                        <td>{row.at ? new Date(row.at).toLocaleString() : "—"}</td>
                        <td>{row.reason ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="empty-state">Nothing attested yet.</div>
            )}
          </div>
        ) : (
          <div className="empty-state">{reportError ?? "Loading the ledger…"}</div>
        )
      ) : null}

      {tab === "Contracts" ? (
        contracts ? (
          contracts.length ? (
            <div className="panel table-scroll" style={{ padding: 8 }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Contract</th>
                    <th>Vendor</th>
                    <th>Since</th>
                  </tr>
                </thead>
                <tbody>
                  {contracts.map((c) => (
                    <tr key={c.contract_node}>
                      <td>
                        <Link href={`/contracts/${encodeURIComponent(c.document_id)}`}>{c.filename ?? c.document_id}</Link>
                      </td>
                      <td>{c.vendor_id ?? "—"}</td>
                      <td>{c.since.slice(0, 10)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="empty-state">No contracts uploaded.</div>
          )
        ) : (
          <div className="empty-state">{contractsError ?? "Loading contracts…"}</div>
        )
      ) : null}

      {notice ? (
        <div className="toast" role="status">
          {notice}
        </div>
      ) : null}
    </section>
  );
}
