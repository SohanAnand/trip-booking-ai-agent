"use client";

import { useEffect, useState } from "react";
import { apiUrl } from "@/lib/api";

type VerifyResult = {
  ok: boolean;
  events_checked: number;
  broken_at_seq: number | null;
  reason: string | null;
};

export default function AuditPage() {
  const [verify, setVerify] = useState<VerifyResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const res = await fetch(apiUrl("/v1/audit/verify"));
        if (!res.ok) throw new Error(await res.text());
        setVerify(await res.json());
      } catch (e: any) {
        setError(e.message || String(e));
      }
    })();
  }, []);

  return (
    <div className="mx-auto max-w-[640px] px-6 py-16 md:py-24">
      <div className="anim-fade-up text-center mb-12">
        <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-faint)]">
          Append-only ledger
        </p>
        <h1 className="display-lg text-[36px] md:text-[44px] text-[var(--text)] mt-2">
          Audit log.
        </h1>
        <p className="text-[16px] leading-[1.5] text-[var(--text-mute)] mt-4 max-w-[480px] mx-auto">
          Every booking is reconstructible from this log. Each event hash includes
          the previous event hash, so any tampered row breaks the chain at that row.
        </p>
      </div>

      {error && (
        <div className="anim-fade-in rounded-2xl border border-rose-200/70 bg-rose-50 px-5 py-3.5 text-[14px] text-rose-800">
          {error}
        </div>
      )}

      {!verify && !error && (
        <div className="anim-fade-up surface-card p-7 flex items-center gap-5">
          <span className="block h-12 w-12 rounded-full bg-[var(--surface-2)] shimmer-bg" />
          <div className="flex-1 space-y-2.5">
            <div className="h-3 w-36 rounded bg-[var(--surface-2)] shimmer-bg" />
            <div className="h-3 w-60 rounded bg-[var(--surface-2)] shimmer-bg" />
          </div>
        </div>
      )}

      {verify && (
        <div
          className={`anim-scale-in surface-card p-7 ${
            verify.ok ? "" : "border-rose-300 bg-rose-50"
          }`}
        >
          <div className="flex items-center gap-5">
            <div
              className={`flex h-12 w-12 items-center justify-center rounded-full ${
                verify.ok
                  ? "bg-emerald-50 text-emerald-700"
                  : "bg-rose-100 text-rose-700"
              }`}
            >
              {verify.ok ? <CheckIcon /> : <XIcon />}
            </div>
            <div className="flex-1">
              <div className="display-md text-[20px] text-[var(--text)]">
                {verify.ok ? "Chain verified" : `Broken at sequence ${verify.broken_at_seq}`}
              </div>
              <div className="text-[14px] text-[var(--text-mute)] mt-0.5 numerals-tabular">
                {verify.ok
                  ? `${verify.events_checked} events checked, all hashes match.`
                  : `${verify.events_checked} events verified before the break.`}
              </div>
            </div>
          </div>

          {!verify.ok && verify.reason && (
            <div className="mt-5 rounded-2xl border border-rose-200 bg-white px-5 py-4 text-[13px] text-rose-900">
              <div className="text-[11px] uppercase tracking-[0.16em] text-rose-500 mb-1">
                Reason
              </div>
              <div className="font-mono text-[12px]">{verify.reason}</div>
            </div>
          )}
        </div>
      )}

      <div
        className="anim-fade-up mt-8 text-[12px] text-[var(--text-faint)] text-center"
        style={{ animationDelay: "120ms" }}
      >
        SHA-256 over canonical JSON of each event, chained via the previous hash.
      </div>
    </div>
  );
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
         strokeLinecap="round" strokeLinejoin="round" className="h-5 w-5">
      <path d="M5 12.5l4.5 4.5L19 7" />
    </svg>
  );
}

function XIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
         strokeLinecap="round" strokeLinejoin="round" className="h-5 w-5">
      <path d="M6 6l12 12M6 18L18 6" />
    </svg>
  );
}
