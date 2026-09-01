"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  api,
  can,
  setTokenProvider,
  type Me,
  type SystemStatus,
} from "@/lib/api";
import {
  currentIdToken,
  signOut,
  watchUser,
  type SignedInUser,
} from "@/lib/auth";
import { usePoll } from "@/lib/hooks";

// Every request asks Identity Platform for a token. Registered here, at import
// of the shell, because the shell wraps every page -- there is no route that
// should be making unauthenticated calls.
setTokenProvider(currentIdToken);

/** `min` is the role the *server* requires for that view's main endpoint.
 *  Hiding a link the caller cannot use is a courtesy; `require_role` on the
 *  endpoint is the enforcement, and it does not consult this list. */
const NAV = [
  { href: "/dashboard", label: "Dashboard", glyph: "▦" },
  { href: "/invoices", label: "Invoices", glyph: "▤" },
  { href: "/documents", label: "Documents", glyph: "▣", min: "clerk" },
  { href: "/activity", label: "Activity", glyph: "≡" },
  { href: "/purchase-orders", label: "Purchase orders", glyph: "◈", min: "clerk" },
  { href: "/settings", label: "Settings", glyph: "⚙", min: "admin" },
  { href: "/admin", label: "Admin", glyph: "✦", min: "admin" },
];

/** Fast enough that an upload appears to move on its own, slow enough that a
 *  parked tab costs nothing worth measuring. */
const POLL_MS = 5000;

function isActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

function pausedMessage(status: SystemStatus): string | null {
  const { alive, last_seen_seconds, ever_run } = status.worker;
  if (alive) return null;
  if (!ever_run) {
    return "No worker has ever run. Uploads will be received but not processed until one starts.";
  }
  const seconds = Math.round(last_seen_seconds ?? 0);
  return `Processing is paused — the worker was last seen ${seconds}s ago. Uploads are safe on the queue and will resume when it comes back.`;
}

/** Routes that must render without the shell, or an unauthenticated visitor
 *  would be redirected away from the very page that lets them sign in. */
const PUBLIC_ROUTES = ["/login", "/signup"];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const isPublic = PUBLIC_ROUTES.includes(pathname);

  // `undefined` while unknown, so a first paint never guesses. Guessing
  // "signed out" flashes a sign-in screen at someone who is signed in, and
  // guessing "signed in" flashes the whole application at someone who is not.
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [user, setUser] = useState<SignedInUser | null>(null);
  // Set when the API accepted the token and refused the account anyway.
  const [denied, setDenied] = useState<string | null>(null);

  /** Ask the API who we are. The server is the authority on whether a token
   *  is required at all: a deployment with authentication disabled answers
   *  this happily and nobody is ever shown a sign-in screen. */
  const identify = useCallback(() => {
    api<Me>("/api/me")
      .then((value) => {
        setMe(value);
        setDenied(null);
      })
      .catch((caught) => {
        const status = caught instanceof ApiError ? caught.status : 0;
        // 403 means the token was accepted and the account has no access --
        // no tenant claim, or an unverified address. Rendering the plain
        // sign-in screen for that told the user nothing: they had just
        // signed in successfully and were sent back to the button they came
        // from, which looks exactly like a button that does nothing.
        setDenied(
          status === 403
            ? (caught as ApiError).message ||
                "This account is not attached to an organisation."
            : null,
        );
        setMe(null);
      });
  }, []);

  useEffect(() => {
    let cancelled = false;
    let unsubscribe: (() => void) | undefined;

    watchUser((value) => {
      if (cancelled) return;
      setUser(value);
      identify();
    }).then((stop) => {
      if (cancelled) stop();
      else unsubscribe = stop;
    });

    return () => {
      cancelled = true;
      unsubscribe?.();
    };
  }, [identify]);

  const { data: status } = usePoll<SystemStatus>(
    me ? "/api/system/status" : null,
    POLL_MS,
  );

  if (isPublic) return <>{children}</>;

  if (me === undefined) {
    return (
      <main style={{ padding: "4rem 2rem" }}>
        <p className="muted">Loading…</p>
      </main>
    );
  }

  if (me === null) {
    // Signed in but refused goes to /signup, which explains what is missing
    // and shows the values an administrator needs. Not signed in at all goes
    // to /login. Sending both to the same screen was how someone who had
    // already signed in ended up pressing the sign-in button again.
    router.replace(denied ? "/signup" : "/login");
    return (
      <main style={{ padding: "4rem 2rem" }}>
        <p className="muted">Taking you to sign in…</p>
      </main>
    );
  }

  const paused = status ? pausedMessage(status) : null;
  const nav = NAV.filter((item) => !item.min || can(me, item.min));

  return (
    <div className="app-shell">
      <a href="#main" className="skip-link">
        Skip to content
      </a>

      <aside className="app-sidebar">
        <Link href="/dashboard" className="sidebar-brand">
          Invoices
        </Link>

        <nav className="app-sidebar-nav" aria-label="Primary">
          {nav.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className="sidebar-link"
              aria-current={isActive(pathname, item.href) ? "page" : undefined}
            >
              <span className="sidebar-glyph" aria-hidden="true">
                {item.glyph}
              </span>
              {item.label}
            </Link>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className="eyebrow" style={{ marginBottom: 4 }}>
            Storage
          </div>
          {status ? (
            <>
              Documents: {status.storage.documents}
              <br />
              Database: {status.storage.database}
              <br />
              Queue: {status.storage.queue}
            </>
          ) : (
            <span className="faint">unavailable</span>
          )}
        </div>
      </aside>

      <div className="app-main">
        <header className="app-topbar">
          <div>
            <div className="eyebrow">Queue</div>
            <strong style={{ fontSize: 13 }}>
              {status?.queue.depth ?? "—"} waiting
              {status?.queue.dead_letters
                ? ` · ${status.queue.dead_letters} failed`
                : ""}
            </strong>
          </div>

          <div
            style={{
              marginLeft: "auto",
              display: "flex",
              alignItems: "center",
              gap: 12,
            }}
          >
            <span className="faint" style={{ fontSize: 12 }}>
              {me.email} · {me.role} · {me.tenant_id}
            </span>
            {user ? (
              <button
                type="button"
                className="btn-chip"
                onClick={() => signOut().then(identify)}
              >
                Sign out
              </button>
            ) : (
              // No signed-in user, yet the API answered: this deployment is
              // running with authentication disabled. Say so plainly -- it is
              // the difference between a demo and a system holding real money.
              <span
                className="badge badge-warn"
                title="The API accepted this request without a token, so it is running with AUTH_DISABLED. Never correct for production."
              >
                dev mode
              </span>
            )}
          </div>
        </header>

        {paused ? (
          <div className="banner" role="status">
            <span aria-hidden="true">⚠</span>
            {paused}
          </div>
        ) : null}

        <main id="main" className="app-content">
          {children}
        </main>
      </div>
    </div>
  );
}
