import type { NextConfig } from "next";

/**
 * Next.js config for the Hub viewer.
 *
 * - `output: "standalone"` produces a minimal server bundle for the M8
 *   container image (no need to ship node_modules at deploy time).
 * - No `images` host allowlist yet; the viewer renders no external images
 *   outside the iframe content origin.
 */
const config: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  // Security defaults that match the Hub's CSP posture on /render/{id}.
  // The Next.js viewer origin serves no script except its own bundle.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Frame-Options", value: "DENY" },
        ],
      },
    ];
  },
};

export default config;
