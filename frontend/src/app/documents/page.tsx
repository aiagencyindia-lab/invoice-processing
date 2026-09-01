"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { api, ApiError, can, type DocumentList, type Me } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

const POLL_MS = 5000;

/** Stage colour is never the only signal -- the word is always there too. */
const STAGE_CLASS: Record<string, string> = {
  queued: "badge-neutral",
  processing: "badge-neutral",
  done: "badge-ok",
  failed: "badge-error",
  unknown: "badge-warn",
};

function bytes(size: number | null): string {
  if (size === null || size === undefined) return "—";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function when(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export default function DocumentsPage() {
  const { data, error, reload } = usePoll<DocumentList>(
    "/api/documents",
    POLL_MS,
  );
  const { data: me } = usePoll<Me>("/api/me");

  const fileInput = useRef<HTMLInputElement>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function upload(file: File) {
    setBusy(true);
    setNotice(null);
    const body = new FormData();
    body.append("file", file);
    try {
      await api("/api/documents", { method: "POST", body });
      // The document is queued, not processed -- saying "uploaded" alone is
      // what made people wait for a result that only a worker can produce.
      setNotice(`${file.name} received and queued for processing.`);
      reload();
    } catch (caught) {
      setNotice(
        caught instanceof ApiError ? caught.friendly : String(caught),
      );
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function retry(documentId: string) {
    setNotice(null);
    try {
      const result = await api<{ message: string }>(
        `/api/documents/${documentId}/retry`,
        { method: "POST" },
      );
      setNotice(result.message);
      reload();
    } catch (caught) {
      setNotice(
        caught instanceof ApiError ? caught.friendly : String(caught),
      );
    }
  }

  const documents = data?.documents ?? [];

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="row-between">
        <h1>Documents</h1>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <input
            ref={fileInput}
            type="file"
            accept="application/pdf"
            className="sr-only"
            id="upload"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) upload(file);
            }}
          />
          <label
            htmlFor="upload"
            className="btn-primary"
            style={{ display: "inline-block" }}
            aria-disabled={busy}
          >
            {busy ? "Uploading…" : "Upload a PDF"}
          </label>
        </div>
      </div>

      {notice ? (
        <div className="panel" role="status" style={{ padding: "12px 16px" }}>
          {notice}
        </div>
      ) : null}

      {error ? <div className="empty-state">{error}</div> : null}

      {documents.length === 0 && !error ? (
        <div className="empty-state">
          No documents yet. Upload a PDF above, or email one to your tenant
          address if inbound mail is configured. Uploads are queued first and
          extracted by a worker — the banner at the top of the page will tell
          you if no worker is running.
        </div>
      ) : (
        <div className="panel table-scroll" style={{ padding: 8 }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>File</th>
                <th>Received</th>
                <th>Source</th>
                <th>Size</th>
                <th>Stage</th>
                <th>What that means</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {documents.map((document) => (
                <tr key={document.document_id}>
                  <td>
                    {document.stage === "done" ? (
                      <Link href={`/invoices?case=${document.case_id}`}>
                        {document.filename ?? document.document_id}
                      </Link>
                    ) : (
                      (document.filename ?? document.document_id)
                    )}
                  </td>
                  <td className="faint">{when(document.received_at)}</td>
                  <td className="muted">{document.source ?? "—"}</td>
                  <td className="muted">{bytes(document.byte_size)}</td>
                  <td>
                    <span
                      className={`badge ${STAGE_CLASS[document.stage] ?? "badge-neutral"}`}
                    >
                      {document.stage}
                    </span>
                  </td>
                  <td className="muted">{document.explanation}</td>
                  <td>
                    {/* Retry costs a model call, so the server requires
                        approver. Hiding it below that rank keeps a clerk from
                        clicking into a 403. */}
                    {can(me, "approver") &&
                    document.stage !== "done" &&
                    document.stage !== "processing" ? (
                      <button
                        type="button"
                        className="btn-chip"
                        onClick={() => retry(document.document_id)}
                      >
                        Retry
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
