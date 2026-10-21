import Link from "next/link";
import type { DimensionScore } from "@/lib/api";
import { DIMENSION_LABELS } from "@/lib/trust";

export function percent(value: number | null | undefined): string {
  // null is "nothing to judge it by", which is not 0%.
  return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}

/** One Trust Score dimension: a 0–1 meter, whether it holds payment, the
 *  findings that drive it and the engine's own sentence. `score` may be a bare
 *  value (the vendor profile's means carry no drivers). */
export function DimensionMeter({ name, score }: { name: string; score: DimensionScore | number | null }) {
  const full = typeof score === "object" && score !== null ? score : null;
  const value = full ? full.value : (score as number | null);
  const label = DIMENSION_LABELS[name] ?? name;
  return (
    <div className="stack" style={{ gap: 2 }} data-dimension={name}>
      <div className="row-between" style={{ gap: 8 }}>
        <span style={{ fontSize: 12.5, fontWeight: 600 }}>{label}</span>
        <span className="row" style={{ gap: 6, alignItems: "center" }}>
          {full?.hold ? <span className="badge badge-warn">hold</span> : null}
          <span style={{ fontSize: 12.5, fontVariantNumeric: "tabular-nums" }}>{percent(value)}</span>
        </span>
      </div>
      {value === null || value === undefined ? null : (
        <meter min={0} max={1} low={0.5} high={0.8} optimum={1} value={value} aria-label={label} style={{ width: "100%" }} />
      )}
      {full?.note ? (
        <span className="muted" style={{ fontSize: 12 }}>
          {full.note}
        </span>
      ) : null}
      {full?.drivers.length ? (
        <span className="chip-row" style={{ fontSize: 12 }}>
          {full.drivers.map((id, index) => (
            <Link key={id} href={`/findings/${encodeURIComponent(id)}`}>
              Finding {index + 1} ›
            </Link>
          ))}
        </span>
      ) : null}
    </div>
  );
}
