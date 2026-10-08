"use client";

import { Fragment, useRef, useState } from "react";
import {
  api,
  ApiError,
  can,
  type BankChange,
  type BankChangeList,
  type Me,
  type Vendor,
  type VendorList,
} from "@/lib/api";
import { usePoll } from "@/lib/hooks";

const EMPTY_FORM = {
  bank_account_name: "",
  bank_account_number: "",
  bank_code: "",
  bank_iban: "",
  reason: "",
};

function Side({ title, name, account, code, iban }: {
  title: string;
  name: string | null;
  account: string | null;
  code: string | null;
  iban: string | null;
}) {
  return (
    <div>
      <div className="eyebrow">{title}</div>
      <div style={{ fontSize: 12.5 }}>Name: {name ?? "—"}</div>
      <div style={{ fontSize: 12.5 }}>Account: {account ?? "—"}</div>
      <div style={{ fontSize: 12.5 }}>Code: {code ?? "—"}</div>
      <div style={{ fontSize: 12.5 }}>IBAN: {iban ?? "—"}</div>
    </div>
  );
}

export default function VendorsPage() {
  const { data, error, reload } = usePoll<VendorList>("/api/vendors");
  const { data: me } = usePoll<Me>("/api/me");
  // Bank-change routes answer 503 without a database. That is an expected
  // state, not a failure, so do not even ask for the queue then.
  const hasDb = data?.source === "database";
  const { data: changes, reload: reloadChanges } = usePoll<BankChangeList>(
    hasDb ? "/api/vendors/bank-changes?status=pending" : null,
  );
  const fileInput = useRef<HTMLInputElement>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [proposing, setProposing] = useState<string | null>(null); // vendor id with the form open
  const [form, setForm] = useState(EMPTY_FORM);
  const [domains, setDomains] = useState<Record<string, string>>({});

  const canPropose = can(me, "clerk") && hasDb;
  const canDecide = can(me, "approver");
  const isAdmin = can(me, "admin");
  const vendors = data?.vendors ?? [];
  const fromDatabase = data?.source === "database";

  function fail(caught: unknown) {
    setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
  }

  async function replace(file: File) {
    setNotice(null);
    setProblem(null);
    let records: unknown;
    try {
      const parsed = JSON.parse(await file.text());
      records = Array.isArray(parsed) ? parsed : parsed?.vendors;
    } catch {
      setProblem(`${file.name} is not valid JSON.`);
      return;
    }
    if (!Array.isArray(records)) {
      setProblem("Expected a list of vendors, or an object with a vendors list.");
      return;
    }
    const replacing = data?.count ?? 0;
    // The database import upserts by name; the file import rewrites the file.
    const confirmed = window.confirm(
      replacing
        ? fromDatabase
          ? `Import the ${records.length} vendors in ${file.name}?\n\n` +
            "This adds or updates suppliers; suppliers not in the file are kept."
          : `Replace all ${replacing} vendors with the ${records.length} in ${file.name}?\n\n` +
            "This replaces rather than merges, so any vendor not in the file is dropped."
        : `Load ${records.length} vendors from ${file.name}?`,
    );
    if (!confirmed) {
      if (fileInput.current) fileInput.current.value = "";
      return;
    }
    try {
      const result = await api<{ count: number }>("/api/vendors", {
        method: "PUT",
        body: JSON.stringify({ vendors: records }),
      });
      setNotice(
        fromDatabase
          ? `Loaded ${result.count} vendors. Suppliers not in the file were kept.`
          : `Loaded ${result.count} vendors. This replaced the previous set.`,
      );
      reload();
    } catch (caught) {
      fail(caught);
    } finally {
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function saveDomain(vendor: Vendor) {
    setNotice(null);
    setProblem(null);
    try {
      await api(`/api/vendors/${vendor.id}`, {
        method: "PATCH",
        body: JSON.stringify({ email_domain: domains[vendor.id] }),
      });
      setNotice(`Email domain saved for ${vendor.name}.`);
      setDomains((all) => {
        const rest = { ...all };
        delete rest[vendor.id];
        return rest;
      });
      reload();
    } catch (caught) {
      fail(caught);
    }
  }

  async function propose(vendorId: string) {
    setNotice(null);
    setProblem(null);
    try {
      await api<BankChange>(`/api/vendors/${vendorId}/bank-changes`, {
        method: "POST",
        body: JSON.stringify(form),
      });
      setNotice("Bank change proposed. A second person must approve it before it is used.");
      setProposing(null);
      setForm(EMPTY_FORM);
      reload();
      reloadChanges();
    } catch (caught) {
      fail(caught);
    }
  }

  async function decide(change: BankChange, action: "approve" | "reject") {
    const note = action === "reject" ? window.prompt("Why is this change rejected?") : null;
    if (action === "reject" && !note) return;
    if (
      !window.confirm(
        `${action === "approve" ? "Approve" : "Reject"} the bank change for ${change.vendor_name}?`,
      )
    )
      return;
    setNotice(null);
    setProblem(null);
    try {
      await api<BankChange>(`/api/vendors/bank-changes/${change.id}/decide`, {
        method: "POST",
        body: JSON.stringify({ action, note }),
      });
      setNotice(
        action === "approve"
          ? `Bank details for ${change.vendor_name} are now verified.`
          : `Change for ${change.vendor_name} rejected.`,
      );
      reload();
      reloadChanges();
    } catch (caught) {
      fail(caught);
    }
  }

  const field = (key: keyof typeof EMPTY_FORM, label: string, id?: string) => (
    <label className="stack" style={{ gap: 2, fontSize: 12.5 }}>
      {label}
      <input
        id={id}
        className="input-field"
        value={form[key]}
        onChange={(event) => setForm({ ...form, [key]: event.target.value })}
      />
    </label>
  );

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="row-between">
        <h1>Vendors</h1>
        {isAdmin ? (
          <>
            <input
              ref={fileInput}
              id="vendor-upload"
              type="file"
              accept="application/json,.json"
              className="sr-only"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) replace(file);
              }}
            />
            <label htmlFor="vendor-upload" className="btn-primary">
              Import list
            </label>
          </>
        ) : null}
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
      {error ? <div className="empty-state">{error}</div> : null}

      {changes?.count ? (
        <div className="panel stack" style={{ padding: 16, gap: 12 }}>
          <div className="eyebrow">Pending changes</div>
          {changes.changes.map((change) => {
            const mine = change.proposed_by === me?.email;
            return (
              <div key={change.id} className="stack" style={{ gap: 8 }}>
                <strong>{change.vendor_name}</strong>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                  <Side
                    title="Previous"
                    name={change.previous_account_name}
                    account={change.previous_account_number}
                    code={change.previous_code}
                    iban={change.previous_iban}
                  />
                  <Side
                    title="Proposed"
                    name={change.proposed_account_name}
                    account={change.proposed_account_number}
                    code={change.proposed_code}
                    iban={change.proposed_iban}
                  />
                </div>
                <div className="muted" style={{ fontSize: 12.5 }}>
                  Proposed by {change.proposed_by}
                  {change.created_at ? ` · ${change.created_at}` : ""}
                </div>
                <div style={{ fontSize: 12.5 }}>Reason: {change.reason}</div>
                {change.source_case_id ? (
                  <a href={`/invoices?case=${encodeURIComponent(change.source_case_id)}`} style={{ fontSize: 12.5 }}>
                    From invoice {change.source_case_id}
                  </a>
                ) : null}
                {canDecide ? (
                  <div className="row" style={{ gap: 8 }}>
                    <button
                      className="btn-primary"
                      disabled={mine}
                      title={mine ? "You proposed this change" : undefined}
                      onClick={() => decide(change, "approve")}
                    >
                      Approve
                    </button>
                    <button
                      className="btn-secondary"
                      disabled={mine}
                      title={mine ? "You proposed this change" : undefined}
                      onClick={() => decide(change, "reject")}
                    >
                      Reject
                    </button>
                    {mine ? (
                      <span className="muted" style={{ fontSize: 12.5 }}>
                        You proposed this change; another approver must decide it.
                      </span>
                    ) : null}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}

      {data && !hasDb ? (
        <span className="muted" style={{ fontSize: 12.5 }}>
          Bank-change control needs a database; this deployment lists vendors from a file.
        </span>
      ) : null}

      {vendors.length === 0 && !error ? (
        <div className="empty-state">
          No vendors loaded. Step 3.2 runs as a checksum alone and step 3.4 cannot compare
          bank details. Import your supplier list (name, tax_id, kind).
        </div>
      ) : (
        <>
          <span className="muted" style={{ fontSize: 12.5 }}>
            {data?.count} vendors · loaded from {data?.source}
          </span>
          <div className="panel table-scroll" style={{ padding: 8 }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Vendor</th>
                  <th>Tax ID</th>
                  <th>Kind</th>
                  <th>Email domain</th>
                  <th>Bank</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {vendors.map((vendor) => (
                  <Fragment key={vendor.id || vendor.name}>{vendorRows(vendor)}</Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );

  // A plain function, not a component: a component declared inside render is
  // a new type each render, so typing in the form would remount it and drop focus.
  function vendorRows(vendor: Vendor) {
    const open = proposing === vendor.id;
    return (
      <>
        <tr>
          <td>{vendor.name}</td>
          <td>{vendor.tax_id}</td>
          <td>{vendor.kind}</td>
          <td>
            {isAdmin && fromDatabase ? (
              <span className="row" style={{ gap: 6 }}>
                <input
                  className="input-field"
                  aria-label={`Email domain for ${vendor.name}`}
                  value={domains[vendor.id] ?? vendor.email_domain ?? ""}
                  onChange={(event) =>
                    setDomains({ ...domains, [vendor.id]: event.target.value })
                  }
                />
                <button
                  className="btn-secondary"
                  disabled={domains[vendor.id] === undefined}
                  onClick={() => saveDomain(vendor)}
                >
                  Save
                </button>
              </span>
            ) : (
              (vendor.email_domain ?? "—")
            )}
          </td>
          <td>
            {vendor.bank_status === "verified" ? (
              <span className="badge badge-ok">
                {vendor.bank_account_masked}
                {vendor.bank_verified_at ? ` · ${vendor.bank_verified_at.slice(0, 10)}` : ""}
              </span>
            ) : vendor.bank_status === "pending" ? (
              <span className="badge badge-warn">pending</span>
            ) : (
              <span className="badge badge-neutral">unverified</span>
            )}
          </td>
          <td>
            {canPropose && vendor.bank_status !== "pending" ? (
              <button
                className="btn-secondary"
                onClick={() => {
                  setForm(EMPTY_FORM);
                  setProposing(open ? null : vendor.id);
                }}
              >
                Request bank change
              </button>
            ) : null}
          </td>
        </tr>
        {open ? (
          <tr>
            <td colSpan={6}>
              <div className="stack" style={{ gap: 8 }}>
                {field("bank_account_name", "Account name")}
                {field("bank_account_number", "Account number", "bank-account-number")}
                {field("bank_code", "Bank code")}
                {field("bank_iban", "IBAN")}
                {field("reason", "Reason", "bank-reason")}
                <div className="row" style={{ gap: 8 }}>
                  <button className="btn-primary" onClick={() => propose(vendor.id)}>
                    Propose
                  </button>
                  <button className="btn-secondary" onClick={() => setProposing(null)}>
                    Cancel
                  </button>
                </div>
              </div>
            </td>
          </tr>
        ) : null}
      </>
    );
  }
}
