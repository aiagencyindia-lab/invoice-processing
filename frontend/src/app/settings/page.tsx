"use client";

import { useState } from "react";
import {
  api,
  ApiError,
  can,
  type Me,
  type SettingField,
  type SettingsResponse,
} from "@/lib/api";
import { usePoll } from "@/lib/hooks";

/** `organization_names` is a list; the API's other settable keys are scalars.
 *  A textarea with one name per line beats a comma-separated box: company
 *  names contain commas ("Acme, Inc.") and would silently split in two. */
function label(key: string): string {
  return key
    .replace(/_/g, " ")
    .replace(/^./, (c) => c.toUpperCase())
    // "Po tolerance" reads as a name, not a purchase order.
    .replace(/\bPo\b/g, "PO");
}

function toInput(field: SettingField): string | boolean {
  if (field.type === "bool") return Boolean(field.value);
  if (field.type === "list") {
    return Array.isArray(field.value) ? field.value.join("\n") : "";
  }
  return field.value === null || field.value === undefined
    ? ""
    : String(field.value);
}

/** Back to the shape the API validates. Returns undefined when the field
 *  should not be sent at all -- an empty number box means "unchanged", not
 *  zero, and sending zero would silently tighten a tolerance to nothing. */
function toPayload(field: SettingField, raw: string | boolean): unknown {
  if (field.type === "bool") return Boolean(raw);
  if (field.type === "list") {
    return String(raw)
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
  }
  const text = String(raw).trim();
  if (text === "") return undefined;
  const value = Number(text);
  return Number.isNaN(value) ? undefined : value;
}

export default function SettingsPage() {
  const { data, error, reload } = usePoll<SettingsResponse>("/api/settings");
  const { data: me } = usePoll<Me>("/api/me");

  // Only the keys the admin has actually touched. Holding an overlay rather
  // than a full copy of the form keeps the server's values authoritative for
  // everything else: there is no second copy to fall out of sync, and no
  // effect needed to reset one after a save.
  const [edits, setEdits] = useState<Record<string, string | boolean>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const editable = can(me, "admin");
  const fields = data?.fields ?? [];

  const shown = (field: SettingField): string | boolean =>
    field.key in edits ? edits[field.key] : toInput(field);

  const changed = fields.filter(
    (field) =>
      field.key in edits &&
      JSON.stringify(edits[field.key]) !== JSON.stringify(toInput(field)),
  );

  async function save() {
    setSaving(true);
    setNotice(null);
    setProblem(null);

    // Only what actually changed. Sending the whole form would rewrite values
    // the admin never touched, and would push a default back as an override.
    const settings: Record<string, unknown> = {};
    for (const field of changed) {
      const value = toPayload(field, shown(field));
      if (value !== undefined) settings[field.key] = value;
    }

    try {
      const result = await api<{ changed: string[] }>("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ settings }),
      });
      setNotice(
        result.changed.length
          ? `Saved: ${result.changed.map(label).join(", ")}. New invoices will use these; already-processed ones keep the decision they were given.`
          : "Nothing to save.",
      );
      setEdits({});
      reload();
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="stack" style={{ gap: 20, maxWidth: 760 }}>
      <h1>Settings</h1>

      {!editable ? (
        <div className="panel muted" style={{ padding: "12px 16px" }}>
          These are shown so you can see why an invoice was decided the way it
          was. Only an administrator can change them.
        </div>
      ) : null}

      {error ? <div className="empty-state">{error}</div> : null}
      {problem ? (
        <div className="panel" role="alert" style={{ padding: "12px 16px", borderColor: "var(--error)", color: "var(--error)" }}>
          {problem}
        </div>
      ) : null}
      {notice ? (
        <div className="panel" role="status" style={{ padding: "12px 16px" }}>
          {notice}
        </div>
      ) : null}

      {fields.map((field) => (
        <div key={field.key} className="panel stack" style={{ gap: 8 }}>
          <label htmlFor={field.key} style={{ font: "600 14px var(--font-ui)" }}>
            {label(field.key)}
          </label>
          <div className="muted" style={{ fontSize: 12.5, lineHeight: 1.5 }}>
            {field.description}
          </div>

          {field.type === "bool" ? (
            <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <input
                id={field.key}
                type="checkbox"
                checked={Boolean(shown(field))}
                disabled={!editable}
                onChange={(event) =>
                  setEdits({ ...edits, [field.key]: event.target.checked })
                }
              />
              <span className="muted" style={{ fontSize: 13 }}>
                {shown(field) ? "On" : "Off"}
              </span>
            </label>
          ) : field.type === "list" ? (
            <textarea
              id={field.key}
              className="input-field"
              rows={4}
              disabled={!editable}
              value={String(shown(field) ?? "")}
              placeholder="One name per line"
              onChange={(event) =>
                setEdits({ ...edits, [field.key]: event.target.value })
              }
            />
          ) : (
            <input
              id={field.key}
              className="input-field"
              type="number"
              step="any"
              min="0"
              disabled={!editable}
              value={String(shown(field) ?? "")}
              onChange={(event) =>
                setEdits({ ...edits, [field.key]: event.target.value })
              }
            />
          )}
        </div>
      ))}

      {editable ? (
        <div className="row-between">
          <span className="muted" style={{ fontSize: 13 }}>
            {changed.length
              ? `${changed.length} unsaved change${changed.length > 1 ? "s" : ""}`
              : "No unsaved changes"}
          </span>
          <button
            type="button"
            className="btn-primary"
            disabled={saving || changed.length === 0}
            onClick={save}
          >
            {saving ? "Saving…" : "Save settings"}
          </button>
        </div>
      ) : null}
    </div>
  );
}
