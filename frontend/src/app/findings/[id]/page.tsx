"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Fragment, useState } from "react";
import { EvidenceRequestDialog } from "@/components/trust/EvidenceRequestDialog";
import { EvidenceViewer } from "@/components/trust/EvidenceViewer";
import { LineageChain } from "@/components/trust/LineageChain";
import { RatingButtons } from "@/components/trust/RatingButtons";
import { TransitionDialog } from "@/components/trust/TransitionDialog";
import {
  api,
  ApiError,
  can,
  downloadFile,
  type EvidenceRequest,
  type EvidenceRequestList,
  type Explanation,
  type Me,
  type TrustFindingDetail,
} from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { DIMENSION_LABELS, ENGINE_LABELS, MONEY_STATUSES, STATUS_LABELS, money, statusBadge, vendorLabel } from "@/lib/trust";

const TABS = ["Why", "Evidence", "Lineage", "History"] as const;
type Tab = (typeof TABS)[number];

/** Statuses from which asking for evidence makes sense: the request route
 *  walks a detected or triaged finding to "needs vendor input" itself, and a
 *  further request may follow while it waits. */
const REQUESTABLE = new Set(["detected", "triaged", "needs_vendor_input"]);

/** The Trust Score dimension a finding counts against. The engine may name it
 *  in the details; otherwise the type family decides (trust/types.py TABLE). */
function dimensionOf(type: string, details: Record<string, unknown>): string | null {
  if (typeof details.dimension === "string") return details.dimension;
  if (type === "REAL-PRICE-JUMP") return "price";
  if (type === "REAL-VOLUME-SPIKE" || type === "PAY-ANOMALY") return "consistency";
  if (type.startsWith("REAL-")) return "reality";
  if (type.startsWith("PROV-")) return "authenticity";
  if (type.startsWith("PAY-") || type.startsWith("GST-")) return "identity";
  if (type.startsWith("LEAK-")) return "price";
  return null;
}

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
  const [requesting, setRequesting] = useState(false);
  // Unavailable (a 503 before the reality engine ships) reads as "none yet":
  // the section only appears once a request exists.
  const { data: requestList, reload: reloadRequests } = usePoll<EvidenceRequestList>(
    `/api/trust/evidence-requests?finding_id=${encodeURIComponent(id)}`,
  );
  const requests = (requestList?.requests ?? []).filter((request) => request.finding_id === id);

  if (error) return <div className="empty-state">{error}</div>;
  if (!detail) return <div className="empty-state">Loading…</div>;

  const transitions = detail.allowed_transitions.filter((to) =>
    can(me, MONEY_STATUSES.has(to) ? "approver" : "clerk"),
  );

  async function attachProof(request: EvidenceRequest, file: File) {
    setNotice(null);
    const form = new FormData();
    form.append("file", file);
    try {
      await api(`/api/trust/evidence-requests/${encodeURIComponent(request.id)}/proof`, {
        method: "POST",
        body: form,
      });
      setNotice(`Attached ${file.name}; the finding will be re-checked.`);
      reloadRequests();
      reload();
    } catch (caught) {
      setNotice(caught instanceof ApiError ? caught.friendly : String(caught));
    }
  }

  const dimension = dimensionOf(detail.type, detail.details);

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
            {dimension ? (
              <span className="badge badge-neutral" title="Trust Score dimension">
                {DIMENSION_LABELS[dimension] ?? dimension}
              </span>
            ) : null}
            {requests.some((request) => request.status === "open" && request.overdue) ? (
              <span className="badge badge-error">evidence overdue</span>
            ) : null}
            <span className="muted" style={{ fontSize: 12.5 }}>
              {vendorLabel(detail.vendor, "Unknown vendor")} · {detail.engine ? (ENGINE_LABELS[detail.engine] ?? detail.engine) : "—"}
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
          {REQUESTABLE.has(detail.status) && can(me, "clerk") ? (
            <button type="button" className="btn-secondary" onClick={() => setRequesting(true)}>
              Request evidence
            </button>
          ) : null}
          <button type="button" className="btn-secondary" onClick={() => pack("html")}>
            Evidence pack (HTML)
          </button>
          <button type="button" className="btn-secondary" onClick={() => pack("json")}>
            Evidence pack (JSON)
          </button>
        </div>
      </header>

      {requests.length ? (
        <div className="panel table-scroll" style={{ padding: 8 }} role="region" aria-label="Evidence requests">
          <table className="data-table">
            <thead>
              <tr>
                <th>Owner</th>
                <th>Due</th>
                <th>Status</th>
                <th>Note</th>
                <th>Proof</th>
              </tr>
            </thead>
            <tbody>
              {requests.map((request) => (
                <tr key={request.id}>
                  <td>{request.owner}</td>
                  <td>
                    {request.due_date ?? "—"}
                    {request.status === "open" && request.overdue ? (
                      <span className="badge badge-error" style={{ marginLeft: 6 }}>
                        overdue
                      </span>
                    ) : null}
                  </td>
                  <td>{request.status}</td>
                  <td>{request.note ?? "—"}</td>
                  <td>
                    {request.status === "open" && can(me, "clerk") ? (
                      <input
                        type="file"
                        aria-label={`Attach proof for ${request.owner}`}
                        onChange={(event) => {
                          const file = event.target.files?.[0];
                          if (file) void attachProof(request, file);
                          event.target.value = "";
                        }}
                      />
                    ) : request.proof_document_id ? (
                      "attached"
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

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
            <div>
              <div className="eyebrow">Evidence</div>
              <ul className="citation-list" aria-label="Explanation evidence">
                {explanation.evidence.map((item, index) => (
                  <li key={item.node_id + index}>
                    <strong style={{ fontSize: 13 }}>{item.label}</strong>
                    {item.excerpt ? <span className="evidence">{item.excerpt}</span> : null}
                  </li>
                ))}
              </ul>
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

      {requesting ? (
        <EvidenceRequestDialog
          finding={detail}
          onCancel={() => setRequesting(false)}
          onDone={(request) => {
            setRequesting(false);
            setNotice(`Evidence requested from ${request.owner}.`);
            reloadRequests();
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
