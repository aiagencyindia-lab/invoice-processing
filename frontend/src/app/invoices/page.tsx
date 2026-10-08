"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  api,
  ApiError,
  can,
  objectUrl,
  REASON_CATEGORIES,
  type Correction,
  type DocumentList,
  type Finding,
  type InvoiceDetail,
  type InvoiceField,
  type InvoiceList,
  type Me,
} from "@/lib/api";
import { usePoll } from "@/lib/hooks";

/** The queue refreshes on its own so an invoice a worker just finished shows
 *  up, but slowly: this screen is where someone is typing, and a fast poll
 *  that re-renders under their hands is worse than a stale row. */
const QUEUE_POLL_MS = 15000;

/** In-flight documents refresh quickly: this is the bar someone watches after
 *  uploading, and it is a listing, not the form they are typing into. */
const INFLIGHT_POLL_MS = 2000;

function money(value: number | null, currency: string | null): string {
  if (value === null || value === undefined) return "";
  return `${currency ?? ""} ${value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
  })}`.trim();
}

/** Why a field is flagged, in the reviewer's words.
 *
 *  Only fields we actually checked get an explanation. An unchecked field has
 *  no verdict, and labelling it "not found" would send the reviewer chasing a
 *  problem that does not exist.
 */
function why(field: InvoiceField): { text: string; unchecked: boolean } | null {
  if (!field.checked) return { text: "not corroborated", unchecked: true };
  if (field.grounded === false)
    return { text: "not found in the document", unchecked: false };
  if (field.grounded === null)
    return { text: "could not be checked against the document", unchecked: false };
  if ((field.confidence ?? 1) < 1)
    return { text: "arithmetic did not corroborate", unchecked: false };
  return null;
}

function isFlagged(field: InvoiceField): boolean {
  return field.checked && (field.confidence ?? 1) < 1;
}

