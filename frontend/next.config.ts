import type { NextConfig } from "next";

const API = process.env.API_ORIGIN ?? "http://127.0.0.1:8000";

// Where Google sign-in goes: the Firebase auth domain, Google's token
// endpoints, and the popup/iframe hosts. Report-only until sign-in has been
// exercised against a live Identity Platform project; then enforce.
const AUTH = process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN || "*.firebaseapp.com";
// Deduplicated: with no auth domain configured, AUTH is the wildcard itself.
const AUTH_HOSTS = [...new Set([`https://${AUTH}`, "https://*.firebaseapp.com"])].join(" ");
const CSP = [
  "default-src 'self'",
  // Next's inline runtime carries no nonce, and the app styles inline.
  "script-src 'self' 'unsafe-inline' https://apis.google.com https://www.gstatic.com",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https://*.googleusercontent.com",
  "font-src 'self' data:",
  `connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com https://www.googleapis.com ${AUTH_HOSTS}`,
  // The document panes frame blob: URLs of PDFs fetched with the token.
  `frame-src blob: ${AUTH_HOSTS} https://accounts.google.com https://apis.google.com`,
  "object-src 'none'",
  "base-uri 'self'",
  "frame-ancestors 'none'",
  "form-action 'self' https://accounts.google.com",
].join("; ");

const SECURITY_HEADERS = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
  { key: "Content-Security-Policy-Report-Only", value: CSP },
  // Browsers ignore HSTS over plain http, so the e2e build is unaffected.
  ...(process.env.NODE_ENV === "production"
    ? [{ key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" }]
    : []),
];

const nextConfig: NextConfig = {
  // Emits .next/standalone so a container image needs no node_modules.
  output: "standalone",
  // Next writes AGENTS.md/CLAUDE.md into the app dir otherwise; this repo
  // keeps its agent instructions at the root.
  agentRules: false,
  // The API is same-origin as far as the browser is concerned, so there is
  // no CORS config to get wrong and no token to leak across origins.
  // Applied to proxied /api responses too; the API sets the same values, and
  // browsers ignore CSP on anything that is not a document.
  async headers() {
    return [{ source: "/:path*", headers: SECURITY_HEADERS }];
  },
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API}/api/:path*` },
      { source: "/webhooks/:path*", destination: `${API}/webhooks/:path*` },
    ];
  },
};

export default nextConfig;
