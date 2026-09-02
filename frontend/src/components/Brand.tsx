/** The Ledgerly mark, drawn rather than loaded.
 *
 *  Inline SVG for three reasons: it stays sharp at every size, it costs no
 *  request, and the monochrome variants come free by drawing in `currentColor`
 *  -- so the same component works on the light sidebar, on a dark card and in
 *  a print stylesheet without three files to keep in step.
 *
 *  The monogram is an L and an E sharing a stem: the blue stroke draws the L,
 *  and the three bars to its right make the E. Geometry is deliberately
 *  simple -- one stroked path and three rounded rectangles -- because the mark
 *  has to survive being rendered at 20 pixels in a sidebar.
 */

const BLUE = "#2563EB";
const INK = "#101828";

export function BrandMark({
  size = 28,
  tone = "colour",
}: {
  size?: number;
  /** `mono` draws everything in `currentColor`, for dark grounds and print. */
  tone?: "colour" | "mono";
}) {
  const blue = tone === "mono" ? "currentColor" : BLUE;
  const ink = tone === "mono" ? "currentColor" : INK;

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 120 120"
      fill="none"
      role="img"
      aria-label="Ledgerly"
    >
      {/* The L: one stroke, so the corner is a single round join rather than
          two rectangles meeting and showing a seam at small sizes. */}
      <path
        d="M24 16 V70 a14 14 0 0 0 14 14 H100"
        stroke={blue}
        strokeWidth="20"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* The E's arms. The last one is blue so the eye reads L and E as one
          object rather than a dark glyph sitting beside a blue one. */}
      <rect x="50" y="16" width="52" height="14" rx="7" fill={ink} />
      <rect x="50" y="41" width="52" height="14" rx="7" fill={ink} />
      <rect x="50" y="66" width="58" height="14" rx="7" fill={blue} />
    </svg>
  );
}

/** Mark, name and the line that says who made it.
 *
 *  "by AIxAI" is set small and quiet on purpose: it is attribution, not a
 *  second brand competing with the first for the same glance.
 */
export function BrandLockup({
  size = 28,
  tone = "colour",
  align = "left",
}: {
  size?: number;
  tone?: "colour" | "mono";
  align?: "left" | "center";
}) {
  return (
    <span
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        justifyContent: align === "center" ? "center" : "flex-start",
      }}
    >
      <BrandMark size={size} tone={tone} />
      <span style={{ display: "flex", flexDirection: "column", lineHeight: 1.05 }}>
        <span
          style={{
            font: `600 ${Math.round(size * 0.62)}px var(--font-ui)`,
            letterSpacing: "-0.02em",
            color: tone === "mono" ? "currentColor" : "var(--ink)",
          }}
        >
          Ledgerly
        </span>
        <span
          style={{
            font: `500 ${Math.max(9, Math.round(size * 0.3))}px var(--font-ui)`,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            color: tone === "mono" ? "currentColor" : "var(--text-faint)",
            opacity: tone === "mono" ? 0.75 : 1,
          }}
        >
          by AIxAI
        </span>
      </span>
    </span>
  );
}
