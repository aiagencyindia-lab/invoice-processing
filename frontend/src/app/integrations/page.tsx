"use client";

import { ApiKeysPanel } from "@/components/trust/ApiKeysPanel";
import { WebhooksPanel } from "@/components/trust/WebhooksPanel";
import { can, type Me } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

/** Machine access to the trust layer: /v2 API keys and outbound webhooks. */
export default function IntegrationsPage() {
  const { data: me } = usePoll<Me>("/api/me");
  return (
    <section className="stack" style={{ gap: 16 }}>
      <h1>Integrations</h1>
      {!me ? (
        <div className="empty-state">Loading…</div>
      ) : !can(me, "admin") ? (
        <div className="empty-state">Integrations are for administrators.</div>
      ) : (
        <>
          <ApiKeysPanel />
          <WebhooksPanel />
        </>
      )}
    </section>
  );
}
