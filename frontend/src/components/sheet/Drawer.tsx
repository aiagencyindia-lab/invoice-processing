"use client";

import { useCallback, useEffect, useRef } from "react";
import {
  ApiError,
  api,
  can,
  clockTime,
  objectUrl,
  type InvoiceDetail,
  type Me,
} from "@/lib/api";
import { useState } from "react";

/** One invoice, beside the sheet rather than instead of it.
 *
 *  Editing here posts to the same `PATCH /fields` the workbench does, so there
 *  is one editor's worth of rules reachable through two doors: same
 *  corrections, same audit trail, same values. What the drawer does not have
 *  is a second set of them.
 *
 *  Editing takes a lock first. Without one, two reviewers working the same
 *  exception queue each read the extraction artifact and each write it back
 *  whole, and whoever saves second silently discards the other's corrections.
 *
 *  j/k still move through the page without closing the drawer, so a reviewer
 *  keeps their place in the list.
 */
/** A line item's fields are untyped JSON, so they are rendered as text at the
 *  boundary rather than trusted to be strings. */
function text(value: unknown): string {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

export function Drawer({
  detail,
  loading,
  me,
  onClose,
  onPrevious,
  onNext,
  onSaved,
}: {
  detail: InvoiceDetail | null;
  loading: boolean;
  me: Me | null;
  onClose: () => void;
  onPrevious: () => void;
  onNext: () => void;
  /** Reload the row and the detail: a saved edit changes both. */
  onSaved: () => void;
}) {
  const [documentUrl, setDocumentUrl] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // Whether *we* hold the lock, readable from a cleanup function that runs
  // after the state is gone.
  const held = useRef(false);
  const caseId = detail?.case_id ?? null;
  const lock = detail?.lock ?? null;
  const theirs = Boolean(lock && lock.holder !== me?.email);

  const release = useCallback(async (id: string) => {
    try {
      await api(`/api/invoices/${id}/lock`, { method: "DELETE" });
    } catch {
      // Nothing useful to do: the lock lapses on its own, which is the whole
      // reason it has an expiry.
    }
  }, []);

  const startEditing = async () => {
    if (!caseId) return;
    setNotice(null);
    try {
      await api(`/api/invoices/${caseId}/lock`, { method: "POST" });
      held.current = true;
      setEdits({});
      setEditing(true);
    } catch (caught) {
      setNotice(
        caught instanceof ApiError ? caught.friendly : String(caught),
      );
      onSaved(); // Re-read: someone else holds it, and the banner should say so.
    }
  };

  const stopEditing = async () => {
    setEditing(false);
    setEdits({});
    if (caseId && held.current) {
      held.current = false;
      await release(caseId);
    }
  };

  const save = async () => {
    if (!caseId) return;
    const changed = Object.entries(edits);
    if (!changed.length) {
      await stopEditing();
      return;
    }
    setSaving(true);
    setNotice(null);
    try {
      await api(`/api/invoices/${caseId}/fields`, {
        method: "PATCH",
        body: JSON.stringify({
          edits: changed.map(([field, value]) => ({ field, value })),
        }),
      });
      setEditing(false);
      setEdits({});
      held.current = false;
      await release(caseId);
      onSaved();
    } catch (caught) {
      setNotice(
        caught instanceof ApiError ? caught.friendly : String(caught),
      );
    } finally {
      setSaving(false);
    }
  };

  // Renewed well inside the fifteen-minute expiry, because reading an invoice
  // properly takes longer than one: without this the lock lapses under a
  // reviewer who is still typing, and someone else can take the record.
  useEffect(() => {
    if (!editing || !caseId) return;
    const timer = setInterval(
      () => {
        api(`/api/invoices/${caseId}/lock`, { method: "POST" }).catch(() => {});
      },
      10 * 60 * 1000,
    );
    return () => clearInterval(timer);
  }, [editing, caseId]);

  // Moving to another invoice, or closing the drawer, gives the lock back
  // rather than leaving it to expire: the next reviewer should not wait out a
  // quarter of an hour for a record nobody is looking at any more. A ref
  // rather than the `editing` state because this must run on unmount, when
  // state is already gone.
  useEffect(() => {
    if (!caseId) return;
    return () => {
      if (held.current) {
        held.current = false;
        release(caseId);
      }
    };
  }, [caseId, release]);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "Escape") onClose();
      if (event.key === "j") onNext();
      if (event.key === "k") onPrevious();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose, onNext, onPrevious]);

  // The PDF is fetched with the bearer token and handed over as a blob: an
  // iframe src is a plain navigation and carries no Authorization header.
  useEffect(() => {
    let revoked: string | null = null;
    let cancelled = false;
    if (caseId) {
      objectUrl(`/api/invoices/${caseId}/document`)
        .then((url) => {
          if (cancelled) {
            URL.revokeObjectURL(url);
            return;
          }
          revoked = url;
          setDocumentUrl(url);
        })
        .catch(() => setDocumentUrl(null));
    }
    return () => {
      cancelled = true;
      if (revoked) URL.revokeObjectURL(revoked);
      setDocumentUrl(null);
    };
  }, [caseId]);

  return (
    <aside className="drawer" aria-label="Invoice detail">
      <div className="drawer-head">
        <div>
          <h2 style={{ margin: 0 }}>
            {detail?.vendor ?? (loading ? "Loading…" : "Invoice")}
          </h2>
          <p className="muted" style={{ margin: 0 }}>
            {detail?.invoice_number ?? ""}
          </p>
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <button type="button" className="btn-chip" onClick={onPrevious}>
            Prev<kbd>k</kbd>
          </button>
          <button type="button" className="btn-chip" onClick={onNext}>
            Next<kbd>j</kbd>
          </button>
          <button type="button" className="btn-chip" onClick={onClose}>
            Close<kbd>esc</kbd>
          </button>
        </div>
      </div>

      {detail ? (
        <div className="stack" style={{ gap: 20 }}>
          {theirs && lock ? (
            <div className="banner" role="status">
              {lock.holder} is editing this invoice until{" "}
              {clockTime(lock.expires_at)}. Your changes would overwrite
              theirs, so editing is closed until they finish.
              {can(me, "admin") ? (
                <button
                  type="button"
                  className="btn-chip"
                  style={{ marginLeft: 8 }}
                  onClick={async () => {
                    await api(
                      `/api/invoices/${detail.case_id}/lock?force=true`,
                      { method: "DELETE" },
                    );
                    onSaved();
                  }}
                >
                  Break the lock
                </button>
              ) : null}
            </div>
          ) : null}
          {notice ? (
            <div className="banner" role="alert">
              {notice}
            </div>
          ) : null}

          <div style={{ display: "flex", gap: 8 }}>
            {editing ? (
              <>
                <button
                  type="button"
                  className="btn-primary"
                  disabled={saving}
                  onClick={save}
                >
                  {saving ? "Saving…" : "Save changes"}
                </button>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={stopEditing}
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                className="btn-secondary"
                disabled={theirs || !can(me, "clerk")}
                title={
                  can(me, "clerk")
                    ? undefined
                    : "Editing an invoice needs the clerk role."
                }
                onClick={startEditing}
              >
                Edit fields
              </button>
            )}
          </div>

          <section>
            <div className="eyebrow">Summary</div>
            <dl className="kv">
              <dt>Total</dt>
              <dd>
                {detail.currency ?? ""}{" "}
                {detail.total?.toLocaleString(undefined, {
                  minimumFractionDigits: 2,
                }) ?? "—"}
              </dd>
              <dt>Invoice date</dt>
              <dd>{detail.invoice_date ?? "—"}</dd>
              <dt>Decision</dt>
              <dd>{detail.decision ?? "—"}</dd>
              <dt>Routing</dt>
              <dd>{detail.routing ?? "—"}</dd>
              {detail.workflow?.decision ? (
                <>
                  <dt>Reviewed</dt>
                  <dd>
                    {detail.workflow.decision} by {detail.workflow.decided_by}
                    {detail.agreement && detail.agreement !== "agree" ? (
                      <span
                        className="badge badge-warn"
                        style={{ marginLeft: 6 }}
                        title={
                          detail.agreement === "exemption"
                            ? "The pipeline rejected this and a person approved it"
                            : "The pipeline accepted this and a person rejected it"
                        }
                      >
                        overruled the pipeline
                      </span>
                    ) : null}
                    {detail.workflow.reason ? (
                      <div className="evidence">
                        {detail.workflow.reason_category}:{" "}
                        {detail.workflow.reason}
                      </div>
                    ) : null}
                  </dd>
                </>
              ) : null}
            </dl>
          </section>

          <section>
            <div className="eyebrow">Extracted fields</div>
            <dl className="kv">
              {detail.fields.map((field) => (
                <div key={field.name} style={{ display: "contents" }}>
                  <dt>{field.name}</dt>
                  <dd>
                    {editing ? (
                      <input
                        className="input-field"
                        aria-label={field.name}
                        value={
                          field.name in edits
                            ? edits[field.name]
                            : String(field.value ?? "")
                        }
                        onChange={(event) =>
                          setEdits({
                            ...edits,
                            [field.name]: event.target.value,
                          })
                        }
                      />
                    ) : (
                      String(field.value ?? "—")
                    )}{" "}
                    {field.checked && (field.confidence ?? 1) < 1 ? (
                      <span className="badge badge-warn">needs review</span>
                    ) : null}
                  </dd>
                </div>
              ))}
            </dl>
          </section>

          {detail.line_items.length ? (
            <section>
              <div className="eyebrow">Line items</div>
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Description</th>
                      <th>Qty</th>
                      <th>Amount</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.line_items.map((line, index) => (
                      <tr key={index}>
                        <td>{text(line.description)}</td>
                        <td>{text(line.quantity)}</td>
                        <td>{text(line.amount_ex_tax)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ) : null}

          <section>
            <div className="eyebrow">Why this decision</div>
            {detail.findings.length ? (
              <div className="stack" style={{ gap: 4 }}>
                {detail.findings.map((finding, index) => (
                  <div
                    key={index}
                    className={`finding ${finding.passed === false ? "fail" : "pass"}`}
                  >
                    <strong>{finding.step}</strong> {finding.rule}
                    <div className="evidence">{finding.evidence}</div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="muted">No validation records.</p>
            )}
          </section>

          {detail.corrections.length ? (
            <section>
              <div className="eyebrow">Edits</div>
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Field</th>
                      <th>Was</th>
                      <th>Now</th>
                      <th>By</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.corrections.map((correction, index) => (
                      <tr key={index}>
                        <td>{correction.field_path}</td>
                        <td className="muted">{String(correction.old_value ?? "—")}</td>
                        <td>{String(correction.new_value ?? "—")}</td>
                        <td className="faint">{correction.actor ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ) : null}

          <section>
            <div className="eyebrow">Document</div>
            {documentUrl ? (
              <iframe
                className="doc-frame"
                src={documentUrl}
                title="Invoice document"
                style={{ width: "100%", height: 360, border: 0 }}
              />
            ) : (
              <p className="muted">No document to show.</p>
            )}
          </section>

          <a className="btn-secondary" href={`/invoices?case=${detail.case_id}`}>
            Open in workbench
          </a>
        </div>
      ) : (
        <p className="muted">{loading ? "Loading…" : "Nothing selected."}</p>
      )}
    </aside>
  );
}
