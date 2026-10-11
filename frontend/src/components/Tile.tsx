import Link from "next/link";

export function Tile({
  label,
  value,
  hint,
  href,
  testId,
}: {
  label: string;
  value: string;
  hint?: string;
  href?: string;
  testId?: string;
}) {
  const body = (
    <div className="panel" style={{ padding: 18, height: "100%" }} data-tile={testId}>
      <div className="eyebrow">{label}</div>
      <div className="tile-value" style={{ font: "600 26px var(--font-ui)", margin: "6px 0 2px" }}>
        {value}
      </div>
      {hint ? (
        <div className="muted" style={{ fontSize: 12 }}>
          {hint}
        </div>
      ) : null}
    </div>
  );
  return href ? (
    <Link href={href} style={{ textDecoration: "none", color: "inherit" }}>
      {body}
    </Link>
  ) : (
    body
  );
}
