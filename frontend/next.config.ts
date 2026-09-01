import type { NextConfig } from "next";

const API = process.env.API_ORIGIN ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // Emits .next/standalone so a container image needs no node_modules.
  output: "standalone",
  // Next writes AGENTS.md/CLAUDE.md into the app dir otherwise; this repo
  // keeps its agent instructions at the root.
  agentRules: false,
  // The API is same-origin as far as the browser is concerned, so there is
  // no CORS config to get wrong and no token to leak across origins.
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API}/api/:path*` },
      { source: "/webhooks/:path*", destination: `${API}/webhooks/:path*` },
    ];
  },
};

export default nextConfig;
