"use client";

import { useState } from "react";
import {
  api,
  ApiError,
  can,
  type ApprovalRule,
  type ApproverGroup,
  type Me,
} from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { DelegationsPanel } from "@/components/DelegationsPanel";

type StepRow = { kind: string; value: string };

function matchSummary(rule: ApprovalRule): string {
  const m = rule.match;
  if (rule.kind === "vendor") return `vendor ${m.vendor_name}`;
  if (rule.kind === "entity") return `organisation ${m.organization_name}`;
  if (rule.kind === "gl") return `GL starting ${m.gl_prefix}`;
  return `at least ${m.min_amount}${m.currency ? ` ${m.currency}` : ""}`;
}

export default function ApprovalSetupPage() {
  const { data: me } = usePoll<Me>("/api/me");
  const groups = usePoll<{ groups: ApproverGroup[] }>("/api/approvals/groups");
  const rules = usePoll<{ rules: ApprovalRule[] }>("/api/approvals/rules");

  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const [groupName, setGroupName] = useState("");
  const [members, setMembers] = useState("");

  const [ruleName, setRuleName] = useState("");
  const [kind, setKind] = useState("amount");
  const [vendorName, setVendorName] = useState("");
  const [orgName, setOrgName] = useState("");
  const [glPrefix, setGlPrefix] = useState("");
  const [minAmount, setMinAmount] = useState("");
  const [currency, setCurrency] = useState("");
  const [steps, setSteps] = useState<StepRow[]>([{ kind: "user", value: "" }]);
  const [enabled, setEnabled] = useState(true);

  function fail(caught: unknown) {
    setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
  }

  async function send(
    path: string,
    method: "POST" | "DELETE",
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
    } catch (caught) {
      fail(caught);
    }
  }

  function addGroup() {
    send(
      "/api/approvals/groups",
      "POST",
      {
        name: groupName,
        members: members.split("\n").map((m) => m.trim()).filter(Boolean),
      },
      `Group ${groupName} created.`,
      () => {
        setGroupName("");
        setMembers("");
        groups.reload();
      },
    );
  }

  function addRule() {
    const match =
      kind === "vendor"
        ? { vendor_name: vendorName }
        : kind === "entity"
          ? { organization_name: orgName }
          : {
              min_amount: Number(minAmount),
              ...(currency.trim() ? { currency: currency.trim().toUpperCase() } : {}),
            };
    send(
      "/api/approvals/rules",
      "POST",
      { name: ruleName, kind, match, steps, enabled },
      `Rule ${ruleName} created.`,
      () => {
        setRuleName("");
        setVendorName("");
        setOrgName("");
        setGlPrefix("");
        setMinAmount("");
        setCurrency("");
        setSteps([{ kind: "user", value: "" }]);
        rules.reload();
      },
    );
  }

  function setStep(i: number, patch: Partial<StepRow>) {
    setSteps(steps.map((s, at) => (at === i ? { ...s, ...patch } : s)));
  }

  if (me && !can(me, "clerk")) {
    return (
      <div className="stack" style={{ gap: 16 }}>
        <h1>Approval setup</h1>
        <div className="empty-state">Your role does not allow this.</div>
      </div>
    );
  }
  const isAdmin = can(me, "admin");
  const noDb = groups.error?.includes("needs a database");

  return (
    <div className="stack" style={{ gap: 20, maxWidth: 860 }}>
      <h1>Approval setup</h1>

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
          {groups.error}
        </span>
      ) : (
        <>
          <section className="panel stack" style={{ gap: 10 }}>
            <div className="eyebrow">Approver groups</div>
            {(groups.data?.groups ?? []).map((g) => (
              <div key={g.id} className="row-between" style={{ fontSize: 13 }}>
                <span>
                  <strong>{g.name}</strong>: {g.members.join(", ")}
                </span>
                {isAdmin ? (
                  <button
                    type="button"
                    className="btn-chip"
                    onClick={() =>
                      send(`/api/approvals/groups/${g.id}`, "DELETE", undefined, `Group ${g.name} deleted.`, groups.reload)
                    }
                  >
                    Delete
                  </button>
                ) : null}
              </div>
            ))}
            {isAdmin ? (
              <>
                <label style={{ fontSize: 12 }}>
                  Group name
                  <input
                    className="input-field"
                    value={groupName}
                    onChange={(event) => setGroupName(event.target.value)}
                  />
                </label>
                <label style={{ fontSize: 12 }}>
                  Members, one email per line
                  <textarea
                    className="input-field"
                    rows={3}
                    value={members}
                    onChange={(event) => setMembers(event.target.value)}
                  />
                </label>
                <div>
                  <button
                    type="button"
                    className="btn-primary"
                    disabled={!groupName.trim() || !members.trim()}
                    onClick={addGroup}
                  >
                    Add group
                  </button>
                </div>
              </>
            ) : null}
          </section>

          <section className="panel stack" style={{ gap: 10 }}>
            <div className="eyebrow">Approval rules</div>
            {(rules.data?.rules ?? []).map((r) => (
              <div key={r.id} className="row-between" style={{ fontSize: 13 }}>
                <span>
                  <strong>{r.name}</strong> · {r.kind} · {matchSummary(r)} ·{" "}
                  {r.steps.map((s) => `${s.kind}:${s.value}`).join(" → ")}
                  {r.enabled ? "" : " (off)"}
                </span>
                {isAdmin ? (
                  <button
                    type="button"
                    className="btn-chip"
                    onClick={() =>
                      send(`/api/approvals/rules/${r.id}`, "DELETE", undefined, `Rule ${r.name} deleted.`, rules.reload)
                    }
                  >
                    Delete
                  </button>
                ) : null}
              </div>
            ))}
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
                    Rule name
                    <input
                      className="input-field"
                      value={ruleName}
                      onChange={(event) => setRuleName(event.target.value)}
                    />
                  </label>
                  <label style={{ fontSize: 12 }}>
                    Kind
                    <select
                      className="input-field"
                      value={kind}
                      onChange={(event) => setKind(event.target.value)}
                    >
                      <option value="amount">amount</option>
                      <option value="vendor">vendor</option>
                      <option value="entity">entity</option>
                      <option value="gl">gl</option>
                    </select>
                  </label>
                  {kind === "vendor" ? (
                    <label style={{ fontSize: 12 }}>
                      Vendor name
                      <input
                        className="input-field"
                        value={vendorName}
                        onChange={(event) => setVendorName(event.target.value)}
                      />
                    </label>
                  ) : kind === "entity" ? (
                    <label style={{ fontSize: 12 }}>
                      Organisation name
                      <input
                        className="input-field"
                        value={orgName}
                        onChange={(event) => setOrgName(event.target.value)}
                      />
                    </label>
                  ) : kind === "gl" ? (
                    <label style={{ fontSize: 12 }}>
                      GL prefix
                      <input
                        className="input-field"
                        maxLength={64}
                        value={glPrefix}
                        onChange={(event) => setGlPrefix(event.target.value)}
                      />
                    </label>
                  ) : (
                    <>
                      <label style={{ fontSize: 12 }}>
                        Minimum amount
                        <input
                          className="input-field"
                          type="number"
                          min="0"
                          value={minAmount}
                          onChange={(event) => setMinAmount(event.target.value)}
                        />
                      </label>
                      <label style={{ fontSize: 12 }}>
                        Currency (optional)
                        <input
                          className="input-field"
                          maxLength={3}
                          value={currency}
                          onChange={(event) => setCurrency(event.target.value)}
                        />
                      </label>
                    </>
                  )}
                </div>
                <div className="eyebrow">Steps, in order</div>
                {steps.map((step, i) => (
                  <div key={i} style={{ display: "flex", gap: 8 }}>
                    <select
                      className="input-field"
                      aria-label={`Step ${i + 1} kind`}
                      value={step.kind}
                      onChange={(event) => setStep(i, { kind: event.target.value })}
                    >
                      <option value="user">user</option>
                      <option value="group">group</option>
                      <option value="role">role</option>
                    </select>
                    <input
                      className="input-field"
                      aria-label={`Step ${i + 1} value`}
                      value={step.value}
                      onChange={(event) => setStep(i, { value: event.target.value })}
                    />
                    <button
                      type="button"
                      className="btn-chip"
                      disabled={steps.length === 1}
                      onClick={() => setSteps(steps.filter((_, at) => at !== i))}
                    >
                      Remove
                    </button>
                  </div>
                ))}
                <div>
                  <button
                    type="button"
                    className="btn-chip"
                    onClick={() => setSteps([...steps, { kind: "user", value: "" }])}
                  >
                    Add step
                  </button>
                </div>
                <label style={{ fontSize: 12 }}>
                  <input
                    type="checkbox"
                    checked={enabled}
                    onChange={(event) => setEnabled(event.target.checked)}
                  />{" "}
                  Enabled
                </label>
                <div>
                  <button
                    type="button"
                    className="btn-primary"
                    disabled={
                      !ruleName.trim() ||
                      steps.some((s) => !s.value.trim()) ||
                      !(kind === "vendor"
                        ? vendorName.trim()
                        : kind === "entity"
                          ? orgName.trim()
                          : kind === "gl"
                            ? glPrefix.trim()
                          : minAmount.trim())
                    }
                    onClick={addRule}
                  >
                    Add rule
                  </button>
                </div>
              </>
            ) : null}
          </section>

          {isAdmin ? <DelegationsPanel me={me} fixedDelegator={false} /> : null}
        </>
      )}
    </div>
  );
}
