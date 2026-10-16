"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Fragment, useState } from "react";
import { EvidenceViewer } from "@/components/trust/EvidenceViewer";
import { LineageChain } from "@/components/trust/LineageChain";
import { RatingButtons } from "@/components/trust/RatingButtons";
import { TransitionDialog } from "@/components/trust/TransitionDialog";
import {
  ApiError,
  can,
  downloadFile,
  type Explanation,
  type Me,
  type TrustFindingDetail,
} from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { ENGINE_LABELS, MONEY_STATUSES, STATUS_LABELS, money, statusBadge } from "@/lib/trust";

const TABS = ["Why", "Evidence", "Lineage", "History"] as const;
type Tab = (typeof TABS)[number];

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

export default function FindingDetailPage() {
  const { id } = useParams<{ id: string }>();
  const path = `/api/trust/findings/${encodeURIComponent(id)}`;
  const { data: detail, error, reload } = usePoll<TrustFindingDetail>(path);
  const { data: me } = usePoll<Me>("/api/me");
  // The Why engine answers 404 until its branch lands; the tab says so.
  const { data: explanation, error: explainError } = usePoll<Explanation>(`${path}/explanation`);
  const [tab, setTab] = useState<Tab>("Why");
  const [citation, setCitation] = useState(0);
  const [moving, setMoving] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  if (error) return <div className="empty-state">{error}</div>;
  if (!detail) return <div className="empty-state">Loading…</div>;

  const transitions = detail.allowed_transitions.filter((to) =>
    can(me, MONEY_STATUSES.has(to) ? "approver" : "clerk"),
  );

  async function pack(format: "html" | "json") {
    setNotice(null);
    try {
      await downloadFile(`${path}/evidence-pack?format=${format}`, `finding-${id}.${format}`);
    } catch (caught) {
      setNotice(caught instanceof ApiError ? caught.friendly : String(caught));
    }
  }

  const left = (
    <div className="stack" style={{ gap: 8 }}>
      <strong>{detail.summary ?? detail.type}</strong>
      {explanation ? (
        <ol style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
          {explanation.why.map((step, index) => (
            <li key={index}>{step.text}</li>
          ))}
        </ol>
      ) : (
        <dl className="kv">
          {Object.entries(detail.details).map(([key, value]) => (
            <Fragment key={key}>
              <dt>{key}</dt>
              <dd>{value === null || value === undefined ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value)}</dd>
            </Fragment>
          ))}
        </dl>
      )}
    </div>
  );

  return (
    <section className="stack" style={{ gap: 16 }}>
      <Link href="/findings" style={{ fontSize: 12.5 }}>
        ‹ Findings
      </Link>
      <header className="row-between" style={{ alignItems: "start", gap: 12, flexWrap: "wrap" }}>
        <div className="stack" style={{ gap: 4 }}>
          <h1 style={{ margin: 0 }}>
            {detail.type} · {money(detail.impact, detail.currency)}
          </h1>
          <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span className={`badge ${statusBadge(detail.status)}`}>{STATUS_LABELS[detail.status] ?? detail.status}</span>
            {detail.hold ? <span className="badge badge-warn">hold</span> : null}
            <span className="muted" style={{ fontSize: 12.5 }}>
              {detail.vendor?.name ?? "Unknown vendor"} · {detail.engine ? (ENGINE_LABELS[detail.engine] ?? detail.engine) : "—"}
              {detail.confidence !== null ? ` · ${Math.round(detail.confidence * 100)}% confidence` : ""}
            </span>
          </div>
          <p className="muted" style={{ margin: 0 }}>
            {detail.summary}
          </p>
        </div>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          {transitions.map((to) => (
            <button key={to} type="button" className="btn-primary" onClick={() => setMoving(to)}>
              {STATUS_LABELS[to] ?? to}
            </button>
          ))}
          <button type="button" className="btn-secondary" onClick={() => pack("html")}>
            Evidence pack (HTML)
          </button>
          <button type="button" className="btn-secondary" onClick={() => pack("json")}>
            Evidence pack (JSON)
          </button>
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

      {tab === "Why" ? (
        explanation ? (
          <div className="panel stack" style={{ padding: 16, gap: 12 }}>
            <div>
              <div className="eyebrow">What happened</div>
              <p style={{ margin: "4px 0 0" }}>{explanation.what_happened}</p>
            </div>
            <div>
              <div className="eyebrow">Why</div>
              <ol style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                {explanation.why.map((step, index) => (
                  <li key={index}>{step.text}</li>
                ))}
              </ol>
            </div>
            <div>
              <div className="eyebrow">Caused by</div>
              <div className="row" style={{ gap: 6, flexWrap: "wrap", marginTop: 4 }}>
                {explanation.caused_by.map((actor, index) => (
                  <span key={index} className="badge badge-neutral">
                    {actor.kind}: {actor.label}
                  </span>
                ))}
              </div>
            </div>
            {explanation.counterfactual ? (
              <dl className="kv">
                <dt>Payable now</dt>
                <dd>{money(explanation.counterfactual.payable_now, explanation.counterfactual.currency)}</dd>
                <dt>Payable if correct</dt>
                <dd>{money(explanation.counterfactual.payable_correct, explanation.counterfactual.currency)}</dd>
                <dt>Delta</dt>
                <dd>{money(explanation.counterfactual.delta, explanation.counterfactual.currency)}</dd>
                <dt>Formula</dt>
                <dd>{explanation.counterfactual.formula}</dd>
                {explanation.counterfactual.note ? (
                  <>
                    <dt>Note</dt>
                    <dd>{explanation.counterfactual.note}</dd>
                  </>
                ) : null}
              </dl>
            ) : null}
            <div>
              <div className="eyebrow">Next action</div>
              <p style={{ margin: "4px 0 0" }}>{explanation.next_action}</p>
            </div>
            {explanation.would_change.length ? (
              <div>
                <div className="eyebrow">What would change this</div>
                <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                  {explanation.would_change.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
              {explanation.confidence_note} · {explanation.version}
            </p>
            {can(me, "clerk") ? <RatingButtons findingId={id} /> : null}
          </div>
        ) : (
          <div className="empty-state">{explainError ?? "Loading the explanation…"}</div>
        )
      ) : null}

      {tab === "Evidence" ? (
        <EvidenceViewer left={left} citations={detail.citations} selected={citation} onSelect={setCitation} />
      ) : null}

      {tab === "Lineage" ? <LineageChain links={detail.lineage} /> : null}

      {tab === "History" ? (
        detail.events.length ? (
          <div className="panel table-scroll" style={{ padding: 8 }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Who</th>
                  <th>From</th>
                  <th>To</th>
                  <th>Amount</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {detail.events.map((event, index) => (
                  <tr key={index}>
                    <td>{when(event.at)}</td>
                    <td>{event.actor}</td>
                    <td>{event.from_status ? (STATUS_LABELS[event.from_status] ?? event.from_status) : "—"}</td>
                    <td>{STATUS_LABELS[event.to_status] ?? event.to_status}</td>
                    <td>{money(event.amount, detail.currency)}</td>
                    <td>{event.reason ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-state">Nobody has moved this finding yet.</div>
        )
      ) : null}

      {moving ? (
        <TransitionDialog
          finding={detail}
          toStatus={moving}
          onCancel={() => setMoving(null)}
          onDone={() => {
            setMoving(null);
            setNotice(`Marked ${STATUS_LABELS[moving] ?? moving}.`);
            reload();
          }}
          onMoved={(current) => {
            setMoving(null);
            setNotice(`This finding moved to ${STATUS_LABELS[current] ?? current}; reloaded.`);
            reload();
          }}
        />
      ) : null}

      {notice ? (
        <div className="toast" role="status">
          {notice}
        </div>
      ) : null}
    </section>
  );
}
