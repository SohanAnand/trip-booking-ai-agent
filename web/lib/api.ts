// API base URL. In dev defaults to localhost:8000 so the browser bypasses
// Next.js's rewrite proxy (whose ~30s timeout is shorter than the agentic
// loop's 30-60s real-LLM turns). In production defaults to "" (same-origin)
// so a reverse proxy or Next rewrite can route appropriately. Override via
// web/.env.local or build env with NEXT_PUBLIC_API_BASE=https://your-host.
//
// Trailing slashes are stripped to avoid the classic `${base}${"/v1/..."}`
// double-slash bug when callers concatenate.

const RAW =
  (typeof process !== "undefined" && process.env?.NEXT_PUBLIC_API_BASE) || "";

const FALLBACK =
  typeof process !== "undefined" && process.env?.NODE_ENV === "production"
    ? ""
    : "http://localhost:8000";

export const API_BASE: string = (RAW || FALLBACK).replace(/\/+$/, "");

export const apiUrl = (path: string): string =>
  path.startsWith("http") ? path : `${API_BASE}${path}`;
