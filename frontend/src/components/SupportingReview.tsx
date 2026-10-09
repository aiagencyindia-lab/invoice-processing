"use client";

import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type Evidence,
  type ImportResult,
  type PurchaseOrderDetail,
  type SupportingDetail,
} from "@/lib/api";

type Cell = string | number | null;

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

const LINE_COLUMNS: Record<string, [string, string][]> = {
  goods_receipt: [
    ["line_number", "Line"],
    ["item_code", "Item"],
    ["description", "Description"],
    ["quantity_received", "Quantity received"],
  ],
  purchase_order: [
    ["line_number", "Line"],
    ["item_code", "Item"],
    ["description", "Description"],
    ["quantity", "Quantity"],
    ["unit", "Unit"],
    ["unit_price", "Unit price"],
  ],
};
const NUMERIC = new Set(["line_number", "quantity_received", "quantity", "unit_price"]);

const label = (key: string) =>
  key === "grn_number"
    ? "GRN number"
    : key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

/** Typed text back to what the API takes: numbers where the column is numeric,
 *  and text left as typed when it is not one, so the server names the problem. */
function typed(key: string, text: string): Cell {
  if (text.trim() === "") return null;
  if (!NUMERIC.has(key)) return text;
  const n = Number(text);
  return Number.isNaN(n) ? text : n;
}

