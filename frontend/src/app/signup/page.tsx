"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { signOut, watchUser, type SignedInUser } from "@/lib/auth";
import { SignIn } from "@/components/SignIn";

/** What "sign up" means here.
 *
 *  Access is granted per organisation by an administrator -- a Google account
 *  on its own sees nothing. So this page does not create anything. It explains
 *  that, gets the visitor an account if they lack one, and then shows the two
 *  values an administrator needs to grant them access.
 *
 *  The alternative, letting a signup create its own organisation, is a
 *  deliberate no: it would let anyone with a Google address mint tenants,
 *  inbound-mail addresses and storage prefixes inside the system, which is the
 *  reason ALLOW_TENANT_PROVISIONING is off by default.
 */
export default function SignUpPage() {
  const [user, setUser] = useState<SignedInUser | null>(null);
  const [known, setKnown] = useState(false);

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
      <main style={{ padding: "4rem 2rem" }}>
        <p className="muted">Loading…</p>
      </main>
    );
  }

  // Nobody signed in yet: they need an account before an administrator has
  // anything to grant access to.
  if (!user) {
    return (
      <SignIn
        onSignedIn={() => {}}
        heading="Request access"
        blurb="Sign in with the Google account you want to use. That creates your account but grants nothing on its own — an administrator adds it to an organisation."
        footer={<Link href="/login">Back to sign in</Link>}
      />
    );
  }

  return (
    <main
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
      }}
    >
      <div className="panel stack" style={{ maxWidth: 520, gap: 14 }}>
        <div className="eyebrow">Invoice processing</div>
        <h1 style={{ fontSize: 20 }}>Your account is registered</h1>
        <p className="muted" style={{ fontSize: 13, lineHeight: 1.6 }}>
          It is not attached to an organisation yet, so there is nothing to show
          you. Send an administrator the two values below and ask them to add
          you — they can do it from Admin → People.
        </p>

        <dl
          style={{
            display: "grid",
            gridTemplateColumns: "auto 1fr",
            gap: "8px 14px",
            fontSize: 13,
            alignItems: "baseline",
          }}
        >
          <dt className="muted">Email</dt>
          <dd style={{ font: "12.5px var(--font-mono)" }}>
            {user.email ?? "—"}
          </dd>
          <dt className="muted">Account id</dt>
          <dd style={{ font: "12.5px var(--font-mono)", wordBreak: "break-all" }}>
            {user.uid}
          </dd>
        </dl>

        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <button
            type="button"
            className="btn-secondary"
            onClick={() =>
              navigator.clipboard?.writeText(`${user.email} ${user.uid}`)
            }
          >
            Copy both
          </button>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => signOut().then(() => setUser(null))}
          >
            Sign out
          </button>
          <Link href="/login" className="btn-chip" style={{ alignSelf: "center" }}>
            Try signing in
          </Link>
        </div>
      </div>
    </main>
  );
}
