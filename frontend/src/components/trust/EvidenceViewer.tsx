"use client";

import { Fragment, useEffect, useState, type ReactNode } from "react";
import { ApiError, objectUrl, type TrustCitation } from "@/lib/api";

type Props = {
  /** The explanation or finding text, rendered on the left. */
  left: ReactNode;
  citations: TrustCitation[];
  selected: number;
  onSelect: (index: number) => void;
};

/**
 * Split view (PRD §8): explanation on the left, the cited source on the right.
 * A citation with a source document shows the page in an iframe at
 * `#page=N`; one without shows the node record. j/k and the arrow keys move
 * between citations.
 */
export function EvidenceViewer({ left, citations, selected, onSelect }: Props) {
  const current = citations[selected] ?? null;
  const documentUrl = current?.source?.document_url ?? null;
  const page = current?.source?.page ?? null;
  // Keyed by the document it was fetched for, so a change of citation never
  // shows the previous citation's page while the next one loads.
  const [loaded, setLoaded] = useState<{
    forUrl: string;
    url: string | null;
    problem: string | null;
  } | null>(null);
  const blobUrl = loaded?.forUrl === documentUrl ? loaded.url : null;
  const problem = loaded?.forUrl === documentUrl ? loaded.problem : null;

  useEffect(() => {
    if (!documentUrl) return;
    let made: string | null = null;
    let cancelled = false;
    objectUrl(documentUrl)
      .then((url) => {
        if (cancelled) return URL.revokeObjectURL(url);
        made = url;
        setLoaded({ forUrl: documentUrl, url, problem: null });
      })
      .catch((caught) =>
        setLoaded({
          forUrl: documentUrl,
          url: null,
          problem: caught instanceof ApiError ? caught.friendly : String(caught),
        }),
      );
    return () => {
      cancelled = true;
      if (made) URL.revokeObjectURL(made);
    };
  }, [documentUrl]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
      if (event.key === "j" || event.key === "ArrowDown") {
        event.preventDefault();
        onSelect(Math.min(selected + 1, citations.length - 1));
      } else if (event.key === "k" || event.key === "ArrowUp") {
        event.preventDefault();
        onSelect(Math.max(selected - 1, 0));
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected, citations.length, onSelect]);

  return (
    <div className="evidence-split">
      <div className="evidence-left">
        {left}
        <ol className="citation-list" aria-label="Citations">
          {citations.map((citation, index) => (
            <li key={citation.node_id + index}>
              <button
                type="button"
                className={index === selected ? "btn-chip is-active" : "btn-chip"}
                onClick={() => onSelect(index)}
                aria-current={index === selected ? "true" : undefined}
              >
                {citation.node?.kind ?? "node"}
                {citation.source?.page ? ` · p.${citation.source.page}` : ""}
              </button>
              {citation.excerpt ? <span className="evidence">{citation.excerpt}</span> : null}
            </li>
          ))}
        </ol>
      </div>
      <div className="evidence-right">
        {problem ? (
          <div className="panel" role="alert" style={{ borderColor: "var(--error)", color: "var(--error)" }}>
            {problem}
          </div>
        ) : blobUrl ? (
          <iframe
            className="doc-frame"
            title="Cited source"
            src={page ? `${blobUrl}#page=${page}` : blobUrl}
          />
        ) : documentUrl ? (
          <p className="empty-state">Loading the source…</p>
        ) : current?.node ? (
          <dl className="kv">
            <dt>Kind</dt>
            <dd>{current.node.kind}</dd>
            <dt>Key</dt>
            <dd>{current.node.natural_key}</dd>
            {Object.entries(current.node.attrs ?? {}).map(([key, value]) => (
              <Fragment key={key}>
                <dt>{key}</dt>
                <dd>{value === null || value === undefined ? "—" : String(value)}</dd>
              </Fragment>
            ))}
          </dl>
        ) : (
          <p className="empty-state">Select a citation to see its evidence.</p>
        )}
      </div>
    </div>
  );
}