export default function SupportingReview({
  id,
  onClose,
  onDone,
}: {
  id: string;
  onClose: () => void;
  /** Called after a successful accept or reject, with the notice to show. */
  onDone: (notice: string) => void;
}) {
  const [detail, setDetail] = useState<SupportingDetail | null>(null);
  const [fields, setFields] = useState<Record<string, Cell>>({});
  const [lines, setLines] = useState<Record<string, Cell>[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [poLines, setPoLines] = useState<number[]>([]);

  useEffect(() => {
    api<SupportingDetail>(`/api/supporting-documents/${id}`)
      .then((d) => {
        setDetail(d);
        setFields({ ...(d.extracted?.fields ?? {}) });
        if (d.kind === "goods_receipt" && d.po_on_file && d.po_number) {
          api<PurchaseOrderDetail>(
            `/api/purchase-orders/${encodeURIComponent(d.po_number)}`,
          )
            .then((po) => setPoLines(po.lines.map((l) => l.line_number)))
            .catch(() => {}); // the pre-filled pairing still works without the list
        }
        // A receipt line starts on the PO line it paired to; null shows "no match".
        setLines(
          (d.extracted?.lines ?? []).map((ln, i) =>
            d.kind === "goods_receipt"
              ? { ...ln, po_line_number: d.paired?.[i] ?? null }
              : { ...ln },
          ),
        );
      })
      .catch((caught) =>
        setError(caught instanceof ApiError ? caught.friendly : String(caught)),
      );
  }, [id]);

  async function decide(path: "accept" | "reject") {
    if (!detail) return;
    setBusy(true);
    setError(null);
    try {
      if (path === "reject") {
        await api(`/api/supporting-documents/${id}/reject`, {
          method: "POST",
          body: JSON.stringify({ reason: reason.trim() }),
        });
        onDone("Rejected the scanned document");
        return;
      }
      const result = await api<ImportResult & { po_number?: string }>(
        `/api/supporting-documents/${id}/accept`,
        { method: "POST", body: JSON.stringify({ fields, lines }) },
      );
      onDone(
        detail.kind === "goods_receipt"
          ? `Recorded ${plural(result.recorded ?? 0, "receipt")}` +
              ((result.rematch_queued ?? 0) > 0
                ? `; ${plural(result.rematch_queued ?? 0, "invoice")} queued for re-match`
                : "")
          : `Saved purchase order ${fields.po_number ?? ""}`.trim(),
      );
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.friendly : String(caught));
    } finally {
      setBusy(false);
    }
  }

  const evidence = (detail?.extracted?._evidence ?? {}) as Record<string, unknown>;
  const summary = evidence._summary as
    | { fields_needing_review?: string[] }
    | undefined;
  const flagged = new Set(summary?.fields_needing_review ?? []);
  const keys = Object.keys(fields).sort(
    (a, b) => Number(flagged.has(b)) - Number(flagged.has(a)),
  );
  const isGrn = detail?.kind === "goods_receipt";
  const blocked = detail?.status === "blocked";
  const columns = detail ? LINE_COLUMNS[detail.kind] : [];
  const setLine = (i: number, key: string, value: Cell) =>
    setLines((all) => all.map((ln, j) => (j === i ? { ...ln, [key]: value } : ln)));

  return (
    <aside
      className="drawer"
      role="dialog"
      aria-label="Review scanned document"
      style={{ width: "min(1100px, 100vw)" }}
    >
      <div className="drawer-head">
        <h2>{isGrn ? "Goods receipt" : "Purchase order"} from a scan</h2>
        <button type="button" className="btn-chip" onClick={onClose}>
          Close
        </button>
      </div>
      {error ? (
        <div role="alert" style={{ color: "var(--error)", marginBottom: 8 }}>
          {error}
        </div>
      ) : null}
      {!detail ? (
        <div className="muted">{error ? "" : "Loading…"}</div>
      ) : (
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
          <iframe
            title="Scanned document"
            src={detail.document_url}
            style={{ flex: "1 1 360px", minHeight: "70vh", border: "1px solid var(--border)" }}
          />
          <div className="stack" style={{ flex: "1 1 420px", gap: 12 }}>
            {blocked ? (
              <div role="status" className="panel" style={{ padding: 10 }}>
                {detail.blocked_reason ?? "Its purchase order is not on file."}
              </div>
            ) : null}
            <div>
              {keys.map((key) => {
                const ev = evidence[key] as Evidence | undefined;
                const check = flagged.has(key);
                return (
                  <div key={key} className={`field${check ? " flagged" : ""}`}>
                    <label htmlFor={`f-${key}`}>{label(key)}</label>
                    <input
                      id={`f-${key}`}
                      value={fields[key] ?? ""}
                      onChange={(e) =>
                        setFields({ ...fields, [key]: typed(key, e.target.value) })
                      }
                    />
                    {check ? (
                      <div className="why">
                        check this
                        {typeof ev?.confidence === "number"
                          ? ` · confidence ${Math.round(ev.confidence * 100)}%`
                          : ""}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    {columns.map(([, name]) => (
                      <th key={name}>{name}</th>
                    ))}
                    {isGrn ? <th>PO line</th> : null}
                  </tr>
                </thead>
                <tbody>
                  {lines.map((ln, i) => (
                    <tr key={i}>
                      {columns.map(([key, name]) => (
                        <td key={key}>
                          <input
                            aria-label={`${name}, line ${i + 1}`}
                            value={ln[key] ?? ""}
                            onChange={(e) => setLine(i, key, typed(key, e.target.value))}
                            style={{ width: "100%", minWidth: 60 }}
                          />
                        </td>
                      ))}
                      {isGrn ? (
                        <td>
                          <select
                            aria-label={`PO line for line ${i + 1}`}
                            value={ln.po_line_number ?? ""}
                            onChange={(e) =>
                              setLine(i, "po_line_number", typed("line_number", e.target.value))
                            }
                          >
                            <option value="">no match</option>
                            {poLines.map((n) => (
                              <option key={n} value={n}>
                                {n}
                              </option>
                            ))}
                          </select>
                        </td>
                      ) : null}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {rejecting ? (
              <div className="stack" style={{ gap: 6 }}>
                <label htmlFor="reject-reason">Reason for rejecting</label>
                <input
                  id="reject-reason"
                  value={reason}
                  maxLength={500}
                  onChange={(e) => setReason(e.target.value)}
                />
                <div style={{ display: "flex", gap: 8 }}>
                  <button
                    type="button"
                    className="btn-primary"
                    disabled={busy || !reason.trim()}
                    onClick={() => decide("reject")}
                  >
                    Confirm reject
                  </button>
                  <button type="button" className="btn-chip" onClick={() => setRejecting(false)}>
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  type="button"
                  className="btn-primary"
                  disabled={busy || blocked}
                  onClick={() => decide("accept")}
                >
                  Accept
                </button>
                <button
                  type="button"
                  className="btn-chip"
                  disabled={busy}
                  onClick={() => setRejecting(true)}
                >
                  Reject
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </aside>
  );
}
