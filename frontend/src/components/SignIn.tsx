"use client";

import { useEffect, useState } from "react";
import {
  firebaseConfigured,
  redirectSignInError,
  signInWithGoogle,
} from "@/lib/auth";

/** The Google button, plus the two states that are not "press this".
 *
 *  It is shown because the API answered 401, never because the browser
 *  guessed that authentication is on: a deployment running with
 *  authentication disabled never reaches this screen.
 */
export function SignIn({
  onSignedIn,
  denied,
  onSignOut,
}: {
  onSignedIn: () => void;
  /** Set when the API verified the token and refused the account anyway. */
  denied?: string | null;
  onSignOut?: () => void;
}) {
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // A redirect that failed comes back as a fresh page load, so the error has
  // to be collected here rather than at the click that started it.
  useEffect(() => {
    let cancelled = false;
    redirectSignInError().then((message) => {
      if (!cancelled && message) setProblem(message);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function go() {
    setBusy(true);
    setProblem(null);
    try {
      // Returns after a popup sign-in; navigates away when it fell back to a
      // redirect, in which case nothing below this line runs.
      await signInWithGoogle();
      onSignedIn();
    } catch (caught) {
      setProblem(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  if (denied) {
    // Signing in worked. Offering the button again would send someone round
    // the same loop for ever, because no amount of signing in adds a claim.
    return (
      <div className="stack" style={{ gap: 14, textAlign: "left" }}>
        <strong style={{ fontSize: 14 }}>
          You are signed in, but this account has no access.
        </strong>
        <div className="muted" style={{ fontSize: 13, lineHeight: 1.55 }}>
          {denied}
        </div>
        <div className="muted" style={{ fontSize: 13, lineHeight: 1.55 }}>
          An administrator has to add this address to an organisation before it
          can see anything.
        </div>
        {onSignOut ? (
          <button type="button" className="btn-secondary" onClick={onSignOut}>
            Sign out and try another account
          </button>
        ) : null}
      </div>
    );
  }

  if (!firebaseConfigured()) {
    // Never render a button that cannot work: a dead button reads as a broken
    // account rather than a missing setting.
    return (
      <div className="muted" style={{ fontSize: 12.5, lineHeight: 1.6, textAlign: "left" }}>
        This app has no Identity Platform configuration, so it cannot obtain a
        token. Set <code>NEXT_PUBLIC_FIREBASE_API_KEY</code>,{" "}
        <code>NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN</code> and{" "}
        <code>NEXT_PUBLIC_FIREBASE_PROJECT_ID</code> — in{" "}
        <code>frontend/.env.local</code> locally, or on the app service when
        deployed — then rebuild. They are baked in at build time.
      </div>
    );
  }

  return (
    <>
      <button
        type="button"
        className="btn-primary"
        style={{ width: "100%" }}
        disabled={busy}
        onClick={go}
      >
        {busy ? "Opening Google…" : "Continue with Google"}
      </button>

      {problem ? (
        <p
          role="alert"
          style={{
            color: "var(--error)",
            font: "12.5px/1.5 var(--font-ui)",
            margin: "14px 0 0",
            textAlign: "left",
          }}
        >
          {problem}
        </p>
      ) : null}

      <p
        style={{
          font: "11px var(--font-ui)",
          color: "var(--text-faint)",
          margin: "20px 0 0",
        }}
      >
        Access is granted per organisation.
      </p>
    </>
  );
}
