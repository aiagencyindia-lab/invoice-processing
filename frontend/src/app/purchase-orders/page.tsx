"use client";

import { useRef, useState } from "react";
import {
  api,
  ApiError,
  can,
  type Me,
  type PurchaseOrderList,
} from "@/lib/api";
import { usePoll } from "@/lib/hooks";

function amount(value: number | null, currency: string | null): string {
  if (value === null || value === undefined) return "—";
  return `${currency ?? ""} ${value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
  })}`.trim();
}

export default function PurchaseOrdersPage() {
  const { data, error, reload } = usePoll<PurchaseOrderList>(
    "/api/purchase-orders",
  );
  const { data: me } = usePoll<Me>("/api/me");
  const fileInput = useRef<HTMLInputElement>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const canReplace = can(me, "admin");
  const orders = data?.purchase_orders ?? [];

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
                </tr>
              </thead>
              <tbody>
                {orders.map((order) => (
                  <tr key={order.po_number}>
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
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
