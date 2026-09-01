"use client";

import Link from "next/link";

/** The shell both auth pages sit in: one centred card, a wordmark, a heading
 *  and a single line of explanation. Matches on-brand-genmedia's login card,
 *  minus its email/password form -- this service authenticates by Identity
 *  Platform token only, so a password field would be a control with nothing
 *  behind it.
 */
export function AuthCard({
  heading,
  blurb,
  children,
  footer,
}: {
  heading: string;
  blurb: string;
  children: React.ReactNode;
  footer?: { text: string; href: string; label: string };
}) {
  return (
    <main
      className="animate-fade-in"
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "40px 24px",
      }}
    >
      <div style={{ width: 400, maxWidth: "100%" }}>
        <div
          style={{
            textAlign: "center",
            font: "600 14px var(--font-ui)",
            letterSpacing: "0.16em",
            marginBottom: 20,
          }}
        >
          INVOICES
        </div>

        <h1
          style={{
            font: "600 28px var(--font-ui)",
            letterSpacing: "-.01em",
            margin: "0 0 6px",
            textAlign: "center",
          }}
        >
          {heading}
        </h1>
        <p
          style={{
            font: "14px/1.5 var(--font-ui)",
            color: "var(--text-muted)",
            margin: "0 0 24px",
            textAlign: "center",
          }}
        >
          {blurb}
        </p>

        <div
          style={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius-md)",
            padding: 36,
            textAlign: "center",
          }}
        >
          {children}
        </div>

        {footer ? (
          <p
            style={{
              font: "13px var(--font-ui)",
              color: "var(--text-muted)",
              margin: "20px 0 0",
              textAlign: "center",
            }}
          >
            {footer.text}{" "}
            <Link
              href={footer.href}
              className="btn-secondary"
              style={{
                padding: "6px 14px",
                marginLeft: 4,
                textDecoration: "none",
                display: "inline-block",
              }}
            >
              {footer.label}
            </Link>
          </p>
        ) : null}
      </div>
    </main>
  );
}
