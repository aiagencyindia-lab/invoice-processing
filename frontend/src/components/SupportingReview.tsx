"use client";

import { useEffect, useRef, useState } from "react";
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
const NUMERIC = new Set([
  "line_number",
  "quantity_received",
  "quantity",
  "unit_price",
  "total_amount",
]);

const LABELS: Record<string, string> = {
  grn_number: "GRN number",
  // Required by the import only when a quantity is negative.
  reason: "Correction reason",
};
const label = (key: string) =>
  LABELS[key] ?? key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

const text = (v: Cell | undefined) => (v === null || v === undefined ? "" : String(v));
const rawLine = (ln: Record<string, Cell>): Record<string, string> =>
  Object.fromEntries(Object.entries(ln).map(([k, v]) => [k, text(v)]));

/** Raw typed text to what the API takes, only when sending: blank is null,
 *  a numeric field or column becomes a number, and anything that is not a finite
 *  number goes as typed so the server names the problem. */
function typed(key: string, raw: string): Cell {
  if (raw.trim() === "") return null;
  if (!NUMERIC.has(key)) return raw;
  const n = Number(raw);
  return Number.isFinite(n) ? n : raw;
}

/** The two decisions with no form of their own, confirmed inline as
 *  "Confirm reject" is: what each will do, and its confirm button. */
const CONFIRM = {
  reopen: {
    what: "Reopening puts it back to awaiting confirmation, to be accepted or rejected again.",
    button: "Confirm reopen",
  },
  "process-as-invoice": {
    what: "This closes the review and runs the document through the invoice pipeline; it does not come back here.",
    button: "Confirm process as invoice",
  },
} as const;
type Confirmable = keyof typeof CONFIRM;

const confidenceOf = (ev: unknown) => {
  const c = (ev as Evidence | undefined)?.confidence;
  return typeof c === "number" ? ` · confidence ${Math.round(c * 100)}%` : "";
};

const convert = (rec: Record<string, string>): Record<string, Cell> =>
  Object.fromEntries(Object.entries(rec).map(([k, v]) => [k, typed(k, v)]));

