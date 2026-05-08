"use client";

import { useEffect, useRef, useState } from "react";
import { apiUrl } from "@/lib/api";

type Event = { seq?: number; type?: string; actor?: string; payload?: any; stage?: string };

const EVENT_LABELS: Record<string, string> = {
  "request.opened":       "Request opened",
  "tool.called":          "Searching",
  "search.no_results":    "No results found",
  "options.presented":    "Three options ready",
  "approval.selection":   "You selected an option",
  "approval.signed":      "Approval token minted",
  "booking.started":      "Booking started",
  "leg.held":             "Leg held",
  "leg.captured":         "Leg captured",
  "leg.compensated":      "Leg compensated",
  "booking.authorized":   "Authorization recorded",
  "booking.committed":    "Booking committed",
  "booking.compensated":  "Booking rolled back",
  "budget.exceeded":      "Budget exceeded",
};

const STAGE_LABELS: Record<string, string> = {
  parsed:    "Parsed your request",
  searching: "Researching live data",
  ranking:   "Ranking and diversifying options",
  complete:  "Done",
};

function labelFor(ev: Event): { title: string; detail?: string } {
  if (ev.type) {
    const title = EVENT_LABELS[ev.type] ?? ev.type;
    let detail: string | undefined;
    const p = ev.payload;
    if (ev.type === "tool.called" && p?.tool) detail = p.tool;
    else if (ev.type === "options.presented" && p?.option_count != null) detail = `${p.option_count} options`;
    else if (ev.type === "leg.held" && p?.leg) detail = p.leg;
    else if (ev.type === "leg.captured" && p?.leg) detail = p.leg;
    else if (ev.type === "booking.committed" && p?.booking_id) detail = p.booking_id;
    return { title, detail };
  }
  if (ev.stage) return { title: STAGE_LABELS[ev.stage] ?? ev.stage };
  return { title: "event" };
}

export default function ActivityStream({ requestId }: { requestId: string }) {
  const [events, setEvents] = useState<Event[]>([]);
  const [live, setLive] = useState(false);
  const listRef = useRef<HTMLOListElement | null>(null);

  // Auto-scroll to the new event when the user is already near the bottom.
  // Avoids hijacking scroll position when they've scrolled up to read history.
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const fromBottom = list.scrollHeight - list.scrollTop - list.clientHeight;
    if (fromBottom < 60) {
      list.scrollTop = list.scrollHeight;
    }
  }, [events.length]);

  useEffect(() => {
    const src = new EventSource(apiUrl(`/v1/trips/${requestId}/stream`));

    const onEvt = (e: MessageEvent) => {
      // EventSource auto-reconnects after transient drops. Flip live=true on
      // any successful message so the indicator stays accurate even when
      // onerror fires during the reconnect cycle.
      setLive(true);
      try {
        const d = JSON.parse(e.data);
        setEvents((prev) => [...prev, d]);
      } catch { /* ignore parse errors */ }
    };

    src.onopen = () => setLive(true);
    src.addEventListener("history", onEvt as any);
    src.addEventListener("progress", onEvt as any);
    src.onerror = () => setLive(false);

    return () => { src.close(); setLive(false); };
  }, [requestId]);

  return (
    <div
      className="surface-card sticky"
      style={{ top: "calc(var(--nav-h) + 16px)" }}
    >
      <div
        className="px-5 pt-4 pb-3 flex items-center justify-between"
        style={{ borderBottom: "1px solid var(--hairline)" }}
      >
        <div>
          <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-faint)]">
            Activity
          </p>
          <p className="text-[14px] text-[var(--text)] mt-0.5">Live stream</p>
        </div>
        <span className="flex items-center gap-1.5 text-[11px] text-[var(--text-mute)]">
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              live ? "bg-emerald-500 anim-orb-pulse" : "bg-[var(--hairline-2)]"
            }`}
          />
          {live ? "Live" : "Idle"}
        </span>
      </div>

      <ol
        ref={listRef}
        className="px-5 py-4 space-y-3 text-[13px] max-h-[60vh] overflow-y-auto thin-scroll"
      >
        {events.length === 0 && (
          <li className="text-[var(--text-faint)] text-[12px]">
            Waiting for the first event.
          </li>
        )}
        {events.map((ev, i) => {
          const { title, detail } = labelFor(ev);
          const isStage = !ev.type;
          // Use seq when present (stable per-event id) so React doesn't
          // re-trigger the fade-up on every existing item when a new one
          // arrives. Fall back to "stage-N" for synthetic stage events.
          const stableKey = ev.seq != null ? `seq-${ev.seq}` : `stage-${i}`;
          // Only animate the most-recently-appended event so older items
          // don't re-fade as new ones arrive.
          const isNewest = i === events.length - 1;
          return (
            <li
              key={stableKey}
              className={`flex gap-3 ${isNewest ? "anim-fade-up" : ""}`}
            >
              <span className="relative mt-1.5 shrink-0">
                <span
                  className={`block h-1.5 w-1.5 rounded-full ${
                    isStage ? "bg-[var(--text)]" : "bg-[var(--text-faint)]"
                  }`}
                />
              </span>
              <div className="flex-1 min-w-0">
                <div className="text-[var(--text)] leading-tight">{title}</div>
                {detail && (
                  <div className="text-[var(--text-mute)] text-[11px] mt-0.5 truncate font-mono">
                    {detail}
                  </div>
                )}
              </div>
              {ev.seq != null && (
                <span className="inline-block w-7 text-right text-[10px] text-[var(--text-faint)] numerals-tabular shrink-0 mt-1">
                  {String(ev.seq).padStart(3, "0")}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
