"use client";

import { useState } from "react";
import {
  api,
  ApiError,
  can,
  type Me,
  type SettingField,
  type Check,
  type Integrations,
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
  // A rate table, one "code: rate" per line. It is a table of numbers and it
  // belongs in the settings screen: without it every line of an invoice is
  // checked against a single rate, and a terminal is the only other way in.
  if (field.type === "dict") {
    const entries = Object.entries(
      (field.value ?? {}) as Record<string, unknown>,
    );
    return entries.map(([code, rate]) => `${code}: ${rate}`).join("\n");
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
  if (field.type === "dict") {
    const table: Record<string, number> = {};
    for (const line of String(raw).split("\n")) {
      const [code, rate] = line.split(":");
      if (!code?.trim() || rate === undefined) continue;
      const value = Number(rate.trim());
      // A malformed line is left for the server to refuse by name, rather
      // than silently dropped here -- a rate table missing one code checks
      // that code against the wrong rate and says nothing.
      table[code.trim()] = Number.isNaN(value) ? (rate.trim() as never) : value;
    }
    return table;
  }
  const text = String(raw).trim();
  if (text === "") return undefined;
  // Text settings are sent as typed. Coercing them to numbers, which is what
  // this used to do for anything that was not a boolean or a list, turned a
  // tax-ID format into NaN and quietly discarded it.
  if (field.type === "str") return text;
  const value = Number(text);
  return Number.isNaN(value) ? undefined : value;
}

const PHASES = [
  { id: "phase1", label: "Phase 1 — Initial intake" },
  { id: "phase2", label: "Phase 2 — Content" },
  { id: "phase3", label: "Phase 3 — External" },
  { id: "phase4", label: "Phase 4 — Calculation" },
];

export default function SettingsPage() {
  const { data, error, reload } = usePoll<
    SettingsResponse & { integrations?: Integrations; checks?: Check[] }
  >("/api/settings");
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
  // `disabled_checks` has a control of its own above; left in the generated
  // list it would render a second time as a textarea of step numbers.
  const fields = (data?.fields ?? []).filter(
    (field) => field.key !== "disabled_checks",
  );

  /** Checks are edited as a set of switched-off ids, which is what the API
   *  stores. Holding the disabled ones rather than the enabled ones means a
   *  step added to the rulebook later arrives switched on, which is the safe
   *  direction for a validation to default. */
  const disabled: string[] =
    (edits["disabled_checks"] as unknown as string[] | undefined) ??
    ((data?.settings?.disabled_checks as string[] | undefined) ?? []);

  const shownCheck = (check: Check) => !disabled.includes(check.id);

  const toggleCheck = (check: Check, on: boolean) => {
    const next = on
      ? disabled.filter((id) => id !== check.id)
      : [...disabled, check.id];
    setEdits({
      ...edits,
      disabled_checks: [...new Set(next)].sort() as unknown as string,
    });
  };

  const shown = (field: SettingField): string | boolean =>
    field.key in edits ? edits[field.key] : toInput(field);

  const changed = fields.filter(
    (field) =>
      field.key in edits &&
      JSON.stringify(edits[field.key]) !== JSON.stringify(toInput(field)),
  );

  const checksChanged =
    "disabled_checks" in edits &&
    JSON.stringify(disabled) !==
      JSON.stringify(data?.settings?.disabled_checks ?? []);

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
    // Already a list of step ids; the generic list handling splits text on
    // newlines and would turn it into one long string.
    if (checksChanged) settings.disabled_checks = disabled;

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
          ) : field.type === "list" || field.type === "dict" ? (
            <textarea
              id={field.key}
              className="input-field"
              rows={4}
              disabled={!editable}
              value={String(shown(field) ?? "")}
              placeholder={
                field.type === "dict"
                  ? "One per line, e.g. 998315: 0.18"
                  : "One per line"
              }
              onChange={(event) =>
                setEdits({ ...edits, [field.key]: event.target.value })
              }
            />
          ) : field.type === "str" ? (
            <input
              id={field.key}
              className="input-field"
              type="text"
              disabled={!editable}
              value={String(shown(field) ?? "")}
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

      <section className="panel stack" style={{ gap: 12 }}>
        <div className="eyebrow">Validation checks</div>
        <div className="muted" style={{ fontSize: 12.5, lineHeight: 1.5 }}>
          Every step the pipeline runs, in order. A step you switch off is
          recorded on the decision as <em>skipped</em>, never as passed — a
          check nobody ran has no verdict, and an invoice should not be able to
          claim one.
        </div>

        {PHASES.map((phase) => {
          const steps = (data?.checks ?? []).filter((c) => c.phase === phase.id);
          if (!steps.length) return null;
          return (
            <div key={phase.id} className="stack" style={{ gap: 6 }}>
              <div
                style={{
                  font: "600 11px var(--font-mono)",
                  letterSpacing: "0.1em",
                  textTransform: "uppercase",
                  color: "var(--text-faint)",
                  marginTop: 6,
                }}
              >
                {phase.label}
              </div>
              {steps.map((step) => {
                const on = shownCheck(step);
                return (
                  <label
                    key={step.id}
                    className="row-between"
                    style={{
                      gap: 10,
                      padding: "7px 0",
                      borderTop: "1px solid var(--divider)",
                      cursor: step.mandatory || !editable ? "default" : "pointer",
                      opacity: step.mandatory ? 0.6 : 1,
                    }}
                  >
                    <span style={{ minWidth: 0 }}>
                      <span style={{ font: "600 13px var(--font-ui)" }}>
                        {step.id} {step.name}
                      </span>
                      <span
                        className="faint"
                        style={{ display: "block", fontSize: 11.5 }}
                      >
                        {step.mandatory
                          ? "Always on — without it there is nothing to check."
                          : step.informational
                            ? "Reports only; never rejects an invoice."
                            : (step.rejection ?? "")}
                      </span>
                    </span>
                    <input
                      type="checkbox"
                      checked={on}
                      disabled={!editable || step.mandatory}
                      onChange={(event) => toggleCheck(step, event.target.checked)}
                    />
                  </label>
                );
              })}
            </div>
          );
        })}
      </section>

      <section className="panel stack" style={{ gap: 10 }}>
        <div className="eyebrow">Export destinations</div>
        <div className="muted" style={{ fontSize: 12.5, lineHeight: 1.5 }}>
          Where an approved invoice can be sent. Configured per deployment, not
          per organisation, so changing one is an operator task rather than
          something this screen can do.
        </div>
        {Object.entries(data?.integrations ?? {}).map(([name, integration]) => (
          <div key={name} className="row-between" style={{ gap: 10 }}>
            <span style={{ font: "600 13px var(--font-ui)" }}>
              {name === "csv" ? "CSV download" : label(name)}
            </span>
            <span style={{ display: "flex", alignItems: "center", gap: 10 }}>
              {integration.configured ? null : (
                <span className="faint" style={{ fontSize: 11.5 }}>
                  needs {integration.sets.join(" and ")}
                </span>
              )}
              <span
                className={`badge ${integration.configured ? "badge-ok" : "badge-neutral"}`}
              >
                {integration.configured ? "configured" : "not configured"}
              </span>
            </span>
          </div>
        ))}
      </section>

      {editable ? (
        <div className="row-between">
          <span className="muted" style={{ fontSize: 13 }}>
            {changed.length + (checksChanged ? 1 : 0)
              ? `${changed.length + (checksChanged ? 1 : 0)} unsaved change${
                  changed.length + (checksChanged ? 1 : 0) > 1 ? "s" : ""
                }`
              : "No unsaved changes"}
          </span>
          <button
            type="button"
            className="btn-primary"
            disabled={saving || (changed.length === 0 && !checksChanged)}
            onClick={save}
          >
            {saving ? "Saving…" : "Save settings"}
          </button>
        </div>
      ) : null}
    </div>
  );
}