export default function SupportingReview({
  id,
  admin,
  onClose,
  onDone,
}: {
  id: string;
  /** May reopen an acceptance that stored nothing. */
  admin: boolean;
  onClose: () => void;
  /** Called after a successful decision, with the notice to show. */
  onDone: (notice: string) => void;
}) {
  const [detail, setDetail] = useState<SupportingDetail | null>(null);
  const [fields, setFields] = useState<Record<string, string>>({});
  const [lines, setLines] = useState<Record<string, string>[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [confirming, setConfirming] = useState<Confirmable | null>(null);
  const [reason, setReason] = useState("");
  const [poLines, setPoLines] = useState<number[] | null>(null);
  const [poLinesFailed, setPoLinesFailed] = useState(false);
  // The PO number was edited: the scan's pairing no longer applies.
  const [rechosen, setRechosen] = useState(false);
  const drawer = useRef<HTMLElement>(null);

  useEffect(() => drawer.current?.focus(), []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    api<SupportingDetail>(`/api/supporting-documents/${id}`)
      .then((d) => {
        setDetail(d);
        const read = rawLine(d.extracted?.fields ?? {});
        setFields(d.kind === "goods_receipt" ? { ...read, reason: read.reason ?? "" } : read);
        if (d.kind === "goods_receipt" && d.po_on_file && d.po_number) {
          api<PurchaseOrderDetail>(
            `/api/purchase-orders/${encodeURIComponent(d.po_number)}`,
          )
            .then((po) => setPoLines(po.lines.map((l) => l.line_number)))
            .catch(() => setPoLinesFailed(true));
        }
        // A receipt line starts on the PO line it paired to; null shows "no match".
        setLines(
          (d.extracted?.lines ?? []).map((ln, i) =>
            d.kind === "goods_receipt"
              ? { ...rawLine(ln), po_line_number: text(d.paired?.[i]) }
              : rawLine(ln),
          ),
        );
      })
      .catch((caught) =>
        setError(caught instanceof ApiError ? caught.friendly : String(caught)),
      );
  }, [id]);

  const poNumber = fields.po_number?.trim() ?? "";
  useEffect(() => {
    if (!rechosen || !poNumber) return;
    let live = true;
    const t = setTimeout(() => {
      api<PurchaseOrderDetail>(`/api/purchase-orders/${encodeURIComponent(poNumber)}`)
        .then((po) => live && setPoLines(po.lines.map((l) => l.line_number)))
        .catch(() => live && setPoLines([])); // not on file: nothing to choose
    }, 300);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [rechosen, poNumber]);

  async function decide(path: "accept" | "reject" | Confirmable) {
    if (!detail) return;
    setBusy(true);
    setError(null);
    try {
      if (path !== "accept") {
        await api(`/api/supporting-documents/${id}/${path}`, {
          method: "POST",
          body: path === "reject" ? JSON.stringify({ reason: reason.trim() }) : undefined,
        });
        onDone(
          {
            reject: "Rejected the scanned document",
            reopen: "Reopened the scanned document; it awaits confirmation again",
            "process-as-invoice": "Sent the document to be processed as an invoice",
          }[path],
        );
        return;
      }
      const result = await api<ImportResult & { po_number?: string }>(
        `/api/supporting-documents/${id}/accept`,
        {
          method: "POST",
          body: JSON.stringify({
            // The reason goes only with a correction; otherwise it is null.
            fields: convert(correcting ? fields : { ...fields, reason: "" }),
            lines: lines.map(convert),
            lines_rechosen: rechosen,
          }),
        },
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
  const undecided = detail?.status === "pending" || blocked;
  const reopenable = admin && detail?.status === "accepted" && detail.stored === false;
  const columns = detail ? LINE_COLUMNS[detail.kind] : [];
  // A negative received quantity is a correction, which needs its reason.
  const correcting = isGrn && lines.some((ln) => Number(ln.quantity_received) < 0);
  const shownKeys = keys.filter((key) => key !== "reason" || correcting);
  const setLine = (i: number, key: string, value: string) =>
    setLines((all) => all.map((ln, j) => (j === i ? { ...ln, [key]: value } : ln)));

  return (
    <aside
      className="drawer"
      role="dialog"
      aria-label="Review scanned document"
      ref={drawer}
      tabIndex={-1}
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
                {detail.blocked_reason ?? "Its purchase order is not on file."}{" "}
                Correct the PO number to one on file to accept.
              </div>
            ) : null}
            <div>
              {shownKeys.map((key) => {
                const ev = evidence[key] as Evidence | undefined;
                const check = flagged.has(key);
                return (
                  <div key={key} className={`field${check ? " flagged" : ""}`}>
                    <label htmlFor={`f-${key}`}>{label(key)}</label>
                    <input
                      id={`f-${key}`}
                      aria-describedby={check ? `why-f-${key}` : undefined}
                      value={fields[key] ?? ""}
                      onChange={(e) => {
                        setFields({ ...fields, [key]: e.target.value });
                        if (isGrn && key === "po_number") {
                          // Paired against the old PO: the clerk picks again.
                          setRechosen(true);
                          setPoLines(null);
                          setPoLinesFailed(false);
                          setLines((all) => all.map((ln) => ({ ...ln, po_line_number: "" })));
                        }
                      }}
                    />
                    {check ? (
                      <div className="why" id={`why-f-${key}`}>
                        check this
                        {confidenceOf(ev)}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
            {poLinesFailed ? (
              <div className="muted" role="status" style={{ fontSize: 12.5 }}>
                The purchase order&apos;s lines could not be loaded, so only the
                paired PO line can be chosen.
              </div>
            ) : null}
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
                      {columns.map(([key, name]) => {
                        const evKey = `lines[${i}].${key}`;
                        const check = flagged.has(evKey);
                        const input = (
                          <input
                            aria-label={`${name}, line ${i + 1}`}
                            aria-describedby={check ? `why-${evKey}` : undefined}
                            value={ln[key] ?? ""}
                            onChange={(e) => setLine(i, key, e.target.value)}
                            style={{ width: "100%", minWidth: 60 }}
                          />
                        );
                        return (
                          <td key={key}>
                            {check ? (
                              // The same bar and hint as a flagged header field.
                              <div
                                className="field flagged"
                                style={{ margin: 0, padding: "0 0 0 8px", borderBottom: "none" }}
                              >
                                {input}
                                <div className="why" id={`why-${evKey}`}>
                                  check this
                                  {confidenceOf(evidence[evKey])}
                                </div>
                              </div>
                            ) : (
                              input
                            )}
                          </td>
                        );
                      })}
                      {isGrn ? (
                        <td>
                          <select
                            aria-label={`PO line for line ${i + 1}`}
                            value={ln.po_line_number ?? ""}
                            onChange={(e) =>
                              setLine(i, "po_line_number", e.target.value)
                            }
                          >
                            <option value="">{rechosen ? "choose" : "no match"}</option>
                            {[
                              ...(poLines ?? []),
                              ...(ln.po_line_number &&
                              !(poLines ?? []).includes(Number(ln.po_line_number))
                                ? [Number(ln.po_line_number)]
                                : []),
                            ].map((n) => (
                              <option key={n} value={n}>
                                {poLines ? n : `PO line ${n}`}
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
            {confirming ? (
              <div className="stack" style={{ gap: 6 }}>
                <div id="confirm-what">{CONFIRM[confirming].what}</div>
                <div style={{ display: "flex", gap: 8 }}>
                  <button
                    type="button"
                    className="btn-primary"
                    aria-describedby="confirm-what"
                    disabled={busy}
                    onClick={() => decide(confirming)}
                  >
                    {CONFIRM[confirming].button}
                  </button>
                  <button type="button" className="btn-chip" onClick={() => setConfirming(null)}>
                    Cancel
                  </button>
                </div>
              </div>
            ) : reopenable ? (
              <div className="stack" style={{ gap: 6 }}>
                <div role="status" className="muted">
                  Accepted, but nothing was stored from it.
                </div>
                <div>
                  <button
                    type="button"
                    className="btn-primary"
                    disabled={busy}
                    onClick={() => setConfirming("reopen")}
                  >
                    Reopen
                  </button>
                </div>
              </div>
            ) : !undecided ? null : rejecting ? (
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
                  disabled={busy}
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
                {/* The classifier was wrong: this is an invoice. */}
                <button
                  type="button"
                  className="btn-chip"
                  disabled={busy}
                  onClick={() => setConfirming("process-as-invoice")}
                >
                  Process as invoice
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </aside>
  );
}
