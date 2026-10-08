"use client";

import { useState } from "react";
import { api, ApiError, type Delegation, type Me } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

/** Delegation form + list. Shared with /admin/approvals, where an admin may
 *  name any delegator; here the delegator is always the signed-in person. */
export function DelegationsPanel({
  me,
  fixedDelegator,
}: {
  me: Me | null;
  fixedDelegator: boolean;
}) {
  const { data, error, reload } = usePoll<{ delegations: Delegation[] }>(
    "/api/approvals/delegations",
  );
  const today = new Date().toISOString().slice(0, 10);
  const [delegator, setDelegator] = useState("");
  const [delegate, setDelegate] = useState("");
  const [from, setFrom] = useState(today);
  const [to, setTo] = useState(today);
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  async function add() {
    setNotice(null);
    setProblem(null);
    try {
      await api("/api/approvals/delegations", {
        method: "POST",
        body: JSON.stringify({
          delegator_email: fixedDelegator ? me?.email : delegator,
          delegate_email: delegate,
          starts_on: from,
          ends_on: to,
        }),
      });
      setNotice(`${delegate} will cover for you from ${from} to ${to}.`);
      setDelegate("");
      reload();
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    }
  }

  async function remove(id: string) {
    setNotice(null);
    setProblem(null);
    try {
      await api(`/api/approvals/delegations/${id}`, { method: "DELETE" });
      reload();
    } catch (caught) {
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    }
  }

  const noDb = error?.includes("needs a database");
  return (
    <section className="panel stack" style={{ gap: 10 }}>
      <div className="eyebrow">Out of office</div>
      {noDb ? (
        <span className="muted" style={{ fontSize: 12.5 }}>
          {error}
        </span>
      ) : (
        <>
          {problem ? (
            <div role="alert" style={{ color: "var(--error)", fontSize: 13 }}>
              {problem}
            </div>
          ) : null}
          {notice ? (
            <div role="status" style={{ fontSize: 13 }}>
              {notice}
            </div>
          ) : null}
          {(data?.delegations ?? []).map((d) => (
            <div key={d.id} className="row-between" style={{ fontSize: 13 }}>
              <span>
                {d.delegator_email} → {d.delegate_email}, {d.starts_on} to{" "}
                {d.ends_on}
              </span>
              <button
                type="button"
                className="btn-chip"
                onClick={() => remove(d.id)}
              >
                Delete
              </button>
            </div>
          ))}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
              gap: 10,
            }}
          >
            {fixedDelegator ? null : (
              <label style={{ fontSize: 12 }}>
                Delegator email
                <input
                  className="input-field"
                  type="email"
                  value={delegator}
                  onChange={(event) => setDelegator(event.target.value)}
                />
              </label>
            )}
            <label style={{ fontSize: 12 }}>
              Delegate email
              <input
                className="input-field"
                type="email"
                value={delegate}
                onChange={(event) => setDelegate(event.target.value)}
              />
            </label>
            <label style={{ fontSize: 12 }}>
              From
              <input
                className="input-field"
                type="date"
                value={from}
                onChange={(event) => setFrom(event.target.value)}
              />
            </label>
            <label style={{ fontSize: 12 }}>
              To
              <input
                className="input-field"
                type="date"
                value={to}
                onChange={(event) => setTo(event.target.value)}
              />
            </label>
          </div>
          <div>
            <button
              type="button"
              className="btn-primary"
              disabled={!delegate.trim() || (!fixedDelegator && !delegator.trim())}
              onClick={add}
            >
              Add delegation
            </button>
          </div>
        </>
      )}
    </section>
  );
}
