"use client";

import { useState } from "react";
import {
  api,
  ApiError,
  objectUrl,
  can,
  type ActionResult,
  type Me,
  type MyApproval,
} from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { DelegationsPanel } from "@/components/DelegationsPanel";
import { DecisionDialog } from "@/components/DecisionDialog";

const POLL_MS = 15000;

export default function ApprovalsPage() {
  const { data: me } = usePoll<Me>("/api/me");
  const { data, error, reload } = usePoll<{ items: MyApproval[] }>(
    "/api/approvals/mine",
    POLL_MS,
  );
  const [deciding, setDeciding] = useState<{
    item: MyApproval;
    action: "approve" | "reject";
  } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // The document route needs the bearer token, which a plain link cannot send.
  async function openPdf(caseId: string) {
    const tab = window.open("", "_blank");
    try {
      const url = await objectUrl(`/api/invoices/${caseId}/document`);
      if (tab) tab.location.href = url;
    } catch (caught) {
      tab?.close();
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    }
  }

  async function decide(
    item: MyApproval,
    action: "approve" | "reject",
    reason: string,
    category: string,
  ) {
    if (busy) return;
    setBusy(true);
    setNotice(null);
    setProblem(null);
    try {
      const result = await api<ActionResult>(
        `/api/invoices/${item.case_id}/actions`,
        {
          method: "POST",
          body: JSON.stringify({
            action,
            reason: reason || null,
            reason_category: category,
          }),
        },
      );
      const verb = action === "approve" ? "Approved" : "Rejected";
      setNotice(
        result.completed
          ? `${verb} ${item.invoice_number ?? item.case_id}`
          : `Step ${result.step} ${verb.toLowerCase()}; next: ${
              result.next
                ? `${result.next.assignee_kind}:${result.next.assignee_value}`
                : "none"
            }`,
      );
      reload();
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    } finally {
      setBusy(false);
    }
  }

  const items = data?.items ?? [];
  const noDb = error?.includes("needs a database");
  return (
    <div className="stack" style={{ gap: 16, maxWidth: 560 }}>
      <h1>Approvals</h1>

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

      {noDb ? (
        <span className="muted" style={{ fontSize: 12.5 }}>
          {error}
        </span>
      ) : error ? (
        <div className="empty-state">{error}</div>
      ) : data && items.length === 0 ? (
        <div className="empty-state">Nothing is waiting for you.</div>
      ) : (
        <div className="card-list">
          {items.map((item) => (
            <div key={`${item.case_id}-${item.position}`} className="panel stack" style={{ gap: 8 }}>
              <div className="row-between">
                <a href={`/invoices?case=${encodeURIComponent(item.case_id)}`}>
                  <strong>{item.vendor ?? "(unknown vendor)"}</strong>{" "}
                  {item.invoice_number ?? ""}
                </a>
                <span style={{ font: "600 15px var(--font-ui)" }}>
                  {item.total === null
                    ? ""
                    : `${item.currency ?? ""} ${item.total.toLocaleString(undefined, { minimumFractionDigits: 2 })}`.trim()}
                </span>
              </div>
              <div className="muted" style={{ fontSize: 13 }}>
                Step {item.position} · {item.why}
              </div>
              {item.due_at ? (
                <div style={{ fontSize: 12.5 }}>
                  Due {new Date(item.due_at).toLocaleDateString()}{" "}
                  {item.overdue ? <span className="badge badge-error">overdue</span> : null}
                </div>
              ) : null}
              <button
                type="button"
                className="btn-chip"
                onClick={() => openPdf(item.case_id)}
              >
                Open PDF
              </button>
              <button
                type="button"
                className="btn-primary btn-big"
                disabled={busy}
                onClick={() => setDeciding({ item, action: "approve" })}
              >
                Approve
              </button>
              <button
                type="button"
                className="btn-secondary btn-big"
                disabled={busy}
                onClick={() => setDeciding({ item, action: "reject" })}
              >
                Reject
              </button>
            </div>
          ))}
        </div>
      )}

      {can(me, "approver") ? <DelegationsPanel me={me} fixedDelegator /> : null}

      {deciding ? (
        <DecisionDialog
          action={deciding.action}
          onCancel={() => setDeciding(null)}
          onConfirm={(reason, category) => {
            const { item, action } = deciding;
            setDeciding(null);
            decide(item, action, reason, category);
          }}
        />
      ) : null}
    </div>
  );
}
