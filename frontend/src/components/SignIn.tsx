"use client";

import { useEffect, useState } from "react";
import {
  firebaseConfigured,
  redirectSignInError,
  signInWithGoogle,
} from "@/lib/auth";

/** The screen shown when the API refuses an unauthenticated request.
 *
 *  It exists because the server said 401, not because the browser decided
 *  the user was signed out. That ordering matters: a deployment running with
 *  authentication disabled never reaches this screen, and no client-side
 *  guess about whether auth is on can disagree with the service.
 */
export function SignIn({
  onSignedIn,
  denied,
  onSignOut,
  heading = "Sign in",
  blurb,
  footer,
}: {
  onSignedIn: () => void;
  /** Set when the API verified the token and refused the account anyway. */
  denied?: string | null;
  onSignOut?: () => void;
  heading?: string;
  blurb?: string;
  footer?: React.ReactNode;
}) {
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const configured = firebaseConfigured();

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
      <div className="panel stack" style={{ maxWidth: 420, gap: 14 }}>
        <div className="eyebrow">Invoice processing</div>
        <h1 style={{ fontSize: 20 }}>{heading}</h1>

        {denied ? (
          // Signing in worked. Saying "sign in" again here would send someone
          // round the same loop indefinitely, so say what actually happened
          // and what the administrator has to do about it.
          <div className="stack" style={{ gap: 12 }}>
            <div className="empty-state" style={{ textAlign: "left" }}>
              <strong>You are signed in, but this account has no access.</strong>
              <div style={{ marginTop: 8 }}>{denied}</div>
              <div style={{ marginTop: 8 }}>
                An administrator has to add this address to an organisation
                before it can see anything.
              </div>
            </div>
            {onSignOut ? (
              <button type="button" className="btn-secondary" onClick={onSignOut}>
                Sign out and try another account
              </button>
            ) : null}
          </div>
        ) : (
          <>
            {/* Outside the configured branch on purpose: the blurb explains
                how access works, which is true whether or not this
                deployment can obtain a token. Nested inside, an unconfigured
                instance showed a heading with nothing under it. */}
            <p className="muted" style={{ fontSize: 13, lineHeight: 1.6 }}>
              {blurb ??
                "Use the Google account your administrator added to this organisation. Access is granted per organisation, so an account without an invitation will be refused even after signing in."}
            </p>
            {configured ? (
              <button
              type="button"
              className="btn-primary"
              style={{ width: "100%" }}
              disabled={busy}
              onClick={go}
            >
              {busy ? "Opening Google…" : "Continue with Google"}
              </button>
            ) : (
          // Never render a button that cannot work. Without the client config
          // there is no way to obtain a token, and a dead button would look
          // like a broken account rather than a missing setting.
          <div className="empty-state" style={{ textAlign: "left" }}>
            The API requires a signed-in user, but this app has no Identity
            Platform configuration, so it cannot obtain a token. Set{" "}
            <code>NEXT_PUBLIC_FIREBASE_API_KEY</code>,{" "}
            <code>NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN</code> and{" "}
            <code>NEXT_PUBLIC_FIREBASE_PROJECT_ID</code> in{" "}
            <code>frontend/.env.local</code> (locally) or on the app service
            (deployed), then rebuild -- they are baked in at build time.
              </div>
            )}
          </>
        )}

        {problem ? (
          <div
            role="alert"
            style={{ color: "var(--error)", fontSize: 13, lineHeight: 1.5 }}
          >
            {problem}
          </div>
        ) : null}

        {footer ? (
          <div className="muted" style={{ fontSize: 12.5 }}>
            {footer}
          </div>
        ) : null}
      </div>
    </main>
  );
}
