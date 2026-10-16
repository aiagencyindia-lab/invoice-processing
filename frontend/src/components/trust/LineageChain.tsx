import type { LineageLink } from "@/lib/api";
import { STAGE_LABELS } from "@/lib/trust";

// The seven stages of trust/graph/lineage.CHAIN, in order; a stage the API
// did not report still gets a cell, so the chain never looks shorter than it is.
const CHAIN = ["contract", "po", "grn", "invoice", "approval", "payment", "gl"];
const GLYPH: Record<LineageLink["status"], string> = { found: "✓", no_evidence: "○", contradicts: "✗" };

export function LineageChain({ links }: { links: LineageLink[] }) {
  return (
    <ol className="chain" aria-label="Lineage">
      {CHAIN.map((stage) => {
        const link = links.find((l) => l.stage === stage) ?? {
          stage,
          status: "no_evidence" as const,
          node_id: null,
          note: "Not reported.",
        };
        return (
          <li key={stage} className={link.status} data-stage={stage}>
            <div className="eyebrow">{STAGE_LABELS[stage] ?? stage}</div>
            <div>
              <span aria-hidden="true">{GLYPH[link.status]} </span>
              <span className={`badge ${link.status === "found" ? "badge-ok" : link.status === "contradicts" ? "badge-error" : "badge-warn"}`}>
                {link.status.replace("_", " ")}
              </span>
            </div>
            <div className="muted" style={{ fontSize: 12 }}>
              {link.note}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
