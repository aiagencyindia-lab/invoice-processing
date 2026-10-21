"use client";

import type { Dimension, TransactionTrust } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { DIMENSION_LABELS } from "@/lib/trust";
import { DimensionMeter, percent } from "./DimensionMeter";

/** The Transaction Trust Score: six dimensions, each able to hold payment on
 *  its own, and a composite that is advisory only and never holds anything. */
export function TrustPanel({ node }: { node: string | null | undefined }) {
  const { data, error } = usePoll<TransactionTrust>(
    node ? `/api/trust/transactions/${encodeURIComponent(node)}/trust` : null,
  );
  return (
    <div className="panel stack" style={{ padding: 16, gap: 10 }} aria-label="Trust score">
      <div className="row-between">
        <div className="eyebrow">Trust score</div>
        {data && !error ? (
          <span className="muted" style={{ fontSize: 12 }}>
            {percent(data.composite)} composite · advisory
          </span>
        ) : null}
      </div>
      {!node ? (
        <span className="muted" style={{ fontSize: 12.5 }}>
          Not scored yet.
        </span>
      ) : error ? (
        <span className="muted" style={{ fontSize: 12.5 }}>
          {error}
        </span>
      ) : !data ? (
        <span className="muted" style={{ fontSize: 12.5 }}>
          Loading…
        </span>
      ) : (
        <>
          {(Object.keys(DIMENSION_LABELS) as Dimension[]).map((name) =>
            data.dimensions[name] ? <DimensionMeter key={name} name={name} score={data.dimensions[name]} /> : null,
          )}
          <span className="faint" style={{ fontSize: 11.5 }}>
            {data.score_version} · {new Date(data.computed_at).toLocaleString()}
          </span>
        </>
      )}
    </div>
  );
}
