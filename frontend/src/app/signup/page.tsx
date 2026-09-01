"use client";

import { useEffect, useState } from "react";
import { AuthCard } from "@/components/AuthCard";
import { SignIn } from "@/components/SignIn";
import { signOut, watchUser, type SignedInUser } from "@/lib/auth";

/** Sign-up creates nothing.
 *
 *  Access is granted per organisation by an administrator, so a sign-up that
 *  provisioned its own tenant would let anyone with a Google address mint
 *  organisations, inbound-mail addresses and storage prefixes inside the
 *  system -- which is what ALLOW_TENANT_PROVISIONING being off by default
 *  exists to prevent. The page gets the visitor an account, then shows the two
 *  values an administrator needs to grant it access.
 */
export default function SignUpPage() {
  const [user, setUser] = useState<SignedInUser | null>(null);
  const [known, setKnown] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let stop: (() => void) | undefined;
    watchUser((value) => {
      if (cancelled) return;
      setUser(value);
      setKnown(true);
    }).then((unsubscribe) => {
      if (cancelled) unsubscribe();
      else stop = unsubscribe;
    });
    return () => {
      cancelled = true;
      stop?.();
    };
  }, []);

  if (!known) {
    return (
      <AuthCard heading="Request access" blurb="Checking your account…">
        <span className="muted" style={{ fontSize: 13 }}>
          One moment.
        </span>
      </AuthCard>
    );
  }

  if (!user) {
    return (
      <AuthCard
        heading="Request access"
        blurb="Sign in first — that creates your account but grants nothing on its own."
        footer={{ text: "Already have access?", href: "/login", label: "Sign in" }}
      >
        <SignIn onSignedIn={() => {}} />
      </AuthCard>
    );
  }

  return (
    <AuthCard
      heading="Almost there"
      blurb="Your account exists but is not attached to an organisation yet."
      footer={{ text: "Been added since?", href: "/login", label: "Sign in" }}
    >
      <div className="stack" style={{ gap: 14, textAlign: "left" }}>
        <div className="muted" style={{ fontSize: 13, lineHeight: 1.55 }}>
          Send these to an administrator. They add you from Admin → People.
        </div>

        <dl
          style={{
            display: "grid",
            gridTemplateColumns: "auto 1fr",
            gap: "6px 12px",
            fontSize: 12.5,
            alignItems: "baseline",
            margin: 0,
          }}
        >
          <dt className="muted">Email</dt>
          <dd style={{ font: "12px var(--font-mono)", margin: 0 }}>
            {user.email ?? "—"}
          </dd>
          <dt className="muted">Account id</dt>
          <dd
            style={{
              font: "12px var(--font-mono)",
              margin: 0,
              wordBreak: "break-all",
            }}
          >
            {user.uid}
          </dd>
        </dl>

        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => {
              navigator.clipboard
                ?.writeText(`${user.email} ${user.uid}`)
                .then(() => setCopied(true))
                .catch(() => setCopied(false));
            }}
          >
            {copied ? "Copied" : "Copy both"}
          </button>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => signOut().then(() => setUser(null))}
          >
            Sign out
          </button>
        </div>
      </div>
    </AuthCard>
  );
}
