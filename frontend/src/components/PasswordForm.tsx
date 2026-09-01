"use client";

import { useState } from "react";
import {
  sendPasswordReset,
  signInWithPassword,
  signUpWithPassword,
} from "@/lib/auth";

/** Email and password, in either mode.
 *
 *  Sign-up sends a verification message and does not sign the user through:
 *  the API refuses a token whose email is unverified, so letting them straight
 *  in would land them on a 403 with no explanation.
 */
export function PasswordForm({
  mode,
  onSignedIn,
  onSignedUp,
}: {
  mode: "login" | "signup";
  onSignedIn: () => void;
  onSignedUp: (email: string) => void;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    setNotice(null);
    try {
      if (mode === "signup") {
        await signUpWithPassword(email, password);
        onSignedUp(email);
      } else {
        await signInWithPassword(email, password);
        onSignedIn();
      }
    } catch (caught) {
      setProblem(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    if (!email.trim()) {
      setProblem("Enter your email address first.");
      return;
    }
    setProblem(null);
    try {
      await sendPasswordReset(email);
      // Worded so it says the same thing whether or not the address has an
      // account: the response must not reveal who is registered.
      setNotice(`If ${email} has an account, a reset link is on its way.`);
    } catch (caught) {
      setProblem(caught instanceof Error ? caught.message : String(caught));
    }
  }

  return (
    <form onSubmit={submit} style={{ textAlign: "left" }}>
      <label
        htmlFor="email"
        style={{ display: "block", font: "600 12px var(--font-ui)", marginBottom: 6 }}
      >
        Work email
      </label>
      <input
        id="email"
        type="email"
        autoComplete="email"
        required
        placeholder="you@company.com"
        className="input-field"
        value={email}
        onChange={(event) => setEmail(event.target.value)}
        disabled={busy}
        style={{ marginBottom: 14 }}
      />

      <label
        htmlFor="password"
        style={{ display: "block", font: "600 12px var(--font-ui)", marginBottom: 6 }}
      >
        Password
      </label>
      <input
        id="password"
        type="password"
        autoComplete={mode === "login" ? "current-password" : "new-password"}
        required
        minLength={6}
        className="input-field"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        disabled={busy}
        style={{ marginBottom: mode === "signup" ? 6 : 14 }}
      />
      {mode === "signup" ? (
        <p
          style={{
            font: "11.5px var(--font-ui)",
            color: "var(--text-faint)",
            margin: "0 0 14px",
          }}
        >
          At least six characters.
        </p>
      ) : null}

      <button
        type="submit"
        className="btn-primary"
        style={{ width: "100%" }}
        disabled={busy}
      >
        {busy
          ? "Working…"
          : mode === "login"
            ? "Sign in"
            : "Create account"}
      </button>

      {mode === "login" ? (
        <button
          type="button"
          className="btn-link"
          style={{ marginTop: 12 }}
          onClick={reset}
        >
          Forgot your password?
        </button>
      ) : null}

      {problem ? (
        <p
          role="alert"
          style={{
            color: "var(--error)",
            font: "12.5px/1.5 var(--font-ui)",
            margin: "14px 0 0",
          }}
        >
          {problem}
        </p>
      ) : null}
      {notice ? (
        <p
          role="status"
          style={{
            color: "var(--text-muted)",
            font: "12.5px/1.5 var(--font-ui)",
            margin: "14px 0 0",
          }}
        >
          {notice}
        </p>
      ) : null}
    </form>
  );
}