function InvoicesWorkbench() {
  const params = useSearchParams();
  const { data: me } = usePoll<Me>("/api/me");
  const { data: list, reload: reloadQueue } = usePoll<InvoiceList>(
    "/api/invoices",
    QUEUE_POLL_MS,
  );

  const [selected, setSelected] = useState<string | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  // Which decision is being explained, if any. Approvals ask too: an approval
  // over a pipeline rejection is the exemption the rulebook is missing, and it
  // is worth more than the rejections if anyone records why it happened.
  const [deciding, setDeciding] = useState<"approve" | "reject" | null>(null);

  // Memoised because it is a dependency of the keyboard handler: a fresh []
  // on every render would re-bind the listener on every render.
  const queue = useMemo(() => list?.invoices ?? [], [list]);
  // Falls through to the first invoice rather than an effect that selects it:
  // the choice is derivable from what we have, so it needs no state of its own.
  const current =
    selected ?? params.get("case") ?? queue[0]?.case_id ?? null;
  const index = queue.findIndex((i) => i.case_id === current);

  const { data: detail, reload: reloadDetail } = usePoll<InvoiceDetail>(
    current ? `/api/invoices/${current}` : null,
  );

  // The document is fetched rather than pointed at: an iframe src is a plain
  // navigation and carries no bearer token, so against a service that wants
  // one the pane showed a 401 body where the invoice should be.
  const [documentUrl, setDocumentUrl] = useState<string | null>(null);
  const [documentProblem, setDocumentProblem] = useState<string | null>(null);

  useEffect(() => {
    if (!current) return;
    let revoked: string | null = null;
    let cancelled = false;

    objectUrl(`/api/invoices/${current}/document`)
      .then((url) => {
        if (cancelled) return URL.revokeObjectURL(url);
        revoked = url;
        setDocumentUrl(url);
        setDocumentProblem(null);
      })
      .catch((caught) => {
        if (cancelled) return;
        setDocumentUrl(null);
        setDocumentProblem(
          caught instanceof ApiError && caught.status === 404
            ? "No source document is stored for this invoice."
            : caught instanceof ApiError
              ? caught.friendly
              : String(caught),
        );
      });

    return () => {
      cancelled = true;
      // Without this every invoice a reviewer opens leaks its PDF for the
      // life of the tab.
      if (revoked) URL.revokeObjectURL(revoked);
    };
  }, [current]);

  const canEdit = can(me, "clerk");
  const canAct = can(me, "approver");
  const dirty = Object.keys(edits).length;

  const say = useCallback((message: string) => {
    setNotice(message);
    window.setTimeout(() => setNotice(null), 2600);
  }, []);

  /** Selecting is the one place edits are dropped, and it is deliberate: an
   *  edit belongs to the invoice it was typed into. */
  const open = useCallback((caseId: string | undefined) => {
    if (!caseId) return;
    setSelected(caseId);
    setEdits({});
  }, []);

  const save = useCallback(async () => {
    if (!detail || !dirty) return say("Nothing to save");
    try {
      const result = await api<{ count: number }>(
        `/api/invoices/${detail.case_id}/fields`,
        {
          method: "PATCH",
          body: JSON.stringify({
            edits: Object.entries(edits).map(([field, value]) => ({
              field,
              value,
            })),
          }),
        },
      );
      setEdits({});
      say(`${result.count} correction${result.count === 1 ? "" : "s"} recorded`);
      reloadDetail();
    } catch (caught) {
      say(caught instanceof ApiError ? caught.friendly : String(caught));
    }
  }, [detail, dirty, edits, say, reloadDetail]);

  const act = useCallback(
    async (
      action: "approve" | "reject",
      reason: string | null = null,
      reasonCategory = "other",
    ) => {
      if (!detail) return;
      try {
        await api(`/api/invoices/${detail.case_id}/actions`, {
          method: "POST",
          body: JSON.stringify({
            action,
            reason,
            reason_category: reasonCategory,
          }),
        });
        say(`${action === "approve" ? "Approved" : "Rejected"} ${detail.case_id}`);
        reloadQueue();
        if (index >= 0 && index < queue.length - 1) open(queue[index + 1].case_id);
        else reloadDetail();
      } catch (caught) {
        say(caught instanceof ApiError ? caught.friendly : String(caught));
      }
    },
    [detail, index, queue, open, say, reloadQueue, reloadDetail],
  );

  const exportCsv = useCallback(async () => {
    if (!detail) return;
    try {
      const result = await api<{ csv: string }>(
        `/api/invoices/${detail.case_id}/export`,
        { method: "POST", body: JSON.stringify({ target: "csv" }) },
      );
      const url = URL.createObjectURL(
        new Blob([result.csv], { type: "text/csv" }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = `${detail.case_id}.csv`;
      link.click();
      URL.revokeObjectURL(url);
      say(`Exported ${detail.case_id}.csv`);
    } catch (caught) {
      // 409 means unapproved. Saying "failed" would hide that approving is one
      // keystroke away.
      if (caught instanceof ApiError && caught.status === 409) {
        say("Approve the invoice before exporting");
      } else {
        say(caught instanceof ApiError ? caught.friendly : String(caught));
      }
    }
  }, [detail, say]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      const typing =
        target?.tagName === "INPUT" || target?.tagName === "TEXTAREA";
      if (typing) {
        if (event.key === "Enter") {
          event.preventDefault();
          save();
        }
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;

      switch (event.key.toLowerCase()) {
        case "j":
          if (index >= 0 && index < queue.length - 1)
            open(queue[index + 1].case_id);
          break;
        case "k":
          if (index > 0) open(queue[index - 1].case_id);
          break;
        case "a":
          if (canAct) setDeciding("approve");
          break;
        case "r":
          if (canAct) setDeciding("reject");
          break;
        case "s":
          if (canEdit) save();
          break;
        case "e":
          exportCsv();
          break;
        case "u":
          document.getElementById("wb-upload")?.click();
          break;
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [index, queue, open, act, save, exportCsv, canAct, canEdit]);

  async function upload(file: File) {
    const body = new FormData();
    body.append("file", file);
    try {
      const result = await api<{ duplicate: boolean }>("/api/documents", {
        method: "POST",
        body,
      });
      // Stored and queued, not extracted. Claiming otherwise sends the
      // reviewer looking for an invoice that is not in the queue yet.
      say(
        result.duplicate
          ? `Already received — ${file.name} matches a document we hold`
          : `${file.name} stored and queued for processing`,
      );
    } catch (caught) {
      say(caught instanceof ApiError ? caught.friendly : String(caught));
    }
  }

  const counts = list?.counts;

  // What the worker is doing right now. An invoice does not exist until its
  // extraction finishes, so without this the screen is silent for the minute
  // that takes -- which reads as an upload that went nowhere.
  const { data: docs } = usePoll<DocumentList>(
    "/api/documents",
    INFLIGHT_POLL_MS,
  );
  const inFlight = (docs?.documents ?? []).filter(
    (d) => d.stage === "processing",
  );

  return (
    <div className="stack" style={{ gap: 14 }}>
      <div className="row-between">
        <h1 style={{ fontSize: 22 }}>Invoices</h1>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="muted" style={{ fontSize: 12.5 }}>
            {counts
              ? `${counts.total} invoices · ${counts.needs_review} need review · ${counts.auto} auto`
              : "loading…"}
          </span>
          <input
            id="wb-upload"
            type="file"
            accept="application/pdf"
            className="sr-only"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) upload(file);
              event.target.value = "";
            }}
          />
          <label htmlFor="wb-upload" className="btn-chip">
            Upload<kbd>u</kbd>
          </label>
        </div>
      </div>

      {inFlight.length > 0 ? (
        <div className="panel" style={{ padding: "12px 16px" }} role="status">
          {inFlight.map((document) => (
            <div key={document.document_id} className="stack" style={{ gap: 4 }}>
              <strong>{document.filename ?? document.document_id}</strong>
              {document.progress ? (
                <progress
                  value={document.progress.step}
                  max={document.progress.total}
                  style={{ width: 200 }}
                />
              ) : null}
              <span className="muted">{document.explanation}</span>
            </div>
          ))}
        </div>
      ) : null}

      {queue.length === 0 ? (
        <div className="empty-state">
          Nothing to review. Upload a PDF here or from Documents; a worker
          extracts it and it appears in this queue.
        </div>
      ) : (
        <div className="workbench">
          <div>
            {queue.map((invoice) => (
              <button
                key={invoice.case_id}
                type="button"
                className="queue-item"
                aria-current={invoice.case_id === current}
                onClick={() => open(invoice.case_id)}
              >
                <div className="queue-vendor">
                  {invoice.vendor ?? "(unknown vendor)"}
                </div>
                <div className="queue-meta">
                  <span>{invoice.invoice_number ?? "—"}</span>
                  <span
                    className={`badge ${
                      invoice.routing === "REVIEW" ? "badge-warn" : "badge-ok"
                    }`}
                  >
                    {invoice.routing}
                  </span>
                </div>
                <div className="queue-meta">
                  <span>{money(invoice.total, invoice.currency)}</span>
                  <span>{invoice.decision ?? ""}</span>
                </div>
              </button>
            ))}
          </div>

          <div className="stack" style={{ gap: 16, paddingRight: 4 }}>
            {detail ? (
              <>
                <div className="row-between">
                  <div>
                    <div className="eyebrow">Extracted fields</div>
                    <span className="muted" style={{ fontSize: 12 }}>
                      {detail.fields.filter(isFlagged).length
                        ? `${detail.fields.filter(isFlagged).length} need attention`
                        : "all corroborated"}
                    </span>
                  </div>
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                    {canEdit ? (
                      <button
                        type="button"
                        className="btn-chip"
                        onClick={save}
                        disabled={!dirty}
                      >
                        Save<kbd>s</kbd>
                      </button>
                    ) : null}
                    {canAct ? (
                      <>
                        <button
                          type="button"
                          className="btn-chip"
                          onClick={() => setDeciding("approve")}
                        >
                          Approve<kbd>a</kbd>
                        </button>
                        <button
                          type="button"
                          className="btn-chip"
                          onClick={() => setDeciding("reject")}
                        >
                          Reject<kbd>r</kbd>
                        </button>
                      </>
                    ) : null}
                    <button
                      type="button"
                      className="btn-chip"
                      onClick={exportCsv}
                    >
                      Export<kbd>e</kbd>
                    </button>
                  </div>
                </div>

                {dirty ? (
                  <span className="muted" style={{ fontSize: 12 }}>
                    {dirty} unsaved edit{dirty > 1 ? "s" : ""}
                  </span>
                ) : null}

                <div>
                  {detail.fields.map((field) => {
                    const reason = why(field);
                    const value =
                      field.name in edits
                        ? edits[field.name]
                        : field.value === null || field.value === undefined
                          ? ""
                          : String(field.value);
                    return (
                      <div
                        key={field.name}
                        className={`field ${isFlagged(field) ? "flagged" : ""}`}
                      >
                        <label htmlFor={`f_${field.name}`}>{field.name}</label>
                        <input
                          id={`f_${field.name}`}
                          className="input-field"
                          value={value}
                          disabled={!canEdit}
                          onChange={(event) =>
                            setEdits({
                              ...edits,
                              [field.name]: event.target.value,
                            })
                          }
                        />
                        {reason ? (
                          <div
                            className={`why ${reason.unchecked ? "unchecked" : ""}`}
                          >
                            {reason.text}
                          </div>
                        ) : null}
                      </div>
                    );
                  })}
                </div>

                <LineItems items={detail.line_items} />
                <Findings findings={detail.findings} />
                <BankChangeShortcut detail={detail} me={me} onDone={say} />
                <Corrections corrections={detail.corrections} />
              </>
            ) : (
              <div className="empty-state">Select an invoice.</div>
            )}
          </div>

          <div>
            {documentUrl ? (
              <iframe
                className="doc-frame"
                title="Source document"
                src={documentUrl}
              />
            ) : documentProblem ? (
              <div className="empty-state">{documentProblem}</div>
            ) : current ? (
              <div className="empty-state">Loading the document…</div>
            ) : null}
          </div>
        </div>
      )}

      {notice ? (
        <div className="toast" role="status">
          {notice}
        </div>
      ) : null}

      {deciding ? (
        <DecisionDialog
          action={deciding}
          onCancel={() => setDeciding(null)}
          onConfirm={(reason, category) => {
            setDeciding(null);
            act(deciding, reason || null, category);
          }}
        />
      ) : null}
    </div>
  );
}

/** Why this decision, in one small form.
 *
 *  It replaces a `window.prompt`, which could ask for one line of free text
 *  and nothing else. The category is what the system can act on -- only
 *  "policy exception" describes a rule the rulebook is missing -- and the note
 *  is what a person reads, and what a rule writer would be given to work from.
 */
function DecisionDialog({
  action,
  onCancel,
  onConfirm,
}: {
  action: "approve" | "reject";
  onCancel: () => void;
  onConfirm: (reason: string, category: string) => void;
}) {
  const [category, setCategory] = useState("policy_exception");
  const [reason, setReason] = useState("");
  const rejecting = action === "reject";
  // The API refuses a reasonless rejection, so the button does too rather than
  // letting someone press it and read an error.
  const ready = !rejecting || reason.trim().length > 0;

  return (
    <div className="dialog-scrim" role="presentation" onClick={onCancel}>
      <div
        className="panel dialog"
        role="dialog"
        aria-modal="true"
        aria-label={rejecting ? "Reject invoice" : "Approve invoice"}
        onClick={(event) => event.stopPropagation()}
      >
        <h2 style={{ margin: "0 0 4px" }}>
          {rejecting ? "Reject this invoice" : "Approve this invoice"}
        </h2>
        <label className="stack" style={{ gap: 4 }}>
          <span className="eyebrow">Why</span>
          <select
            className="input-field"
            value={category}
            autoFocus
            onChange={(event) => setCategory(event.target.value)}
          >
            {REASON_CATEGORIES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="eyebrow">
            Note {rejecting ? "(required)" : "(optional)"}
          </span>
          <textarea
            className="input-field"
            rows={3}
            value={reason}
            placeholder="What a colleague would need to know in six months."
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button type="button" className="btn-secondary" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={!ready}
            onClick={() => onConfirm(reason.trim(), category)}
          >
            {rejecting ? "Reject" : "Approve"}
          </button>
        </div>
      </div>
    </div>
  );
}

function LineItems({ items }: { items: Record<string, unknown>[] }) {
  if (!items.length) {
    return (
      <div>
        <div className="eyebrow">Line items</div>
        <div className="muted" style={{ fontSize: 12.5 }}>
          None extracted.
        </div>
      </div>
    );
  }
  return (
    <div>
      <div className="eyebrow" style={{ marginBottom: 6 }}>
        Line items
      </div>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Description</th>
              <th>Qty</th>
              <th>Unit</th>
              <th>Amount</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item, position) => (
              <tr key={position}>
                <td>{String(item.description ?? "")}</td>
                <td>{String(item.quantity ?? "")}</td>
                <td>{String(item.unit_price ?? "")}</td>
                <td>{String(item.amount_ex_tax ?? "")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** A bank signal on this invoice: offer to raise the vendor bank change from
 *  the details already read, so nobody retypes an account number. */
function BankChangeShortcut({
  detail,
  me,
  onDone,
}: {
  detail: InvoiceDetail;
  me: Me | null;
  onDone: (m: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const bank = detail.findings.find((f) => f.step === "3.4" && f.passed === false);
  if (!bank || !can(me, "clerk")) return null;
  const pending =
    bank.signals?.includes("recent_bank_change") &&
    /awaiting approval/.test(bank.evidence ?? "");
  if (pending) {
    return (
      <div className="muted" style={{ fontSize: 12.5 }}>
        A bank change for this vendor is awaiting approval.{" "}
        <a href="/vendors">Open vendors</a>
      </div>
    );
  }
  // Only a mismatch or an unverified vendor is cured by proposing these
  // details; a recent change or a sender/domain signal is not.
  if (!bank.signals?.some((s) => s === "bank_mismatch" || s === "bank_unverified")) {
    return null;
  }
  return (
    <div className="stack" style={{ gap: 6 }}>
      <button
        className="btn-secondary"
        disabled={busy}
        onClick={async () => {
          // The approver needs to know how these were confirmed; a blank or
          // cancelled prompt sends nothing.
          const reason = window.prompt("How were these bank details confirmed with the vendor?");
          if (!reason?.trim()) return;
          setBusy(true);
          setProblem(null);
          try {
            await api(`/api/invoices/${detail.case_id}/bank-change`, {
              method: "POST",
              body: JSON.stringify({ reason }),
            });
            onDone(
              `Bank change proposed for ${detail.vendor ?? "this vendor"}. A second person must approve it on the Vendors page.`,
            );
          } catch (caught) {
            setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
          } finally {
            setBusy(false);
          }
        }}
      >
        Propose these bank details for {detail.vendor ?? "this vendor"}
      </button>
      {problem ? (
        <div role="alert" style={{ color: "var(--error)", fontSize: 12.5 }}>
          {problem}
        </div>
      ) : null}
    </div>
  );
}

/** The findings list is the product's differentiator: a decision that can be
 *  explained rather than asserted. It is shown in full, passes included --
 *  "which checks ran and passed" is half of an auditor's question. */
function Findings({ findings }: { findings: Finding[] }) {
  if (!findings.length) {
    return (
      <div>
        <div className="eyebrow">Why this decision</div>
        <div className="muted" style={{ fontSize: 12.5 }}>
          No validation records.
        </div>
      </div>
    );
  }
  return (
    <div>
      <div className="eyebrow" style={{ marginBottom: 6 }}>
        Why this decision
      </div>
      {findings.map((finding, position) => (
        <div
          key={`${finding.phase}-${finding.step}-${position}`}
          className={`finding ${finding.passed ? "pass" : "fail"}`}
        >
          <div style={{ font: "600 12px var(--font-ui)" }}>
            {finding.passed ? "pass" : "fail"} · {finding.step ?? finding.phase}{" "}
            {finding.rule ?? ""}
            {finding.informational ? " (informational)" : ""}
            {finding.implemented === false ? " (not implemented)" : ""}
          </div>
          {finding.evidence ? (
            <div className="evidence">{finding.evidence}</div>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function Corrections({ corrections }: { corrections: Correction[] }) {
  if (!corrections.length) return null;
  return (
    <div>
      <div className="eyebrow" style={{ marginBottom: 6 }}>
        Corrections
      </div>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Field</th>
              <th>Was</th>
              <th>Now</th>
              <th>Who</th>
              <th>When</th>
            </tr>
          </thead>
          <tbody>
            {corrections.map((correction, position) => (
              <tr key={`${correction.field_path}-${position}`}>
                <td>{correction.field_path}</td>
                <td className="muted">{String(correction.old_value ?? "")}</td>
                <td>{String(correction.new_value ?? "")}</td>
                <td className="muted">{correction.actor}</td>
                <td className="faint">
                  {new Date(correction.at).toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function InvoicesPage() {
  // useSearchParams needs a Suspense boundary for this route to stay static.
  return (
    <Suspense fallback={<div className="empty-state">Loading…</div>}>
      <InvoicesWorkbench />
    </Suspense>
  );
}
