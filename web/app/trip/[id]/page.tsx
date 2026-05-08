"use client";

import Link from "next/link";
import { useEffect, useState, use } from "react";
import ItineraryCard from "@/components/ItineraryCard";
import ApprovalDrawer from "@/components/ApprovalDrawer";
import ActivityStream from "@/components/ActivityStream";
import { apiUrl } from "@/lib/api";

type LoadStatus = "loading" | "ready" | "empty" | "error";

export default function TripPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [options, setOptions] = useState<any[]>([]);
  const [selected, setSelected] = useState<any | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<LoadStatus>("loading");
  const [attempt, setAttempt] = useState(0);
  const [booked, setBooked] = useState<{ booking_id: string; state: string } | null>(null);

  useEffect(() => {
    let alive = true;
    setStatus("loading");
    setError(null);
    (async () => {
      try {
        const res = await fetch(apiUrl(`/v1/trips/${id}`));
        if (!alive) return;
        if (!res.ok) {
          setError("This trip is no longer in memory. The dev server may have restarted.");
          setStatus("error");
          return;
        }
        const data = await res.json();
        const opts = data.options || [];
        setOptions(opts);
        setStatus(opts.length === 0 ? "empty" : "ready");
      } catch (e: any) {
        if (!alive) return;
        setError(e.message || String(e));
        setStatus("error");
      }
    })();
    return () => { alive = false; };
  }, [id, attempt]);

  return (
    <div className="mx-auto max-w-[1080px] px-6 py-12 md:py-16">
      <div className="anim-fade-up mb-10 flex items-end justify-between gap-4">
        <div>
          <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-faint)]">
            Your itinerary
          </p>
          <h1 className="display-lg text-[32px] md:text-[40px] text-[var(--text)] mt-1.5">
            Three options.
          </h1>
        </div>
        <p className="hidden sm:block text-[11px] text-[var(--text-faint)] numerals-tabular">
          {id}
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-10">
        <div className="lg:col-span-2 space-y-5">
          {status === "error" && (
            <div className="anim-fade-in space-y-3">
              <div
                role="alert"
                className="rounded-2xl border border-rose-200/70 bg-rose-50 px-5 py-3.5 text-[14px] text-rose-800"
              >
                {error}
              </div>
              <div className="flex gap-3">
                <button
                  type="button"
                  onClick={() => setAttempt((n) => n + 1)}
                  className="btn-pill"
                >
                  Try again
                </button>
                <Link href="/" className="btn-pill-ghost">
                  Plan a different trip
                </Link>
              </div>
            </div>
          )}

          {status === "loading" && <SkeletonCards />}

          {status === "empty" && (
            <div className="anim-fade-in surface-card p-7 text-center">
              <h2 className="display-md text-[20px] text-[var(--text)] mb-2">
                No options for this prompt.
              </h2>
              <p className="text-[14px] text-[var(--text-mute)] mb-5">
                The agent searched real inventory and found nothing it could
                ship as three distinct options. Try broader dates, a different
                city, or a higher budget.
              </p>
              <Link href="/" className="btn-pill inline-flex">
                Plan a different trip
              </Link>
            </div>
          )}

          {booked && (
            <div className="anim-fade-in rounded-2xl border border-emerald-200/80 bg-emerald-50/80 px-5 py-3.5 text-[14px] text-emerald-900">
              Booking <span className="font-mono">{booked.booking_id}</span>{" "}
              {booked.state.toLowerCase()}. Confirmation details are in the audit log.
            </div>
          )}

          {options.map((opt: any, i: number) => (
            <div
              key={opt.id}
              className="anim-fade-up"
              style={{ animationDelay: `${i * 110}ms` }}
            >
              <ItineraryCard option={opt} onBook={() => setSelected(opt)} />
            </div>
          ))}
        </div>

        <aside className="lg:col-span-1">
          <ActivityStream requestId={id} />
        </aside>
      </div>

      {selected && (
        <ApprovalDrawer
          requestId={id}
          option={selected}
          onClose={() => setSelected(null)}
          onBooked={(result) => {
            setSelected(null);
            setBooked({ booking_id: result.booking_id, state: result.state });
          }}
        />
      )}
    </div>
  );
}

function SkeletonCards() {
  return (
    <div className="space-y-5">
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="anim-fade-up surface-card p-7 md:p-8"
          style={{ animationDelay: `${i * 90}ms` }}
        >
          <div className="flex items-start justify-between gap-6">
            <div className="space-y-3 flex-1">
              <div className="h-7 w-28 rounded-full bg-[var(--surface-2)] shimmer-bg" />
              <div className="h-6 w-3/4 rounded bg-[var(--surface-2)] shimmer-bg" />
              <div className="h-3 w-1/2 rounded bg-[var(--surface-2)] shimmer-bg" />
            </div>
            <div className="h-8 w-24 rounded bg-[var(--surface-2)] shimmer-bg" />
          </div>
        </div>
      ))}
    </div>
  );
}
