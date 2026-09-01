"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api, hasToken, type SystemStatus } from "@/lib/api";

const NAV = [
  { href: "/dashboard", label: "Dashboard", glyph: "▦" },
  { href: "/invoices", label: "Invoices", glyph: "▤" },
  { href: "/documents", label: "Documents", glyph: "▣" },
  { href: "/activity", label: "Activity", glyph: "≡" },
  { href: "/purchase-orders", label: "Purchase orders", glyph: "◈" },
  { href: "/settings", label: "Settings", glyph: "⚙" },
  { href: "/admin", label: "Admin", glyph: "✦" },
];

/** How often the shell re-checks the pipeline. Five seconds is fast
 *  enough that an upload appears to move on its own, and slow enough
 *  that a parked tab costs nothing worth measuring. Polling, not
 *  websockets -- there is one user per tenant in the pilot. */
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
  const [status, setStatus] = useState<SystemStatus | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api<SystemStatus>("/api/system/status")
        .then((value) => !cancelled && setStatus(value))
        // A failed status probe is itself a signal, but not one worth a
        // red screen: the banner simply stays quiet.
        .catch(() => !cancelled && setStatus(null));

    load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const paused = status ? pausedMessage(status) : null;

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
          {NAV.map((item) => (
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

          <div style={{ marginLeft: "auto" }}>
            {hasToken() ? null : (
              <span className="badge badge-neutral" title="No bearer token is being sent; the API is running with authentication disabled.">
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
