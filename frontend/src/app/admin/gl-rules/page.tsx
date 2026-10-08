"use client";

import { useState } from "react";
import { api, ApiError, can, type GlRule, type Me } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

function matchSummary(rule: GlRule): string {
  const parts = [
    rule.vendor_name ? `vendor ${rule.vendor_name}` : null,
    rule.keyword ? `line mentions “${rule.keyword}”` : null,
  ];
  return parts.filter(Boolean).join(" and ");
}

export default function GlRulesPage() {
  const { data: me } = usePoll<Me>("/api/me");
  const rules = usePoll<{ rules: GlRule[] }>("/api/gl-rules");

  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const [vendorName, setVendorName] = useState("");
  const [keyword, setKeyword] = useState("");
  const [glAccount, setGlAccount] = useState("");
  // Unsaved account edits on existing rules, by rule id.
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  async function send(
    path: string,
    method: "POST" | "PATCH" | "DELETE",
    body: unknown,
    done: string,
    after: () => void,
  ) {
    setNotice(null);
    setProblem(null);
    try {
      await api(path, {
        method,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      setNotice(done);
      after();
      rules.reload();
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    }
  }

  function addRule() {
    send(
      "/api/gl-rules",
      "POST",
      {
        vendor_name: vendorName.trim() || null,
        keyword: keyword.trim() || null,
        gl_account: glAccount.trim(),
        enabled: true,
      },
      `Rule for ${glAccount.trim()} created.`,
      () => {
        setVendorName("");
        setKeyword("");
        setGlAccount("");
      },
    );
  }

  function forget(id: string) {
    setDrafts((all) => {
      const rest = { ...all };
      delete rest[id];
      return rest;
    });
  }

  if (me && !can(me, "clerk")) {
    return (
      <div className="stack" style={{ gap: 16 }}>
        <h1>GL rules</h1>
        <div className="empty-state">Your role does not allow this.</div>
      </div>
    );
  }
  const isAdmin = can(me, "admin");
  const noDb = rules.error?.includes("needs a database");

  return (
    <div className="stack" style={{ gap: 20, maxWidth: 860 }}>
      <h1>GL rules</h1>

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
          {rules.error}
        </span>
      ) : (
        <section className="panel stack" style={{ gap: 10 }}>
          <div className="eyebrow">Coding rules</div>
          <span className="muted" style={{ fontSize: 12.5 }}>
            Each invoice line is coded by the most specific enabled rule: vendor and
            keyword, then keyword, then vendor. A person&apos;s correction is never
            overwritten.
          </span>
          {rules.error ? (
            <span className="muted" style={{ fontSize: 12.5 }}>
              {rules.error}
            </span>
          ) : null}
          {(rules.data?.rules ?? []).map((r) => {
            const draft = drafts[r.id];
            const dirty = draft !== undefined && draft.trim() !== r.gl_account;
            return (
              <div key={r.id} className="row-between" style={{ fontSize: 13, gap: 8 }}>
                <span>
                  {matchSummary(r)} → <strong>{r.gl_account}</strong>
                  {r.enabled ? "" : " (off)"}
                </span>
                {isAdmin ? (
                  <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <input
                      className="input-field"
                      aria-label={`GL account for rule ${matchSummary(r)}`}
                      maxLength={64}
                      style={{ width: 110 }}
                      value={draft ?? r.gl_account}
                      onChange={(event) =>
                        setDrafts({ ...drafts, [r.id]: event.target.value })
                      }
                    />
                    {dirty ? (
                      <button
                        type="button"
                        className="btn-chip"
                        disabled={!draft.trim()}
                        onClick={() =>
                          send(
                            `/api/gl-rules/${r.id}`,
                            "PATCH",
                            { gl_account: draft.trim() },
                            `Rule now codes to ${draft.trim()}.`,
                            () => forget(r.id),
                          )
                        }
                      >
                        Save
                      </button>
                    ) : null}
                    <label style={{ fontSize: 12 }}>
                      <input
                        type="checkbox"
                        checked={r.enabled}
                        onChange={(event) =>
                          send(
                            `/api/gl-rules/${r.id}`,
                            "PATCH",
                            { enabled: event.target.checked },
                            `Rule ${event.target.checked ? "enabled" : "disabled"}.`,
                            () => undefined,
                          )
                        }
                      />{" "}
                      Enabled
                    </label>
                    <button
                      type="button"
                      className="btn-chip"
                      onClick={() => {
                        if (!window.confirm(`Delete the rule for ${matchSummary(r)}?`)) return;
                        send(`/api/gl-rules/${r.id}`, "DELETE", undefined, "Rule deleted.", () =>
                          forget(r.id),
                        );
                      }}
                    >
                      Delete
                    </button>
                  </span>
                ) : null}
              </div>
            );
          })}
          {rules.data && rules.data.rules.length === 0 ? (
            <span className="muted" style={{ fontSize: 12.5 }}>
              No rules yet. Lines stay uncoded until a rule or a person codes them.
            </span>
          ) : null}
          {isAdmin ? (
            <>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
                  gap: 10,
                }}
              >
                <label style={{ fontSize: 12 }}>
                  Vendor name
                  <input
                    className="input-field"
                    maxLength={255}
                    value={vendorName}
                    onChange={(event) => setVendorName(event.target.value)}
                  />
                </label>
                <label style={{ fontSize: 12 }}>
                  Line keyword
                  <input
                    className="input-field"
                    maxLength={200}
                    value={keyword}
                    onChange={(event) => setKeyword(event.target.value)}
                  />
                </label>
                <label style={{ fontSize: 12 }}>
                  GL account
                  <input
                    className="input-field"
                    maxLength={64}
                    value={glAccount}
                    onChange={(event) => setGlAccount(event.target.value)}
                  />
                </label>
              </div>
              <div>
                <button
                  type="button"
                  className="btn-primary"
                  disabled={!glAccount.trim() || !(vendorName.trim() || keyword.trim())}
                  title="Needs a GL account and a vendor name, a keyword, or both."
                  onClick={addRule}
                >
                  Add rule
                </button>
              </div>
            </>
          ) : null}
        </section>
      )}
    </div>
  );
}
