"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AuthCard, OrDivider } from "@/components/AuthCard";
import { PasswordForm } from "@/components/PasswordForm";
import { SignIn } from "@/components/SignIn";
import { api, ApiError } from "@/lib/api";
import {
  firebaseConfigured,
  isEmailVerified,
  refreshToken,
  resendVerification,
  signOut,
  watchUser,
  type SignedInUser,
} from "@/lib/auth";

type Step = "loading" | "account" | "verify" | "organisation";

export default function SignUpPage() {
  const router = useRouter();
  const [user, setUser] = useState<SignedInUser | null>(null);
  const [step, setStep] = useState<Step>("loading");
  const [organisation, setOrganisation] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  /** Which step this account is on. Derived from Firebase and re-derived on
   *  demand, because verifying an email happens in another tab and nothing
   *  tells this one. */
  const settle = useCallback(async (value: SignedInUser | null) => {
    setUser(value);
    if (!value) return setStep("account");
    if (await isEmailVerified()) {
      // reload() updates the user, not the ID token: the cached token still
      // says email_verified=false and the server would refuse signup.
      // A failed refresh must not strand the page on "Checking…"; if the
      // token really is stale, the server's 403 says so in words.
      await refreshToken().catch(() => undefined);
      setStep("organisation");
    } else {
      setStep("verify");
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let stop: (() => void) | undefined;
    watchUser((value) => {
      if (!cancelled) void settle(value);
    }).then((unsubscribe) => {
      if (cancelled) unsubscribe();
      else stop = unsubscribe;
    });
    return () => {
      cancelled = true;
      stop?.();
    };
  }, [settle]);

  async function createOrganisation(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      await api("/api/signup", {
        method: "POST",
        body: JSON.stringify({ organisation }),
      });
      // Claims are baked into a token when it is minted, so without forcing a
      // refresh the account keeps presenting the one it signed up with -- no
      // claims -- and is refused by the system that just granted it access.
      await refreshToken();
      router.replace("/dashboard");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        await refreshToken();
        router.replace("/dashboard");
        return;
      }
      setProblem(caught instanceof ApiError ? caught.friendly : String(caught));
    } finally {
      setBusy(false);
    }
  }

  if (step === "loading") {
    return (
      <AuthCard heading="Create your account" blurb="One moment.">
        <span className="muted" style={{ fontSize: 13 }}>
          Checking…
        </span>
      </AuthCard>
    );
  }

  if (step === "account") {
    return (
      <AuthCard
        heading="Create your account"
        blurb="Then name your organisation and you are in."
        footer={{ text: "Already have one?", href: "/login", label: "Sign in" }}
      >
        <SignIn onSignedIn={() => {}} />
        {firebaseConfigured() ? (
          <>
            <OrDivider />
            <PasswordForm
              mode="signup"
              onSignedIn={() => {}}
              onSignedUp={() => setStep("verify")}
            />
          </>
        ) : null}
      </AuthCard>
    );
  }

  if (step === "verify") {
    return (
      <AuthCard
        heading="Check your email"
        blurb={`We sent a link to ${user?.email ?? "your address"}.`}
      >
        <div className="stack" style={{ gap: 12, textAlign: "left" }}>
          <div className="muted" style={{ fontSize: 13, lineHeight: 1.55 }}>
            Open it, then come back and continue. Invoices are financial
            records, so an address has to be proven before it can reach any.
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button
              type="button"
              className="btn-primary"
              onClick={async () => {
                setProblem(null);
                if (await isEmailVerified()) {
                  await refreshToken().catch(() => undefined);
                  setStep("organisation");
                } else {
                  setProblem("Not verified yet — open the link, then try again.");
                }
              }}
            >
              I have verified
            </button>
            <button
              type="button"
              className="btn-secondary"
              onClick={() =>
                resendVerification()
                  .then(() => setNotice("Sent again."))
                  .catch((c) => setProblem(String(c)))
              }
            >
              Resend
            </button>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => signOut().then(() => setStep("account"))}
            >
              Start over
            </button>
          </div>
          {problem ? (
            <p role="alert" style={{ color: "var(--error)", fontSize: 12.5 }}>
              {problem}
            </p>
          ) : null}
          {notice ? (
            <p role="status" className="muted" style={{ fontSize: 12.5 }}>
              {notice}
            </p>
          ) : null}
        </div>
      </AuthCard>
    );
  }

  return (
    <AuthCard
      heading="Name your organisation"
      blurb="It appears on your invoices and holds your rules, settings and users."
    >
      <form onSubmit={createOrganisation} style={{ textAlign: "left" }}>
        <label
          htmlFor="organisation"
          style={{ display: "block", font: "600 12px var(--font-ui)", marginBottom: 6 }}
        >
          Organisation name
        </label>
        <input
          id="organisation"
          className="input-field"
          required
          minLength={2}
          placeholder="Acme Pty Ltd"
          value={organisation}
          onChange={(event) => setOrganisation(event.target.value)}
          disabled={busy}
          style={{ marginBottom: 6 }}
        />
        <p
          style={{
            font: "11.5px/1.5 var(--font-ui)",
            color: "var(--text-faint)",
            margin: "0 0 14px",
          }}
        >
          Use the name suppliers invoice you under. You can add the other
          spellings in Settings afterwards.
        </p>
        <button
          type="submit"
          className="btn-primary"
          style={{ width: "100%" }}
          disabled={busy}
        >
          {busy ? "Creating…" : "Create organisation"}
        </button>
        {problem ? (
          <p
            role="alert"
            style={{ color: "var(--error)", font: "12.5px/1.5 var(--font-ui)", margin: "14px 0 0" }}
          >
            {problem}
          </p>
        ) : null}
      </form>
    </AuthCard>
  );
}
