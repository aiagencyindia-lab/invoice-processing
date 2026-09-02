"use client";

import { useEffect } from "react";
import { objectUrl, type InvoiceDetail } from "@/lib/api";
import { useState } from "react";

/** One invoice, beside the sheet rather than instead of it.
 *
 *  Read-only on purpose. Editing lives in the workbench, which already has the
 *  field form, the corrections trail and the keyboard map -- a second editor
 *  would be a second place for the same rules to drift apart. The drawer links
 *  there instead, and j/k move through the page without closing it, so a
 *  reviewer keeps their place in the list.
 */
/** A line item's fields are untyped JSON, so they are rendered as text at the
 *  boundary rather than trusted to be strings. */
function text(value: unknown): string {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

export function Drawer({
  detail,
  loading,
  onClose,
  onPrevious,
  onNext,
}: {
  detail: InvoiceDetail | null;
  loading: boolean;
  onClose: () => void;
  onPrevious: () => void;
  onNext: () => void;
}) {
  const [documentUrl, setDocumentUrl] = useState<string | null>(null);
  const caseId = detail?.case_id ?? null;

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
            </dl>
          </section>

          <section>
            <div className="eyebrow">Extracted fields</div>
            <dl className="kv">
              {detail.fields.map((field) => (
                <div key={field.name} style={{ display: "contents" }}>
                  <dt>{field.name}</dt>
                  <dd>
                    {String(field.value ?? "—")}{" "}
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
            Open in workbench to edit
          </a>
        </div>
      ) : (
        <p className="muted">{loading ? "Loading…" : "Nothing selected."}</p>
      )}
    </aside>
  );
}
