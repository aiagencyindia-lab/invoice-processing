"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Fragment, useEffect, useState } from "react";
import { api, ApiError, can, objectUrl, type ContractClause, type Me } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

// Mirrors CLAUSE_TYPES in api/trust_routes.py.
const CLAUSE_TYPES = ["rate", "escalator", "tier", "rebate", "discount", "term"];

type Draft = { type: string; params: [string, string][] };

function draftOf(clause: ContractClause): Draft {
  return { type: clause.type ?? "", params: Object.entries(clause.params) };
}

export default function ContractPage() {
  const { document_id: documentId } = useParams<{ document_id: string }>();
  const { data: me } = usePoll<Me>("/api/me");
  const { data: clauses, error, reload } = usePoll<ContractClause[]>(
    `/api/trust/contracts/${encodeURIComponent(documentId)}/clauses`,
  );
  const [file, setFile] = useState<{ url: string | null; problem: string | null }>({ url: null, problem: null });
  const [page, setPage] = useState<number | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>({ type: "", params: [] });
  const [notice, setNotice] = useState<string | null>(null);
  const canReview = can(me, "approver");

  useEffect(() => {
    let made: string | null = null;
    let cancelled = false;
    objectUrl(`/api/trust/contracts/${encodeURIComponent(documentId)}/file`)
      .then((url) => {
        if (cancelled) return URL.revokeObjectURL(url);
        made = url;
        setFile({ url, problem: null });
      })
      .catch((caught) => {
        if (cancelled) return;
        setFile({ url: null, problem: caught instanceof ApiError ? caught.friendly : String(caught) });
      });
    return () => {
      cancelled = true;
      if (made) URL.revokeObjectURL(made);
      // The blob is gone; never let a later render show its URL again.
      setFile({ url: null, problem: null });
    };
  }, [documentId]);

  async function save(clause: ContractClause) {
    setNotice(null);
    try {
      await api<ContractClause>(`/api/trust/clauses/${encodeURIComponent(clause.node_id)}`, {
        method: "PATCH",
        body: JSON.stringify({
          type: draft.type || null,
          params: Object.fromEntries(draft.params.filter(([k]) => k.trim()).map(([k, v]) => [k.trim(), v])),
        }),
      });
      // The review re-versions the clause under a new node id: reload rather
      // than patch the row, so the next save targets the open version.
      setEditing(null);
      setNotice("Clause reviewed.");
      reload();
    } catch (caught) {
      setNotice(caught instanceof ApiError ? caught.friendly : String(caught));
    }
  }

  const setParam = (index: number, key: string, value: string) =>
    setDraft({ ...draft, params: draft.params.map((p, i) => (i === index ? [key, value] : p)) });

  return (
    <section className="stack" style={{ gap: 16 }}>
      <Link href="/trust-audit" style={{ fontSize: 12.5 }}>
        ‹ Trust audit
      </Link>
      <h1 style={{ margin: 0 }}>Contract clauses</h1>
      {error ? <div className="empty-state">{error}</div> : null}
      <div className="evidence-split">
        <div className="evidence-left stack" style={{ gap: 10 }}>
          {clauses && clauses.length === 0 ? <div className="empty-state">No clauses were read from this contract.</div> : null}
          {(clauses ?? []).map((clause) => {
            const open = editing === clause.node_id;
            return (
              <div key={clause.node_id} className="panel stack" style={{ padding: 12, gap: 6 }}>
                <div className="row-between">
                  <button
                    type="button"
                    className={page === clause.page && clause.page ? "btn-chip is-active" : "btn-chip"}
                    onClick={() => setPage(clause.page)}
                  >
                    {clause.type ?? "unclassified"}
                    {clause.page ? ` · p.${clause.page}` : ""}
                  </button>
                  <span className="row" style={{ gap: 6, alignItems: "center" }}>
                    {clause.reviewed ? (
                      <span className="badge badge-ok">reviewed</span>
                    ) : (
                      <span className="badge badge-warn">
                        {clause.confidence === null ? "unreviewed" : `${Math.round(clause.confidence * 100)}%`}
                      </span>
                    )}
                    {canReview && !open ? (
                      <button
                        type="button"
                        className="btn-secondary"
                        onClick={() => {
                          setDraft(draftOf(clause));
                          setEditing(clause.node_id);
                        }}
                      >
                        Edit
                      </button>
                    ) : null}
                  </span>
                </div>
                {clause.excerpt ? <span className="evidence">{clause.excerpt}</span> : null}
                {open ? (
                  <div className="stack" style={{ gap: 6 }}>
                    <label className="stack" style={{ gap: 2, fontSize: 12.5 }}>
                      Type
                      <select className="input-field" value={draft.type} onChange={(event) => setDraft({ ...draft, type: event.target.value })}>
                        <option value="">—</option>
                        {CLAUSE_TYPES.map((t) => (
                          <option key={t} value={t}>
                            {t}
                          </option>
                        ))}
                      </select>
                    </label>
                    {draft.params.map(([key, value], index) => (
                      <div key={index} className="row" style={{ gap: 6 }}>
                        <input className="input-field" aria-label="Parameter" value={key} onChange={(event) => setParam(index, event.target.value, value)} />
                        <input className="input-field" aria-label="Value" value={value} onChange={(event) => setParam(index, key, event.target.value)} />
                      </div>
                    ))}
                    <div className="row" style={{ gap: 8 }}>
                      <button type="button" className="btn-secondary" onClick={() => setDraft({ ...draft, params: [...draft.params, ["", ""]] })}>
                        + Parameter
                      </button>
                      <button type="button" className="btn-primary" onClick={() => save(clause)}>
                        Save
                      </button>
                      <button type="button" className="btn-secondary" onClick={() => setEditing(null)}>
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <dl className="kv">
                    {Object.entries(clause.params).map(([key, value]) => (
                      <Fragment key={key}>
                        <dt>{key}</dt>
                        <dd>{value}</dd>
                      </Fragment>
                    ))}
                  </dl>
                )}
              </div>
            );
          })}
        </div>
        <div className="evidence-right">
          {file.problem ? (
            <div className="empty-state">{file.problem}</div>
          ) : file.url ? (
            <iframe className="doc-frame" title="Contract" src={page ? `${file.url}#page=${page}` : file.url} />
          ) : (
            <p className="empty-state">Loading the contract…</p>
          )}
        </div>
      </div>
      {notice ? (
        <div className="toast" role="status">
          {notice}
        </div>
      ) : null}
    </section>
  );
}
