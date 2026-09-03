"use client";

import { useState } from "react";
import { api, ApiError, can, type Me, type RuleProposal } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

/** Rules — where a rejection becomes policy, and a person says yes.
 *
 *  This is what replaces the chat agent. Teaching the system used to mean
 *  running an ADK session on a laptop, and the rules it wrote went into a file
 *  nothing read. Here the loop closes in the product: an overruled decision
 *  becomes a proposal, a rule is drafted from the reviewer's own words against
 *  the tenant's own cases, and approving it makes it live.
 *
 *  The screen's real job is the approval. A rule changes what gets paid, so
 *  what it fires on and what else it touches have to be readable before
 *  anybody can agree to it.
 */

// Drafting is several Vertex calls, so the page has to come alive on its own
// when one lands. Only while something is actually in flight.
const BUSY = new Set(["drafting"]);

function condition(c: { field: string; operator: string; value: unknown }) {
  return `${c.field} ${c.operator.replace(/_/g, " ")} ${JSON.stringify(c.value)}`;
}

export default function RulesPage() {
  const { data: me } = usePoll<Me>("/api/me");
  const { data, error, reload } = usePoll<{ proposals: RuleProposal[] }>(
    "/api/rules/proposals",
    5000,
  );
  const { data: live, reload: reloadLive } = usePoll<{
    rules: { id: string; name: string; enabled: boolean }[];
  }>("/api/rules");
  const [notice, setNotice] = useState<string | null>(null);

  const proposals = data?.proposals ?? [];
  const act = async (path: string, body?: object) => {
    setNotice(null);
    try {
      await api(path, {
        method: "POST",
        body: body ? JSON.stringify(body) : undefined,
      });
      reload();
      reloadLive();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        const detail = caught.detail as { message?: string } | undefined;
        setNotice(detail?.message ?? caught.friendly);
      } else {
        setNotice(caught instanceof ApiError ? caught.friendly : String(caught));
      }
    }
  };

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="row-between">
        <h1>Rules</h1>
        <span className="muted" style={{ fontSize: 12.5 }}>
          {proposals.length} proposed · {live?.rules.length ?? 0} in force
        </span>
      </div>

      {notice ? (
        <div className="banner" role="alert">
          {notice}
        </div>
      ) : null}
      {error ? <div className="empty-state">{error}</div> : null}

      {!proposals.length && !error ? (
        <div className="empty-state">
          Nothing proposed. A rule starts life as a rejection: when a reviewer
          overrules the pipeline and records the reason as a policy exception,
          it appears here.
        </div>
      ) : null}

      {proposals.map((proposal) => (
        <section key={proposal.rule_id} className="panel" style={{ padding: 16 }}>
          <div className="row-between">
            <div>
              <div className="eyebrow">{proposal.rule_id}</div>
              <h2 style={{ margin: "2px 0" }}>{proposal.name}</h2>
            </div>
            <span
              className={`badge ${
                proposal.status === "failed" ? "badge-error" : "badge-neutral"
              }`}
            >
              {proposal.status === "pending_draft"
                ? "not drafted"
                : proposal.status}
            </span>
          </div>

          <p className="muted" style={{ marginBottom: 4 }}>
            From{" "}
            <a href={`/invoices?case=${proposal.source_case_id}`}>
              {proposal.source_case_id}
            </a>
            , overruled by {proposal.author}
          </p>
          {proposal.note ? (
            <blockquote className="evidence" style={{ margin: "0 0 10px" }}>
              “{proposal.note}”
            </blockquote>
          ) : null}

          {proposal.conditions.length ? (
            <div style={{ marginBottom: 10 }}>
              <div className="eyebrow">Fires when all of these hold</div>
              <ul className="stack" style={{ gap: 2, paddingLeft: 18 }}>
                {proposal.conditions.map((c, index) => (
                  <li key={index}>
                    <code>{condition(c)}</code>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {proposal.impact ? (
            <div style={{ marginBottom: 10 }}>
              <div className="eyebrow">What else it touches</div>
              <p className="muted" style={{ margin: 0 }}>
                {proposal.impact.summary ??
                  `${proposal.impact.collateral_matches?.length ?? 0} other cases match`}
                {proposal.impact.sampled
                  ? ` (measured against ${proposal.impact.sample_size} of ${proposal.impact.total_cases} cases)`
                  : proposal.impact.total_cases
                    ? ` (measured against all ${proposal.impact.total_cases} cases)`
                    : ""}
              </p>
            </div>
          ) : null}

          {proposal.has_collateral ? (
            <div className="banner" role="status">
              {proposal.collateral_warning ??
                "This rule also matches cases it was not written for."}{" "}
              Approving it will re-decide them too.
            </div>
          ) : null}
          {proposal.llm_actions?.length ? (
            <div className="banner" role="status">
              This rule calls a model ({proposal.llm_actions.join(", ")}) every
              time it matches an invoice — minutes of processing, a cost per
              invoice, and no guarantee of the same answer twice. A rule that
              sets fields directly is instant, free and reproducible; ask for a
              redraft unless this one genuinely has to recompute downstream
              fields.
            </div>
          ) : null}
          {proposal.error ? (
            <div className="banner" role="alert">
              {proposal.error}
            </div>
          ) : null}

          <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
            {proposal.status !== "draft" ? (
              <button
                type="button"
                className="btn-secondary"
                disabled={BUSY.has(proposal.status)}
                onClick={() =>
                  act(`/api/rules/proposals/${proposal.rule_id}/draft`)
                }
              >
                {proposal.status === "drafting"
                  ? "Writing the rule…"
                  : proposal.status === "failed"
                    ? "Try again"
                    : "Draft a rule"}
              </button>
            ) : null}
            {proposal.status === "draft" && can(me, "admin") ? (
              <>
                <button
                  type="button"
                  className="btn-primary"
                  onClick={() =>
                    act(`/api/rules/proposals/${proposal.rule_id}/decision`, {
                      decision: "approve",
                      // Never silently: the confirm is the acknowledgement the
                      // API refuses to proceed without.
                      accept_collateral: proposal.has_collateral
                        ? window.confirm(
                            `${proposal.collateral_warning ?? "This rule matches other cases."}\n\nApprove it anyway?`,
                          )
                        : false,
                    })
                  }
                >
                  Approve
                </button>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() =>
                    act(`/api/rules/proposals/${proposal.rule_id}/decision`, {
                      decision: "reject",
                    })
                  }
                >
                  Reject
                </button>
              </>
            ) : null}
          </div>
        </section>
      ))}

      {live?.rules.length ? (
        <section className="panel" style={{ padding: 12 }}>
          <div className="eyebrow">In force</div>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Rule</th>
                  <th>Name</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {live.rules.map((rule) => (
                  <tr key={rule.id}>
                    <td>{rule.id}</td>
                    <td>{rule.name}</td>
                    <td>
                      {can(me, "admin") ? (
                        <button
                          type="button"
                          className="btn-chip"
                          onClick={() => act(`/api/rules/${rule.id}/disable`)}
                        >
                          Turn off
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}
    </div>
  );
}
