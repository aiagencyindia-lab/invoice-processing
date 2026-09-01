"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { can, hasToken, type Me, type SystemStatus } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

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

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { data: status } = usePoll<SystemStatus>("/api/system/status", POLL_MS);
  const { data: me } = usePoll<Me>("/api/me");

  const paused = status ? pausedMessage(status) : null;
  // Until /api/me answers, show the unrestricted views only. Briefly hiding a
  // link a user does have is harmless; briefly showing one they do not invites
  // a click that ends in a 403.
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
            {me ? (
              <span className="faint" style={{ fontSize: 12 }}>
                {me.email} · {me.role} · {me.tenant_id}
              </span>
            ) : null}
            {hasToken() ? null : (
              <span
                className="badge badge-neutral"
                title="No bearer token is being sent; the API is running with authentication disabled."
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
