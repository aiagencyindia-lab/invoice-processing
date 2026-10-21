"use client";

import { useState } from "react";
import { ReadinessTable } from "@/components/trust/ReadinessTable";
import { can, type Me, type Readiness } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

/** Engine readiness: what each engine has found in its current mode, and
 *  whether the evidence supports moving it. The operator decides. */
export default function TrustEnginesPage() {
  const { data: me } = usePoll<Me>("/api/me");
  const { data, error, reload } = usePoll<Readiness>("/api/trust/readiness");
  const [notice, setNotice] = useState<string | null>(null);

  return (
    <section className="stack" style={{ gap: 16 }}>
      <h1>Trust engines</h1>
      <p className="muted" style={{ margin: 0 }}>
        Engines move off → shadow → advisory → enforcing only when an administrator moves them. Each row shows the
        evidence for the next step.
      </p>
      {!me ? (
        <div className="empty-state">Loading…</div>
      ) : !can(me, "admin") ? (
        <div className="empty-state">Engine modes are for administrators.</div>
      ) : error ? (
        <div className="empty-state" role="alert">
          {error}
        </div>
      ) : !data ? (
        <div className="empty-state">Loading…</div>
      ) : (
        <ReadinessTable
          engines={data.engines}
          onChanged={(message) => {
            setNotice(message);
            reload();
          }}
        />
      )}
      {notice ? (
        <div className="toast" role="status">
          {notice}
        </div>
      ) : null}
    </section>
  );
}
