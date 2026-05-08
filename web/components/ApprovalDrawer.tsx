"use client";

import { useEffect, useId, useRef, useState } from "react";
import { apiUrl } from "@/lib/api";

type Stage = "loading" | "summary" | "authorizing" | "done" | "error";

export default function ApprovalDrawer({
  requestId,
  option,
  onClose,
  onBooked,
}: {
  requestId: string;
  option: any;
  onClose: () => void;
  onBooked: (result: any) => void;
}) {
  // `stage` describes the canonical workflow position; `inlineError` carries
  // a recoverable error to render alongside the active stage so users can
  // retry without losing their consent text or OTP.
  const [stage, setStage] = useState<Stage>("loading");
  const [summary, setSummary] = useState<any | null>(null);
  const [otp, setOtp] = useState<string>("");
  const [enteredOtp, setEnteredOtp] = useState<string>("");
  const [inlineError, setInlineError] = useState<string | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);

  const titleId = useId();
  const otpCardId = useId();
  const otpInputId = useId();
  const panelRef = useRef<HTMLDivElement | null>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  // Step 1: select(). Re-runs when the user clicks Retry from the error state.
  useEffect(() => {
    let alive = true;
    setStage("loading");
    setInlineError(null);
    (async () => {
      try {
        const res = await fetch(apiUrl(`/v1/trips/${requestId}/select`), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ option_id: option.id }),
        });
        if (!alive) return;
        if (!res.ok) throw new Error(await res.text());
        const data = await res.json();
        setSummary(data);
        setOtp(data.dev_otp);
        setStage("summary");
      } catch (e: any) {
        if (!alive) return;
        setInlineError(e.message || String(e));
        setStage("error");
      }
    })();
    return () => { alive = false; };
  }, [requestId, option.id, loadAttempt]);

  async function authorize() {
    // Double-submit guard: only allow firing when stage is "summary".
    if (stage !== "summary" || enteredOtp.length !== 6) return;
    setStage("authorizing");
    setInlineError(null);
    try {
      const res = await fetch(apiUrl(`/v1/trips/${requestId}/approve`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ option_id: option.id, code: enteredOtp }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setStage("done");
      onBooked(data);
    } catch (e: any) {
      // Recoverable error: stay on summary so the user keeps their consent
      // text + OTP and can retry without re-running /select.
      setInlineError(e.message || String(e));
      setStage("summary");
      setEnteredOtp("");
    }
  }

  // ARIA + keyboard plumbing: Esc closes; focus trap inside the panel; focus
  // restored to the previously-focused element on close. Body scroll locked
  // while the modal is open.
  useEffect(() => {
    restoreFocusRef.current = document.activeElement as HTMLElement | null;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "Tab" && panelRef.current) {
        const focusables = panelRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])',
        );
        if (focusables.length === 0) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", onKey);

    // Focus first interactive element after mount.
    requestAnimationFrame(() => {
      const target = panelRef.current?.querySelector<HTMLElement>(
        'input, button:not([aria-label="Close"])',
      );
      target?.focus();
    });

    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
      restoreFocusRef.current?.focus?.();
    };
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 anim-fade-in flex items-end lg:items-center justify-center"
      style={{ background: "rgba(0,0,0,0.32)", backdropFilter: "blur(8px)" }}
      onClick={onClose}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="anim-scale-in bg-white rounded-t-3xl lg:rounded-3xl w-full lg:max-w-[520px] border border-[var(--hairline)]"
        style={{ boxShadow: "0 30px 80px -20px rgba(0,0,0,0.25)" }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Sheet handle on mobile */}
        <div className="flex justify-center pt-2.5 lg:hidden">
          <span className="block h-[3px] w-9 rounded-full bg-[var(--hairline-2)]" />
        </div>

        <div className="px-7 pt-6 pb-3 flex items-start justify-between gap-4">
          <div>
            <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-faint)]">
              {stage === "summary" ? "Step 2 of 2" : "Approval"}
            </p>
            <h2 id={titleId} className="display-md text-[22px] text-[var(--text)] mt-1.5">
              {stage === "loading" && "Revalidating"}
              {stage === "summary" && "Confirm and authorize"}
              {stage === "authorizing" && "Booking"}
              {stage === "done" && "Booked"}
              {stage === "error" && "Something went wrong"}
            </h2>
          </div>
          <button
            onClick={onClose}
            aria-label="Close approval drawer"
            className="h-10 w-10 rounded-full flex items-center justify-center text-[var(--text-mute)] hover:text-[var(--text)] hover:bg-[var(--surface-2)] transition-colors"
          >
            <CloseIcon />
          </button>
        </div>

        <div className="px-7 pb-7">
          {stage === "loading" && (
            <div className="py-10 flex flex-col items-center text-center">
              <Spinner />
              <p className="mt-5 text-[14px] text-[var(--text-mute)]">
                Checking that your price and cancellation policy still hold.
              </p>
            </div>
          )}

          {stage === "summary" && summary && (
            <div className="anim-fade-in space-y-5">
              <div className="rounded-2xl bg-[var(--surface-2)] p-4 text-[14px] leading-[1.55] text-[var(--text-soft)]">
                {summary.summary.consent_text}
              </div>

              <dl className="grid grid-cols-3 gap-4 text-[13px]">
                <Detail label="Total" value={summary.summary.total_price_display} mono />
                <Detail label="Cancellation" value={summary.summary.cancellation_policy} />
                <Detail label="Payment" value={summary.summary.payment_method_id} mono />
              </dl>

              {summary.drift?.has_drift && (
                <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4 text-[13px] text-amber-900">
                  <div className="font-medium mb-1">Drift detected</div>
                  <div className="text-amber-800">
                    {(summary.drift.diffs ?? [])
                      .map((d: any) => typeof d === "string" ? d : JSON.stringify(d))
                      .join("; ")}
                  </div>
                </div>
              )}

              <div className="pt-5" style={{ borderTop: "1px solid var(--hairline)" }}>
                <p className="text-[13px] text-[var(--text-mute)] mb-4 leading-relaxed">
                  In production this is a passkey prompt. For the demo your code is shown below.
                </p>

                <div
                  id={otpCardId}
                  role="status"
                  aria-live="polite"
                  className="rounded-2xl bg-[var(--text)] text-white px-5 py-4 mb-4 text-center"
                  style={{ boxShadow: "0 8px 24px -12px rgba(0,0,0,0.3)" }}
                >
                  <div className="text-[10px] uppercase tracking-[0.22em] text-white/50 mb-1.5">
                    Your code
                  </div>
                  <div className="font-mono text-[28px] tracking-[0.4em] numerals-tabular">
                    {otp}
                  </div>
                </div>

                <label htmlFor={otpInputId} className="sr-only">
                  Enter your 6 digit code
                </label>
                <input
                  id={otpInputId}
                  aria-describedby={otpCardId}
                  value={enteredOtp}
                  onChange={(e) => setEnteredOtp(e.target.value.replace(/[^0-9]/g, ""))}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && enteredOtp.length === 6 && stage === "summary") {
                      e.preventDefault();
                      authorize();
                    }
                  }}
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  placeholder="Enter the 6 digit code"
                  maxLength={6}
                  className="w-full rounded-2xl border border-[var(--hairline-2)] bg-white px-4 py-3 mb-4 font-mono text-center tracking-[0.4em] text-[18px] numerals-tabular text-[var(--text)] placeholder:text-[var(--text-faint)] placeholder:tracking-normal focus:outline-none focus:border-[var(--text)] transition-colors"
                />

                {inlineError && (
                  <div
                    role="alert"
                    className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-2.5 mb-4 text-[13px] text-rose-800"
                  >
                    {inlineError}
                  </div>
                )}

                <button
                  type="button"
                  onClick={authorize}
                  disabled={enteredOtp.length !== 6 || stage !== "summary"}
                  className="btn-pill w-full"
                >
                  Authorize and book
                </button>
              </div>
            </div>
          )}

          {stage === "authorizing" && (
            <div className="py-10 flex flex-col items-center text-center">
              <Spinner />
              <p className="mt-5 text-[14px] text-[var(--text-mute)]">
                Running two-phase commit.
              </p>
              <p className="text-[12px] text-[var(--text-faint)] mt-1">
                This can take a few seconds.
              </p>
            </div>
          )}

          {stage === "error" && (
            <div className="anim-fade-in space-y-4">
              <div
                role="alert"
                className="rounded-2xl border border-rose-200 bg-rose-50 p-4 text-[14px] text-rose-800"
              >
                {inlineError ?? "Could not load this option."}
              </div>
              <div className="flex gap-3 justify-end">
                <button
                  type="button"
                  onClick={onClose}
                  className="btn-pill-ghost"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => setLoadAttempt((n) => n + 1)}
                  className="btn-pill"
                >
                  Try again
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Detail({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-[0.18em] text-[var(--text-faint)]">
        {label}
      </dt>
      <dd className={`mt-1.5 text-[var(--text)] text-[13px] ${mono ? "font-mono numerals-tabular" : ""}`}>
        {value}
      </dd>
    </div>
  );
}

function Spinner() {
  return (
    <div className="relative h-12 w-12">
      <span className="absolute inset-0 rounded-full bg-[var(--surface-2)]" />
      <span
        className="absolute inset-1.5 rounded-full animate-spin"
        style={{
          border: "2px solid var(--hairline)",
          borderTopColor: "var(--text)",
        }}
      />
    </div>
  );
}

function CloseIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-4 w-4"
      aria-hidden="true"
    >
      <path d="M4 4l8 8M4 12L12 4" />
    </svg>
  );
}
