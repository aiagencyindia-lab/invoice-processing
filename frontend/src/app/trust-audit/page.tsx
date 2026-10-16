"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, ApiError, can, type Me, type TrustAudit } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { ENGINE_LABELS } from "@/lib/trust";

const CATEGORIES = ["leakage", "payment", "provenance", "gst_itc"];

const EMPTY = {
  name: "",
  period_from: "",
  period_to: "",
  vendors: "",
  categories: [] as string[],
  recovered_pct: "25",
  prevented_pct: "10",
  cap: "",
  minimum: "",
  approver: "",
};

/** A comma- or newline-separated list, trimmed and de-blanked. */
function listOf(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export default function TrustAuditPage() {
  const router = useRouter();
  const { data: me } = usePoll<Me>("/api/me");
  // The audit routes land on the v2/w2-audit branch; until then this is a 404
  // the page shows as text, with the form still usable once they exist.
  const { data: audits, error } = usePoll<TrustAudit[]>("/api/trust/audits");
  const [form, setForm] = useState(EMPTY);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function create() {
    setBusy(true);
    setProblem(null);
    try {
      const audit = await api<TrustAudit>("/api/trust/audits", {
        method: "POST",
        body: JSON.stringify({
          name: form.name.trim(),
          period_from: form.period_from || null,
          period_to: form.period_to || null,
          // TODO(scaffold follow-up): the API scopes by vendor *node id*; the
          // scaffold has no vendors -> nodes mapping, so names are sent as typed.
          vendors: listOf(form.vendors),
          categories: form.categories,
          terms: {
            recovered_pct: form.recovered_pct,
            prevented_pct: form.prevented_pct,
            cap: form.cap.trim() || null,
            minimum: form.minimum.trim() || null,
          },
          approver: form.approver.trim() || null,
        }),
      });
      router.push(`/trust-audit/${encodeURIComponent(audit.id)}`);
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
      setBusy(false);
    }
  }

  const field = (key: keyof typeof EMPTY, label: string, type = "text") => (
    <label className="stack" style={{ gap: 2, fontSize: 12.5 }}>
      {label}
      <input
        type={type}
        className="input-field"
        inputMode={type === "text" && /pct|cap|minimum/.test(key) ? "decimal" : undefined}
        value={form[key] as string}
        onChange={(event) => setForm({ ...form, [key]: event.target.value })}
      />
    </label>
  );

  return (
    <section className="stack" style={{ gap: 16 }}>
      <h1>Trust audit</h1>

      {error ? <div className="empty-state">{error}</div> : null}
      {audits && audits.length === 0 ? <div className="empty-state">No engagements yet.</div> : null}
      {audits?.length ? (
        <div className="panel table-scroll" style={{ padding: 8 }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>Engagement</th>
                <th>Status</th>
                <th>Period</th>
                <th>Vendors</th>
                <th>Approver</th>
              </tr>
            </thead>
            <tbody>
              {audits.map((audit) => (
                <tr key={audit.id}>
                  <td>
                    <Link href={`/trust-audit/${encodeURIComponent(audit.id)}`}>{audit.name}</Link>
                  </td>
                  <td>
                    <span className={`badge ${audit.status === "active" ? "badge-ok" : "badge-neutral"}`}>{audit.status}</span>
                  </td>
                  <td>
                    {audit.period_from ?? "…"} – {audit.period_to ?? "…"}
                  </td>
                  <td>{audit.vendors.length || "all"}</td>
                  <td>{audit.approver ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {can(me, "approver") ? (
        <form
          className="panel stack"
          style={{ padding: 16, gap: 10, maxWidth: 640 }}
          onSubmit={(event) => {
            event.preventDefault();
            create();
          }}
        >
          <div className="eyebrow">New engagement</div>
          {field("name", "Name")}
          <div className="row" style={{ gap: 8 }}>
            {field("period_from", "Period from", "date")}
            {field("period_to", "Period to", "date")}
          </div>
          <label className="stack" style={{ gap: 2, fontSize: 12.5 }}>
            Vendors (one per line; blank for all)
            <textarea
              className="input-field"
              rows={3}
              value={form.vendors}
              onChange={(event) => setForm({ ...form, vendors: event.target.value })}
            />
          </label>
          <fieldset className="row" style={{ gap: 12, border: 0, padding: 0, margin: 0, flexWrap: "wrap" }}>
            <legend className="eyebrow">Categories (blank for all)</legend>
            {CATEGORIES.map((category) => (
              <label key={category} className="row" style={{ gap: 4, alignItems: "center", fontSize: 12.5 }}>
                <input
                  type="checkbox"
                  checked={form.categories.includes(category)}
                  onChange={(event) =>
                    setForm({
                      ...form,
                      categories: event.target.checked
                        ? [...form.categories, category]
                        : form.categories.filter((c) => c !== category),
                    })
                  }
                />
                {ENGINE_LABELS[category] ?? category}
              </label>
            ))}
          </fieldset>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            {field("recovered_pct", "Recovered %")}
            {field("prevented_pct", "Prevented %")}
            {field("cap", "Cap (optional)")}
            {field("minimum", "Minimum (optional)")}
          </div>
          {field("approver", "Approver email", "email")}
          {problem ? (
            <p role="alert" style={{ margin: 0, color: "var(--error)", fontSize: 12.5 }}>
              {problem}
            </p>
          ) : null}
          <div>
            <button type="submit" className="btn-primary" disabled={busy || !form.name.trim()}>
              Create engagement
            </button>
          </div>
        </form>
      ) : null}
    </section>
  );
}
