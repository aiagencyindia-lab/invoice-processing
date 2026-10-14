"use client";

import Link from "next/link";
import { useState } from "react";
import {
  api,
  ApiError,
  type Me,
  type SsoDomain,
  type SsoDomainList,
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

  const { data: me } = usePoll<Me>("/api/me");
  const {
    data: domains,
    error: domainsError,
    reload: reloadDomains,
  } = usePoll<SsoDomainList>("/api/admin/sso-domains");
  const [newDomain, setNewDomain] = useState("");
  const [newRole, setNewRole] = useState<"viewer" | "clerk">("viewer");

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

  function fail(caught: unknown) {
    setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
  }

  async function addDomain() {
    setNotice(null);
    setProblem(null);
    try {
      const record = await api<SsoDomain>("/api/admin/sso-domains", {
        method: "POST",
        body: JSON.stringify({
          domain: newDomain.trim(),
          // Microsoft bindings are made by Ledgerly support, not here.
          provider: "google",
          default_role: newRole,
        }),
      });
      setNotice(
        `${record.domain} connected. People at that domain can sign in as ${record.default_role}.`,
      );
      setNewDomain("");
      reloadDomains();
    } catch (caught) {
      fail(caught);
    }
  }

  async function removeDomain(d: SsoDomain) {
    if (!window.confirm(`Disconnect ${d.domain}? People already added keep their access.`)) return;
    setNotice(null);
    setProblem(null);
    try {
      await api(`/api/admin/sso-domains/${d.id}`, { method: "DELETE" });
      setNotice(`${d.domain} disconnected.`);
      reloadDomains();
    } catch (caught) {
      fail(caught);
    }
  }

  async function patchUser(target: User, body: object, done: string) {
    setNotice(null);
    setProblem(null);
    try {
      await api(`/api/admin/users/${target.id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });
      setNotice(done);
      reload();
    } catch (caught) {
      fail(caught);
    }
  }

  function setActive(target: User, active: boolean) {
    if (!active && !window.confirm(`Deactivate ${target.email}? They will be refused on their next request.`)) return;
    patchUser(
      target,
      { is_active: active },
      active
        ? `${target.email} can sign in again.`
        : `${target.email} can no longer sign in.`,
    );
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
        <span style={{ display: "flex", gap: 16 }}>
          <Link href="/admin/approvals">Approval setup</Link>
          <Link href="/admin/gl-rules">GL rules</Link>
        </span>
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
        <div className="eyebrow">Single sign-on</div>
        <div className="muted" style={{ fontSize: 12.5 }}>
          People with a Google Workspace account at a connected domain can sign
          in straight away and are given the default role.
        </div>
        {domainsError ? <div className="empty-state">{domainsError}</div> : null}
        {domains && domains.domains.length > 0 ? (
          <table className="data-table">
            <thead>
              <tr><th>Domain</th><th>Provider</th><th>Default role</th><th /></tr>
            </thead>
            <tbody>
              {domains.domains.map((d) => (
                <tr key={d.id}>
                  <td>{d.domain}</td>
                  <td>
                    {d.provider === "microsoft"
                      ? "Microsoft 365 — set up by Ledgerly support"
                      : "Google Workspace"}
                  </td>
                  <td><span className="badge">{d.default_role}</span></td>
                  <td>
                    <button type="button" className="btn-chip" aria-label={`Disconnect ${d.domain}`}
                            onClick={() => removeDomain(d)}>
                      Disconnect
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="muted" style={{ fontSize: 13 }}>No domains connected.</div>
        )}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 10 }}>
          <label style={{ fontSize: 12 }}>
            Domain
            <input className="input-field" aria-label="Domain" value={newDomain}
                   placeholder="acme.com" onChange={(e) => setNewDomain(e.target.value)} />
          </label>
          <label style={{ fontSize: 12 }}>
            Provider
            <select className="input-field" aria-label="Provider" value="google" disabled>
              <option value="google">Google Workspace</option>
            </select>
          </label>
          <label style={{ fontSize: 12 }}>
            Default role
            <select className="input-field" aria-label="Default role" value={newRole}
                    onChange={(e) => setNewRole(e.target.value as "viewer" | "clerk")}>
              <option value="viewer">viewer</option>
              <option value="clerk">clerk</option>
            </select>
          </label>
        </div>
        <div className="row-between">
          <span className="muted" style={{ fontSize: 12 }}>
            Public mailbox domains such as gmail.com cannot be connected.
          </span>
          <button type="button" className="btn-primary" disabled={!newDomain.trim()} onClick={addDomain}>
            Connect domain
          </button>
        </div>
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
                      <select
                        className="input-field"
                        aria-label={`Role of ${user.email}`}
                        value={user.role}
                        // Until /api/me answers, any row could be the caller's own.
                        disabled={!me || user.email === me.email}
                        onChange={(event) => {
                          const role = event.target.value;
                          if (
                            role === "admin" &&
                            !window.confirm(`Make ${user.email} an admin? They can then manage everyone here.`)
                          )
                            return;
                          patchUser(
                            user,
                            { role },
                            `${user.email} is now ${role}; it applies on their next request.`,
                          );
                        }}
                      >
                        {(users?.roles ?? ["viewer", "clerk", "approver", "admin"]).map(
                          (value) => (
                            <option key={value} value={value}>
                              {value}
                            </option>
                          ),
                        )}
                      </select>
                    </td>
                    <td>
                      {user.is_active === false ? (
                        <span className="badge badge-warn">deactivated</span>
                      ) : user.uid ? (
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
                      {!me || user.email === me.email ? null : (
                        <button
                          type="button"
                          className="btn-chip"
                          aria-label={`${user.is_active === false ? "Reactivate" : "Deactivate"} ${user.email}`}
                          onClick={() => setActive(user, user.is_active === false)}
                        >
                          {user.is_active === false ? "Reactivate" : "Deactivate"}
                        </button>
                      )}
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
