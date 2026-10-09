"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  can,
  type ImportResult,
  type Me,
  type PurchaseOrderDetail,
  type PurchaseOrderList,
} from "@/lib/api";
import { usePoll } from "@/lib/hooks";

function amount(value: number | null, currency: string | null): string {
  if (value === null || value === undefined) return "—";
  return `${currency ?? ""} ${value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
  })}`.trim();
}

const PO_TEMPLATE =
  "po_number,vendor_name,total_amount,currency,status,amount_invoiced,customer_name,valid_from,valid_to,amount_basis,line_number,item_code,description,qty_ordered,unit_price,uom,receipt_required,qty_invoiced\n";
const GRN_TEMPLATE =
  "grn_number,po_number,received_on,reason,line_number,item_code,qty_received\n";

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

function TemplateLink({ name, csv }: { name: string; csv: string }) {
  // Built client-side: a header row is not worth a round trip.
  return (
    <a
      href={`data:text/csv;charset=utf-8,${encodeURIComponent(csv)}`}
      download={name}
      className="muted"
      style={{ fontSize: 12.5 }}
    >
      Download template
    </a>
  );
}

function PoDetailRows({ poNumber }: { poNumber: string }) {
  const [detail, setDetail] = useState<PurchaseOrderDetail | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    api<PurchaseOrderDetail>(
      `/api/purchase-orders/${encodeURIComponent(poNumber)}`,
    )
      .then(setDetail)
      .catch((caught) =>
        setProblem(caught instanceof ApiError ? caught.friendly : String(caught)),
      );
  }, [poNumber]);
  if (problem) return <div style={{ color: "var(--error)" }}>{problem}</div>;
  if (!detail) return <div className="muted">Loading…</div>;
  return (
    <div className="stack" style={{ gap: 8 }}>
      {detail.lines.length === 0 ? (
        <div className="muted">This order has no lines.</div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Line</th>
              <th>Item</th>
              <th>Description</th>
              <th>Ordered</th>
              <th>Received</th>
              <th>Billed</th>
              <th>Remaining</th>
              <th>Receipt?</th>
            </tr>
          </thead>
          <tbody>
            {detail.lines.map((ln) => (
              <tr key={ln.line_number}>
                <td>{ln.line_number}</td>
                <td>{ln.item_code ?? "—"}</td>
                <td>{ln.description ?? "—"}</td>
                <td>{ln.qty_ordered}</td>
                <td>{ln.received}</td>
                <td>{ln.billed}</td>
                <td>{ln.remaining}</td>
                <td>{ln.receipt_required ? "Yes" : "No"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="eyebrow">Goods receipts</div>
      {detail.receipts.length === 0 ? (
        <div className="muted">No receipts recorded.</div>
      ) : (
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          {detail.receipts.map((g) => (
            <li key={g.grn_number}>
              {g.grn_number} · {g.received_on}
              {g.reason ? ` · ${g.reason}` : ""} ·{" "}
              {g.lines.map((l) => `line ${l.line_number}: ${l.qty_received}`).join(", ")}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function PurchaseOrdersPage() {
  const { data, error, reload } = usePoll<PurchaseOrderList>(
    "/api/purchase-orders",
  );
  const { data: me } = usePoll<Me>("/api/me");
  const fileInput = useRef<HTMLInputElement>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const [expanded, setExpanded] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const canReplace = can(me, "admin");
  const canImport = can(me, "clerk");
  const orders = data?.purchase_orders ?? [];

  async function importFile(
    path: string,
    file: File,
    describe: (r: ImportResult) => string,
  ) {
    setNotice(null);
    setProblem(null);
    setImporting(true);
    const body = new FormData();
    body.append("file", file);
    try {
      const result = await api<ImportResult>(path, { method: "POST", body });
      setNotice(describe(result));
      reload();
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    } finally {
      setImporting(false);
    }
  }

  async function replace(file: File) {
    setNotice(null);
    setProblem(null);
    let records: unknown;
    try {
      const parsed = JSON.parse(await file.text());
      // Accept the bare array or the wrapped object: an ERP export is whatever
      // the ERP emits, and rejecting one of the two shapes over a wrapper key
      // is a pointless thing to make someone debug.
      records = Array.isArray(parsed) ? parsed : parsed?.purchase_orders;
    } catch {
      setProblem(`${file.name} is not valid JSON.`);
      return;
    }
    if (!Array.isArray(records)) {
      setProblem(
        "Expected a list of purchase orders, or an object with a purchase_orders list.",
      );
      return;
    }

    // The only destructive control in the application: this discards every
    // order currently loaded. A file picked by mistake would silently stop
    // every PO matching, and the previous set is not recoverable from here.
    const replacing = data?.count ?? 0;
    const confirmed = window.confirm(
      replacing
        ? `Replace all ${replacing} purchase orders with the ${records.length} in ${file.name}?\n\n` +
            "This replaces rather than merges, so any order not in the file stops matching."
        : `Load ${records.length} purchase orders from ${file.name}?`,
    );
    if (!confirmed) {
      if (fileInput.current) fileInput.current.value = "";
      return;
    }

    try {
      const result = await api<{ count: number }>("/api/purchase-orders", {
        method: "PUT",
        body: JSON.stringify({ purchase_orders: records }),
      });
      // Replace, not merge -- say so, because the difference is whether orders
      // the customer's ERP has closed are still matchable.
      setNotice(
        `Loaded ${result.count} purchase orders. This replaced the previous set.`,
      );
      reload();
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    } finally {
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="row-between">
        <h1>Purchase orders</h1>
        {canReplace ? (
          <>
            <input
              ref={fileInput}
              id="po-upload"
              type="file"
              accept="application/json,.json"
              className="sr-only"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) replace(file);
              }}
            />
            <label htmlFor="po-upload" className="btn-primary">
              Replace from JSON
            </label>
          </>
        ) : null}
      </div>

      {canImport ? (
        <div className="panel row-between" style={{ padding: "12px 16px", flexWrap: "wrap", gap: 12 }}>
          {[
            {
              id: "po-import",
              label: "Import purchase orders (CSV/JSON)",
              path: "/api/purchase-orders/import",
              template: <TemplateLink name="purchase-orders-template.csv" csv={PO_TEMPLATE} />,
              describe: (r: ImportResult) =>
                `Imported ${plural((r.created ?? 0) + (r.updated ?? 0), "purchase order")}`,
            },
            {
              id: "grn-import",
              label: "Import goods receipts (CSV/JSON)",
              path: "/api/goods-receipts/import",
              template: <TemplateLink name="goods-receipts-template.csv" csv={GRN_TEMPLATE} />,
              describe: (r: ImportResult) =>
                `Recorded ${plural(r.recorded ?? 0, "receipt")}` +
                ((r.rematch_queued ?? 0) > 0
                  ? `; ${plural(r.rematch_queued ?? 0, "invoice")} queued for re-match`
                  : ""),
            },
          ].map((c) => (
            <div key={c.id} className="stack" style={{ gap: 4 }}>
              <input
                id={c.id}
                type="file"
                accept=".csv,.json,text/csv,application/json"
                className="sr-only"
                disabled={importing}
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) importFile(c.path, file, c.describe);
                  event.target.value = "";
                }}
              />
              <label htmlFor={c.id} className="btn-primary">
                {c.label}
              </label>
              {c.template}
            </div>
          ))}
        </div>
      ) : null}

      {problem ? (
        <div
          className="panel"
          role="alert"
          style={{ padding: "12px 16px", borderColor: "var(--error)", color: "var(--error)" }}
        >
          {problem}
        </div>
      ) : null}
      {notice ? (
        <div className="panel" role="status" style={{ padding: "12px 16px" }}>
          {notice}
        </div>
      ) : null}
      {error ? <div className="empty-state">{error}</div> : null}

      {orders.length === 0 && !error ? (
        <div className="empty-state">
          No purchase orders loaded, so the Step 2.2 two-way match reports every
          quoted PO as not found. Upload your ERP&apos;s export — a JSON list of
          records with <code>po_number</code>, <code>vendor_name</code> and{" "}
          <code>total_amount</code>.
        </div>
      ) : (
        <>
          <span className="muted" style={{ fontSize: 12.5 }}>
            {data?.count} orders · loaded from {data?.source}
          </span>
          <div className="panel table-scroll" style={{ padding: 8 }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>PO</th>
                  <th>Vendor</th>
                  <th>Total</th>
                  <th>Invoiced</th>
                  <th>Remaining</th>
                  <th>Status</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {orders.map((order) => {
                  const key = order.po_number ?? "";
                  const open = expanded === key;
                  return (
                    <Fragment key={key}>
                      <tr>
                        <td>{order.po_number}</td>
                        <td>{order.vendor_name}</td>
                        <td>{amount(order.total_amount, order.currency)}</td>
                        <td className="muted">
                          {amount(order.amount_invoiced, order.currency)}
                        </td>
                        <td>{amount(order.remaining, order.currency)}</td>
                        <td>
                          <span
                            className={`badge ${order.open ? "badge-ok" : "badge-neutral"}`}
                          >
                            {order.status ?? (order.open ? "open" : "closed")}
                          </span>
                        </td>
                        <td>
                          {canImport && order.po_number ? (
                            <button
                              type="button"
                              className="muted"
                              aria-expanded={open}
                              onClick={() => setExpanded(open ? null : key)}
                            >
                              {open ? "Hide lines" : "Lines & receipts"}
                            </button>
                          ) : null}
                        </td>
                      </tr>
                      {open ? (
                        <tr>
                          <td colSpan={7}>
                            <PoDetailRows poNumber={key} />
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
