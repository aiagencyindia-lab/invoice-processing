"use client";

import { useState } from "react";
import { api, ApiError, type ApiKey, type ApiKeyCreated } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

/** Roles a key may carry: never admin, so a leaked key cannot mint more keys. */
const KEY_ROLES = ["viewer", "clerk", "approver"];

/** Copy a secret to the clipboard; the reveal stays up whether or not it worked. */
export async function copy(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** Per-tenant API keys for /v2. The full key exists in this component's state
 *  once, right after creation, and is gone when the admin dismisses it. */
export function ApiKeysPanel() {
  const { data, error, reload } = usePoll<{ keys: ApiKey[] }>("/api/integrations/api-keys");
  const [name, setName] = useState("");
  const [role, setRole] = useState("viewer");
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);

  async function create() {
    setBusy(true);
    setProblem(null);
    try {
      const key = await api<ApiKeyCreated>("/api/integrations/api-keys", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), role }),
      });
      setCreated(key);
      setCopied(false);
      setName("");
      reload();
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(key: ApiKey) {
    if (!window.confirm(`Revoke ${key.name}? Anything using it stops working at once.`)) return;
    setProblem(null);
    try {
      await api(`/api/integrations/api-keys/${encodeURIComponent(key.id)}`, { method: "DELETE" });
      reload();
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    }
  }

  return (
    <div className="panel stack" style={{ padding: 16, gap: 10 }} aria-label="API keys">
      <h2 style={{ margin: 0 }}>API keys</h2>
      {error ? (
        <div className="empty-state">{error}</div>
      ) : (
        <>
          <form
            className="row"
            style={{ gap: 8, alignItems: "end", flexWrap: "wrap" }}
            onSubmit={(event) => {
              event.preventDefault();
              void create();
            }}
          >
            <label className="stack" style={{ gap: 4 }}>
              <span className="eyebrow">Key name</span>
              <input
                className="input-field"
                maxLength={128}
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            <label className="stack" style={{ gap: 4 }}>
              <span className="eyebrow">Key role</span>
              <select className="input-field" value={role} onChange={(event) => setRole(event.target.value)}>
                {KEY_ROLES.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
            <button type="submit" className="btn-primary" disabled={busy || !name.trim()}>
              Create key
            </button>
          </form>

          {created ? (
            <div className="panel stack" style={{ padding: 12, gap: 6 }} role="status" aria-label="New API key">
              <strong style={{ fontSize: 13 }}>Copy this key now. It is not shown again.</strong>
              <code style={{ wordBreak: "break-all" }}>{created.key}</code>
              <div className="row" style={{ gap: 8 }}>
                <button type="button" className="btn-secondary" onClick={async () => setCopied(await copy(created.key))}>
                  {copied ? "Copied ✓" : "Copy"}
                </button>
                <button type="button" className="btn-secondary" onClick={() => setCreated(null)}>
                  Done
                </button>
              </div>
            </div>
          ) : null}

          {data?.keys.length ? (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Prefix</th>
                    <th>Role</th>
                    <th>Created</th>
                    <th>Last used</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {data.keys.map((key) => (
                    <tr key={key.id}>
                      <td>{key.name}</td>
                      <td>
                        <code>ik_{key.prefix}_…</code>
                      </td>
                      <td>{key.role}</td>
                      <td>{key.created_at ? new Date(key.created_at).toLocaleDateString() : "—"}</td>
                      <td>{key.last_used_at ? new Date(key.last_used_at).toLocaleString() : "never"}</td>
                      <td>
                        {key.revoked_at ? (
                          <span className="badge badge-neutral">revoked</span>
                        ) : (
                          <button
                            type="button"
                            className="btn-chip"
                            aria-label={`Revoke ${key.name}`}
                            onClick={() => revoke(key)}
                          >
                            Revoke
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : data ? (
            <span className="muted" style={{ fontSize: 12.5 }}>
              No keys yet.
            </span>
          ) : null}
        </>
      )}
      {problem ? (
        <p role="alert" style={{ margin: 0, color: "var(--error)", fontSize: 12.5 }}>
          {problem}
        </p>
      ) : null}
    </div>
  );
}
