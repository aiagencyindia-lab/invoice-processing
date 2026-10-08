"use client";

import Link from "next/link";
import { useState } from "react";
import {
  api,
  ApiError,
  type TenantResponse,
  type User,
  type UserList,
} from "@/lib/api";
import { usePoll } from "@/lib/hooks";

export default function AdminPage() {
  const { data: tenant, error: tenantError } =
    usePoll<TenantResponse>("/api/admin/tenants");
  const { data: users, error: usersError, reload } =
    usePoll<UserList>("/api/admin/users");

  const [email, setEmail] = useState("");
  const [role, setRole] = useState("viewer");
  const [uid, setUid] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [managers, setManagers] = useState<Record<string, string>>({});

  // A role refusal is the whole page rather than one control, so it is said
  // once at the top instead of on every panel. Anything else -- an
  // unconfigured database, most likely -- is reported in the pane it broke.
  const refused = tenantError?.toLowerCase().includes("role") ?? false;

  async function addUser() {
    setBusy(true);
    setNotice(null);
    setProblem(null);
    try {
      const record = await api<User & { warning?: string }>(
        "/api/admin/users",
        {
          method: "POST",
          body: JSON.stringify({
            email,
            role,
            uid: uid || null,
            display_name: displayName || null,
          }),
        },
      );
      setNotice(
        record.warning ??
          `${record.email} added as ${record.role} and can sign in.`,
      );
      setEmail("");
      setUid("");
      setDisplayName("");
      reload();
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function linkAccount(target: User) {
    const value = window.prompt(
      `Identity Platform uid for ${target.email}:`,
    );
    if (!value) return;
    setNotice(null);
    setProblem(null);
    try {
      // The role is not sent: the server takes it from the stored user, so
      // linking an account can never be a way to grant a role.
      await api("/api/admin/users/claims", {
        method: "POST",
        body: JSON.stringify({ email: target.email, uid: value }),
      });
      setNotice(`${target.email} linked and can now sign in.`);
      reload();
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    }
  }

  async function saveManager(target: User) {
    setNotice(null);
    setProblem(null);
    const value = (managers[target.email] ?? "").trim();
    try {
      await api(`/api/admin/users/${target.id}`, {
        method: "PATCH",
        body: JSON.stringify({ manager_email: value || null }),
      });
      setNotice(
        value
          ? `${target.email}'s overdue approvals now escalate to ${value}.`
          : `${target.email} has no manager; overdue approvals go to an admin.`,
      );
      setManagers((all) => {
        const rest = { ...all };
        delete rest[target.email];
        return rest;
      });
      reload();
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    }
  }

  if (refused) {
    return (
      <div className="stack" style={{ gap: 16 }}>
        <h1>Admin</h1>
        <div className="empty-state">{tenantError}</div>
      </div>
    );
  }

  return (
    <div className="stack" style={{ gap: 20, maxWidth: 860 }}>
      <div className="row-between">
        <h1>Admin</h1>
        <Link href="/admin/approvals">Approval setup</Link>
      </div>

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

      <section className="panel stack" style={{ gap: 10 }}>
        <div className="eyebrow">Organisation</div>
        {tenant ? (
          <>
            <div style={{ font: "600 16px var(--font-ui)" }}>
              {tenant.tenant.name}
            </div>
            <dl
              style={{
                display: "grid",
                gridTemplateColumns: "160px 1fr",
                gap: "6px 12px",
                fontSize: 13,
              }}
            >
              <dt className="muted">Slug</dt>
              <dd>{tenant.tenant.slug}</dd>
              <dt className="muted">Tenant id</dt>
              <dd style={{ font: "12px var(--font-mono)" }}>
                {tenant.tenant.id}
              </dd>
              <dt className="muted">Invoice inbox</dt>
              <dd>
                {tenant.tenant.inbox ?? (
                  <span className="faint">
                    inbound email is not configured on this deployment
                  </span>
                )}
              </dd>
              <dt className="muted">Purchase orders</dt>
              <dd>{tenant.tenant.purchase_order_file}</dd>
            </dl>
            {!tenant.can_create_tenants ? (
              <div className="muted" style={{ fontSize: 12.5 }}>
                Creating further tenants is a platform operation and is disabled
                on this deployment.
              </div>
            ) : null}
          </>
        ) : tenantError ? (
          // Never leave this pane saying "loading…" forever. The most likely
          // cause is a deployment with no database, and the endpoint now says
          // exactly that -- so show what it said.
          <div className="empty-state">{tenantError}</div>
        ) : (
          <span className="faint">loading…</span>
        )}
      </section>

      <section className="panel stack" style={{ gap: 12 }}>
        <div className="eyebrow">People</div>

        {usersError ? <div className="empty-state">{usersError}</div> : null}

        {users && users.users.length > 0 ? (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Email</th>
                  <th>Name</th>
                  <th>Role</th>
                  <th>Can sign in</th>
                  <th>Manager</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {users.users.map((user) => (
                  <tr key={user.email}>
                    <td>{user.email}</td>
                    <td className="muted">{user.display_name ?? "—"}</td>
                    <td>
                      <span className="badge">{user.role}</span>
                    </td>
                    <td>
                      {user.uid ? (
                        <span className="badge badge-ok">yes</span>
                      ) : (
                        <span className="badge badge-warn">not linked</span>
                      )}
                    </td>
                    <td>
                      <input
                        className="input-field"
                        type="email"
                        aria-label={`Manager of ${user.email}`}
                        placeholder="none"
                        value={managers[user.email] ?? user.manager_email ?? ""}
                        onChange={(event) =>
                          setManagers({ ...managers, [user.email]: event.target.value })
                        }
                      />
                      {user.email in managers ? (
                        <button
                          type="button"
                          className="btn-chip"
                          onClick={() => saveManager(user)}
                        >
                          Save
                        </button>
                      ) : null}
                    </td>
                    <td>
                      {user.uid ? null : (
                        <button
                          type="button"
                          className="btn-chip"
                          onClick={() => linkAccount(user)}
                        >
                          Link account
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="muted" style={{ fontSize: 13 }}>
            No users recorded yet.
          </div>
        )}

        <div className="eyebrow" style={{ marginTop: 8 }}>
          Add someone
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
            gap: 10,
          }}
        >
          <label style={{ fontSize: 12 }}>
            Email
            <input
              className="input-field"
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </label>
          <label style={{ fontSize: 12 }}>
            Role
            <select
              className="input-field"
              value={role}
              onChange={(event) => setRole(event.target.value)}
            >
              {(users?.roles ?? ["viewer", "clerk", "approver", "admin"]).map(
                (value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ),
              )}
            </select>
          </label>
          <label style={{ fontSize: 12 }}>
            Display name
            <input
              className="input-field"
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
            />
          </label>
          <label style={{ fontSize: 12 }}>
            Identity Platform uid
            <input
              className="input-field"
              value={uid}
              placeholder="optional — link later"
              onChange={(event) => setUid(event.target.value)}
            />
          </label>
        </div>
        <div className="row-between">
          <span className="muted" style={{ fontSize: 12 }}>
            Without a uid the person is recorded but cannot sign in yet.
          </span>
          <button
            type="button"
            className="btn-primary"
            disabled={busy || !email.trim()}
            onClick={addUser}
          >
            {busy ? "Adding…" : "Add user"}
          </button>
        </div>
      </section>
    </div>
  );
}
