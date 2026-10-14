"use client";

import { useEffect, useState } from "react";
import { ApiError, api } from "@/lib/api";
import {
  firebaseConfigured,
  redirectSignInError,
  signInWithGoogle,
  signInWithMicrosoft,
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
  // null: the Microsoft button; a string: its work-email field.
  const [msEmail, setMsEmail] = useState<string | null>(null);

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

  /** Which customer's provider this address signs in through, then that
   *  provider: one per Microsoft customer, so the address has to come first. */
  async function microsoft() {
    if (!msEmail) return;
    setBusy(true);
    setProblem(null);
    try {
      const { provider_id } = await api<{ provider_id: string }>(
        `/api/sso/microsoft?email=${encodeURIComponent(msEmail)}`,
      );
      await signInWithMicrosoft(provider_id, msEmail);
      onSignedIn();
    } catch (caught) {
      setProblem(
        caught instanceof ApiError
          ? caught.friendly
          : caught instanceof Error
            ? caught.message
            : String(caught),
      );
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

      {process.env.NEXT_PUBLIC_SSO_MICROSOFT === "1" ? (
        msEmail === null ? (
          <button
            type="button"
            className="btn-secondary"
            style={{ width: "100%", marginTop: 10 }}
            disabled={busy}
            onClick={() => setMsEmail("")}
          >
            Sign in with Microsoft
          </button>
        ) : (
          <form
            className="stack"
            style={{ gap: 8, marginTop: 10 }}
            onSubmit={(e) => {
              e.preventDefault();
              void microsoft();
            }}
          >
            <input
              className="input-field"
              type="email"
              aria-label="Microsoft work email"
              placeholder="you@company.com"
              value={msEmail}
              onChange={(e) => setMsEmail(e.target.value)}
              required
            />
            <button type="submit" className="btn-secondary" disabled={busy}>
              {busy ? "Opening Microsoft…" : "Continue with Microsoft"}
            </button>
          </form>
        )
      ) : null}

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
