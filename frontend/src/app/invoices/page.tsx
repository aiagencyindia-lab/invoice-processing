"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  api,
  ApiError,
  can,
  objectUrl,
  type ActionResult,
  type ApprovalChain,
  type ApprovalStep,
  type Correction,
  type DocumentList,
  type Finding,
  type InvoiceDetail,
  type InvoiceField,
  type InvoiceList,
  type Me,
  type MyApproval,
} from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { DecisionDialog } from "@/components/DecisionDialog";

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

  // Both 503 without a database; the panel then simply does not appear.
  const { data: chain, reload: reloadChain } = usePoll<ApprovalChain>(
    current ? `/api/invoices/${current}/approvals` : null,
  );
  const { data: mine, reload: reloadMine } = usePoll<{ items: MyApproval[] }>(
    "/api/approvals/mine",
  );
  const currentStep = chain?.steps.find((s) => s.position === chain.current);
  // Once the queue has loaded and this step is not in it, Approve and Reject
  // would only earn a refusal, so they are disabled. Still only a hint: the
  // server decides, and an admin keeps Override.
  const notMine =
    currentStep && mine && !mine.items.some((i) => i.case_id === current)
      ? currentStep
      : null;

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
      overrideReason: string | null = null,
    ) => {
      if (!detail) return;
      try {
        const result = await api<ActionResult>(
          `/api/invoices/${detail.case_id}/actions`,
          {
            method: "POST",
            body: JSON.stringify({
              action,
              reason,
              reason_category: reasonCategory,
              ...(overrideReason ? { override_reason: overrideReason } : {}),
            }),
          },
        );
        reloadChain();
        reloadMine();
        if (result.completed === false) {
          // More people still have to sign: stay here, the queue has not moved.
          const next = result.next
            ? `${result.next.assignee_kind}:${result.next.assignee_value}`
            : "no one";
          say(`Step ${result.step} approved; waiting on ${next}`);
          reloadDetail();
          return;
        }
        say(`${action === "approve" ? "Approved" : "Rejected"} ${detail.case_id}`);
        reloadQueue();
        if (index >= 0 && index < queue.length - 1) open(queue[index + 1].case_id);
        else reloadDetail();
      } catch (caught) {
        say(caught instanceof ApiError ? caught.friendly : String(caught));
      }
    },
    [detail, index, queue, open, say, reloadQueue, reloadDetail, reloadChain, reloadMine],
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
          if (canAct && !notMine) setDeciding("approve");
          break;
        case "r":
          if (canAct && !notMine) setDeciding("reject");
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
  }, [index, queue, open, act, save, exportCsv, canAct, canEdit, notMine]);

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
                          disabled={Boolean(notMine)}
                        >
                          Approve<kbd>a</kbd>
                        </button>
                        <button
                          type="button"
                          className="btn-chip"
                          onClick={() => setDeciding("reject")}
                          disabled={Boolean(notMine)}
                        >
                          Reject<kbd>r</kbd>
                        </button>
                        {can(me, "admin") && currentStep ? (
                          <button
                            type="button"
                            className="btn-chip"
                            onClick={() => {
                              const why = window.prompt(
                                "Why does this approval override the routing rule?",
                              );
                              if (why?.trim()) act("approve", null, "other", why.trim());
                            }}
                          >
                            Override…
                          </button>
                        ) : null}
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

                {canAct && notMine ? (
                  <span className="muted" style={{ fontSize: 12.5 }}>
                    Step {notMine.position} is assigned to {notMine.assignee_kind}:
                    {notMine.assignee_value}
                  </span>
                ) : null}

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
                {chain && chain.steps.length > 0 ? (
                  <ApprovalPanel chain={chain} />
                ) : null}
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

/** Who has to sign, in order. The current step is outlined; delegation and
 *  override are shown because an auditor asks "who really approved this". */
function ApprovalPanel({ chain }: { chain: ApprovalChain }) {
  const badge = (step: ApprovalStep) =>
    step.status === "approved"
      ? "badge-ok"
      : step.status === "rejected"
        ? "badge-error"
        : step.status === "pending"
          ? "badge-warn"
          : "badge-neutral";
  return (
    <div>
      <div className="eyebrow" style={{ marginBottom: 6 }}>
        Approvals
      </div>
      {chain.steps.map((step) => (
        <div
          key={step.id}
          className="finding"
          aria-current={step.position === chain.current}
          style={
            step.position === chain.current
              ? { outline: "2px solid var(--ink)" }
              : undefined
          }
        >
          <div className="row-between" style={{ font: "600 12px var(--font-ui)" }}>
            <span>
              Step {step.position} · {step.assignee_kind}:{step.assignee_value}
            </span>
            <span>
              {step.position === chain.current ? (
                <span className="muted" style={{ marginRight: 6 }}>
                  current
                </span>
              ) : null}
              <span className={`badge ${badge(step)}`}>{step.status}</span>
            </span>
          </div>
          <div className="evidence">
            {step.decided_by
              ? `${step.decided_by}${step.acting_for ? ` (as delegate for ${step.acting_for})` : ""}`
              : null}
            {step.due_at ? ` due ${new Date(step.due_at).toLocaleDateString()}` : ""}
            {step.escalated_to ? ` escalated to ${step.escalated_to}` : ""}
            {step.override_reason ? ` override: ${step.override_reason}` : ""}
            {step.note ? ` note: ${step.note}` : ""}
          </div>
        </div>
      ))}
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
