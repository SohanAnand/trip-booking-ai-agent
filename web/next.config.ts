import type { NextConfig } from "next";
import path from "node:path";

const config: NextConfig = {
  reactStrictMode: true,
  // Pin workspace root to this directory so Next.js doesn't pick a stray
  // lockfile elsewhere on the machine.
  outputFileTracingRoot: path.join(__dirname),
  async rewrites() {
    // Proxy API to FastAPI on :8000 in dev so the browser sees same-origin.
    return [
      { source: "/api/:path*", destination: "http://localhost:8000/:path*" },
    ];
  },
};

export default config;
