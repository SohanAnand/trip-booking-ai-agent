"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { apiUrl } from "@/lib/api";

const EXAMPLES = [
  "Five days in NYC, no budget cap, best luxury and best view",
  "A week in Tokyo this autumn under $5,000",
  "Long weekend in Porto, refundable flights",
  "Ten days in London then five in Rome, $12K total",
];

const STAGES = [
  "Reading your request.",
  "Searching real flight inventory.",
  "Comparing hotels in your style.",
  "Checking the forecast.",
  "Choosing three options for you.",
];

export default function Home() {
  const router = useRouter();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stageIdx, setStageIdx] = useState(0);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (!busy) return;
    setStageIdx(0);
    const i = setInterval(() => {
      setStageIdx((n) => (n + 1) % STAGES.length);
    }, 2400);
    return () => clearInterval(i);
  }, [busy]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(apiUrl("/v1/trips"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ raw_text: text }),
      });
      if (!res.ok) {
        let msg = await res.text();
        try { msg = JSON.parse(msg).detail ?? msg; } catch { /* leave msg */ }
        throw new Error(msg);
      }
      const data = await res.json();
      router.push(`/trip/${data.request_id}`);
    } catch (err: any) {
      setError(err.message || String(err));
      setBusy(false);
    }
  }

  return (
    <section className="min-h-[calc(100vh-48px)] flex items-center justify-center px-6 py-24 md:py-32">
      <div className="w-full max-w-[720px]">
        {!busy && (
          <div className="anim-fade-up text-center mb-14">
            <h1 className="display-xl text-[44px] md:text-[64px] text-[var(--text)]">
              Where to next.
            </h1>
            <p className="mt-5 text-[17px] md:text-[19px] leading-[1.47] text-[var(--text-mute)] max-w-[560px] mx-auto">
              Describe the trip you want. The agent researches real flights and
              hotels, then presents three options chosen for you.
            </p>
          </div>
        )}

        {busy ? (
          <Loader stageIdx={stageIdx} />
        ) : (
          <form onSubmit={submit} className="anim-fade-up" style={{ animationDelay: "100ms" }}>
            <div className="surface-card surface-card-interactive p-1.5 pl-2">
              <textarea
                ref={inputRef}
                value={text}
                onChange={(e) => setText(e.target.value)}
                rows={2}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    submit(e as unknown as React.FormEvent);
                  }
                }}
                className="w-full resize-none bg-transparent px-3 pt-3 pb-2 text-[16px] leading-[1.5] text-[var(--text)] placeholder:text-[var(--text-faint)] focus:outline-none"
                placeholder="A trip you'd actually love. Be specific."
                autoFocus
              />
              <div className="flex items-center justify-between gap-3 px-2 pb-1.5 pt-1">
                <span className="text-[12px] text-[var(--text-faint)]">
                  Press Enter to plan
                </span>
                <button type="submit" disabled={!text.trim()} className="btn-pill">
                  Plan it
                </button>
              </div>
            </div>

            <div
              className="mt-8 flex flex-wrap items-center justify-center gap-2"
              aria-label="Example prompts"
            >
              {EXAMPLES.map((ex, i) => (
                <button
                  key={ex}
                  type="button"
                  onClick={() => { setText(ex); inputRef.current?.focus(); }}
                  className="chip anim-fade-up"
                  style={{ animationDelay: `${180 + i * 80}ms` }}
                >
                  {ex}
                </button>
              ))}
            </div>

            {error && (
              <div className="anim-fade-in mt-8 mx-auto max-w-[560px] text-center text-[14px] text-rose-700">
                {error}
              </div>
            )}
          </form>
        )}
      </div>
    </section>
  );
}

function Loader({ stageIdx }: { stageIdx: number }) {
  return (
    <div className="anim-fade-in flex flex-col items-center text-center py-8">
      {/* Calm pulsing orb with a thin orbiting ring. Apple-quiet. */}
      <div className="relative h-20 w-20 mb-10">
        <span
          className="absolute inset-0 rounded-full anim-orb-pulse"
          style={{
            background:
              "radial-gradient(circle at 30% 30%, #1d1d1f 0%, #1d1d1f 40%, transparent 70%)",
            opacity: 0.9,
          }}
        />
        <span
          className="absolute inset-[-12px] rounded-full anim-orb-orbit"
          style={{
            border: "1px solid var(--hairline)",
            borderTopColor: "rgba(0,0,0,0.35)",
          }}
        />
      </div>

      {/* Crossfading status line */}
      <div className="relative h-7 w-full max-w-md">
        {STAGES.map((s, i) => (
          <p
            key={s}
            className={`absolute inset-0 text-[16px] text-[var(--text-soft)] transition-opacity duration-700 ease-out ${
              i === stageIdx ? "opacity-100" : "opacity-0"
            }`}
          >
            {s}
          </p>
        ))}
      </div>

      <div className="mt-3 text-[12px] text-[var(--text-faint)] numerals-tabular">
        Researching live data. This takes about a minute.
      </div>
    </div>
  );
}
