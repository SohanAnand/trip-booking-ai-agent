"use client";

import { useId, useState } from "react";

type Grounded<T> = { value: T };
type Option = {
  id: string;
  rank: number;
  tradeoff_label: string;    // free-form slug picked per request
  why_this_one: string;
  the_catch: string[];
  // Legacy single-leg fields. Present when the request has only one leg.
  flight?: Grounded<any> | null;
  hotel?: Grounded<any> | null;
  weather?: Grounded<any> | null;
  // Multi-leg payload. Each entry is one leg of a multi-city trip.
  legs?: Array<{
    leg_index: number;
    origin?: string;
    destination?: string;
    flight: Grounded<any>;
    hotel: Grounded<any>;
    weather?: Grounded<any> | null;
    leg_total_cents?: number;
  }> | null;
  return_flight?: Grounded<any> | null;
  total_price_cents: Grounded<number>;
  currency: Grounded<string>;
};

// Display name for known slugs; unknown slugs fall back to title-cased text.
const LABEL_OVERRIDES: Record<string, string> = {
  // Legacy slugs from the old fixed-enum era.
  cheapest: "Best value",
  best_reviewed: "Top reviewed",
  "top-reviewed": "Top reviewed",
  alternative: "Worth considering",
  // Common one-word slugs Claude may still emit.
  value: "Best value",
  luxury: "Luxury pick",
  premium: "Premium pick",
  balanced: "Balanced pick",
  refundable: "Fully refundable",
  fastest: "Fastest route",
  central: "Most central",
  boutique: "Boutique stay",
  "all-inclusive": "All-inclusive",
  "closest-to-target": "Closest to target",
};

function prettyLabel(slug: string): string {
  const key = (slug || "").toLowerCase();
  if (LABEL_OVERRIDES[key]) return LABEL_OVERRIDES[key];
  return key
    .replace(/[-_]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase()) || "Option";
}

export default function ItineraryCard({
  option,
  onBook,
}: {
  option: Option;
  onBook: () => void;
}) {
  const [open, setOpen] = useState(false);
  const detailsId = useId();

  const total = (option.total_price_cents.value / 100).toLocaleString("en-US", {
    style: "currency",
    currency: option.currency.value,
    maximumFractionDigits: 0,
  });

  // Normalize single-leg payloads into a leg array for unified rendering.
  const legs = normalizeLegs(option);

  // Defensive accessor: total_price_cents is raw `int` on child models but
  // `Grounded<int>` at the top level; if any backend ever wraps a child
  // price in {value:...} we'd render NaN without this.
  const cents = (x: any): number =>
    typeof x === "number" ? x : (x?.value ?? 0);

  const fmtMoney = (x: any) =>
    (cents(x) / 100).toLocaleString("en-US", {
      style: "currency",
      currency: option.currency.value,
      maximumFractionDigits: 0,
    });

  // Toggle on Enter or Space when the card root is focused (it's a div with
  // role=button to avoid the invalid button-in-button HTML the original used).
  const onKeyToggle = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setOpen((v) => !v);
    }
  };

  return (
    <article className="surface-card surface-card-interactive overflow-hidden">
      <div
        role="button"
        tabIndex={0}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={onKeyToggle}
        aria-expanded={open}
        aria-controls={detailsId}
        className="w-full text-left p-7 md:p-8 cursor-pointer focus:outline-none"
      >
        <div className="flex items-start justify-between gap-3 md:gap-6">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2 mb-4">
              <span className="chip">
                {prettyLabel(option.tradeoff_label)}
              </span>
              {legs.length > 1 && (
                <span className="text-[12px] text-[var(--text-faint)]">
                  {legs.length} cities
                </span>
              )}
            </div>
            <h3 className="display-md text-[20px] md:text-[22px] text-[var(--text)]">
              {option.why_this_one}
            </h3>
            <p className="mt-3 text-[13px] text-[var(--text-mute)] numerals-tabular">
              {legs.map((l, i) => (
                <span key={l.leg_index ?? i}>
                  {i > 0 && <span className="text-[var(--text-faint)]"> · </span>}
                  {l.destination ?? l?.hotel?.value?.neighborhood ?? "Stay"}
                </span>
              ))}
            </p>
          </div>
          <div className="text-right shrink-0 flex flex-col items-end gap-2 md:gap-3">
            <div className="display-md text-[20px] md:text-[28px] text-[var(--text)] numerals-tabular">
              {total}
            </div>
            <span className="text-[12px] text-[var(--text-mute)] inline-flex items-center gap-1.5">
              {open ? "Hide" : "Details"}
              <Chevron open={open} />
            </span>
          </div>
        </div>
      </div>

      {/* Animated expand region using grid-template-rows trick. */}
      <div
        id={detailsId}
        className="grid"
        style={{
          gridTemplateRows: open ? "1fr" : "0fr",
          transition: "grid-template-rows 480ms cubic-bezier(0.16, 1, 0.3, 1)",
        }}
      >
        <div className="overflow-hidden">
          <div
            className={`px-7 md:px-8 pb-8 transition-opacity duration-300 ${
              open ? "opacity-100" : "opacity-0"
            }`}
            style={{ borderTop: "1px solid var(--hairline)" }}
          >
            <div className="pt-7 space-y-8">
              {legs.map((leg, i) => (
                <LegBlock
                  key={leg.leg_index ?? i}
                  leg={leg}
                  index={i}
                  totalLegs={legs.length}
                  fmtMoney={fmtMoney}
                />
              ))}

              {option.return_flight && (
                <ReturnBlock
                  returnFlight={option.return_flight.value}
                  fmtMoney={fmtMoney}
                />
              )}
            </div>

            <CostBreakdown
              option={option}
              legs={legs}
              fmtMoney={fmtMoney}
              cents={cents}
              total={total}
            />

            {Array.isArray(option.the_catch) && option.the_catch.length > 0 && (
              <div
                className="mt-8 pt-6"
                style={{ borderTop: "1px solid var(--hairline)" }}
              >
                <div className="text-[11px] uppercase tracking-[0.16em] text-[var(--text-faint)] mb-3">
                  Worth knowing
                </div>
                <ul className="space-y-2 text-[14px] text-[var(--text-soft)]">
                  {option.the_catch.map((c, i) => (
                    <li key={i} className="flex gap-2.5">
                      <span className="mt-2 h-1 w-1 rounded-full bg-[var(--text-faint)] shrink-0" />
                      <span>{c}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div className="mt-8 flex items-center justify-end">
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onBook();
                }}
                className="btn-pill"
              >
                Book this
              </button>
            </div>
          </div>
        </div>
      </div>
    </article>
  );
}

function normalizeLegs(option: Option) {
  // Drop legs with missing flight/hotel — partial payloads from a degraded
  // search shouldn't crash the page.
  if (option.legs && option.legs.length > 0) {
    return option.legs.filter((l) => l?.flight?.value && l?.hotel?.value);
  }
  if (option.flight?.value && option.hotel?.value) {
    return [{
      leg_index: 0,
      flight: option.flight,
      hotel: option.hotel,
      weather: option.weather ?? null,
    }];
  }
  return [] as NonNullable<Option["legs"]>;
}

function LegBlock({
  leg,
  index,
  totalLegs,
  fmtMoney,
}: {
  leg: NonNullable<Option["legs"]>[number];
  index: number;
  totalLegs: number;
  fmtMoney: (cents: number) => string;
}) {
  const flight = leg.flight?.value;
  const hotel = leg.hotel?.value;
  const weather = leg.weather?.value;
  const out = flight?.outbound?.[0];
  if (!flight || !hotel || !out) {
    return (
      <FactUnavailable
        label={totalLegs > 1 ? `Leg ${index + 1}` : "Leg"}
        message="Flight or hotel data unavailable for this leg."
      />
    );
  }

  return (
    <div>
      {totalLegs > 1 && (
        <div className="flex items-center gap-2 mb-4">
          <span className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-mute)] font-medium">
            Leg {index + 1}
          </span>
          <span className="text-[var(--text-faint)]">·</span>
          <span className="text-[14px] text-[var(--text-soft)]">
            {out.origin} to {out.destination}
          </span>
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-x-6 gap-y-5 text-[14px]">
        <Fact label="Flight">
          <div className="font-medium text-[var(--text)]">
            {out.carrier}{out.flight_number}{" "}
            <span className="text-[var(--text-mute)] font-normal">
              {out.origin} to {out.destination}
            </span>
          </div>
          <div className="text-[var(--text-mute)] mt-0.5 numerals-tabular">
            Depart {out.depart.slice(0, 10)}
          </div>
          <div className="text-[var(--text-mute)] mt-0.5">
            {out.refundable ? "Refundable" : "Non refundable"}
            {flight.baggage_included ? " · Bag included" : " · Bag extra"}
          </div>
          <div className="text-[var(--text)] mt-1.5 numerals-tabular font-medium">
            {fmtMoney(flight.total_price_cents)} <span className="text-[var(--text-mute)] font-normal">one-way</span>
          </div>
        </Fact>

        <Fact label="Hotel">
          <div
            className="font-medium text-[var(--text)] overflow-hidden"
            style={{
              display: "-webkit-box",
              WebkitLineClamp: 2,
              WebkitBoxOrient: "vertical",
            }}
          >
            {hotel.name}
          </div>
          <div className="text-[var(--text-mute)] mt-0.5">
            {hotel.neighborhood} · {hotel.star_rating}★
          </div>
          <div className="text-[var(--text-mute)] mt-0.5 numerals-tabular">
            {hotel.nights} {hotel.nights === 1 ? "night" : "nights"} × {fmtMoney(hotel.nightly_rate_cents)}/night
          </div>
          <div className="text-[var(--text-mute)] mt-0.5">
            {hotel.refundable_until
              ? `Free cancel until ${hotel.refundable_until.slice(0, 10)}`
              : "Non refundable"}
          </div>
          <div className="text-[var(--text)] mt-1.5 numerals-tabular font-medium">
            {fmtMoney(hotel.total_price_cents)} <span className="text-[var(--text-mute)] font-normal">total</span>
          </div>
        </Fact>

        {weather ? (
          <Fact label="Weather">
            <div className="font-medium text-[var(--text)] capitalize">{weather.summary}</div>
            <div className="text-[var(--text-mute)] mt-0.5 numerals-tabular">
              {weather.avg_high_c}°C high · {weather.avg_low_c}°C low
            </div>
            <div className="text-[var(--text-mute)] mt-0.5 numerals-tabular">
              {Math.round(weather.rain_probability * 100)}% chance of rain
            </div>
          </Fact>
        ) : (
          <Fact label="Weather">
            <div className="text-[var(--text-faint)]">forecast unavailable</div>
          </Fact>
        )}
      </div>
    </div>
  );
}

function ReturnBlock({
  returnFlight,
  fmtMoney,
}: {
  returnFlight: any;
  fmtMoney: (cents: number) => string;
}) {
  const out = returnFlight?.outbound?.[0];
  if (!out) {
    return (
      <FactUnavailable
        label="Return"
        message="Return flight data unavailable."
      />
    );
  }
  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <span className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-mute)] font-medium">
          Return
        </span>
        <span className="text-[var(--text-faint)]">·</span>
        <span className="text-[14px] text-[var(--text-soft)]">
          {out.origin} to {out.destination}
        </span>
      </div>
      <div className="text-[14px] text-[var(--text-soft)] numerals-tabular">
        {out.carrier}{out.flight_number} · Depart {out.depart.slice(0, 10)} ·{" "}
        {out.refundable ? "Refundable" : "Non refundable"}
      </div>
      <div className="text-[var(--text)] mt-1.5 text-[14px] numerals-tabular font-medium">
        {fmtMoney(returnFlight.total_price_cents)}{" "}
        <span className="text-[var(--text-mute)] font-normal">one-way</span>
      </div>
    </div>
  );
}

function CostBreakdown({
  option,
  legs,
  fmtMoney,
  cents,
  total,
}: {
  option: Option;
  legs: NonNullable<Option["legs"]>;
  fmtMoney: (x: any) => string;
  cents: (x: any) => number;
  total: string;
}) {
  type Row = { label: string; sub?: string; cents: number };
  const rows: Row[] = [];
  legs.forEach((leg, i) => {
    const flight = leg.flight?.value;
    const hotel = leg.hotel?.value;
    const out = flight?.outbound?.[0];
    const legTag = legs.length > 1 ? `Leg ${i + 1} ` : "";
    if (flight && out) {
      rows.push({
        label: `${legTag}flight`,
        sub: `${out.carrier}${out.flight_number} ${out.origin} to ${out.destination}`,
        cents: cents(flight.total_price_cents),
      });
    }
    if (hotel) {
      rows.push({
        label: `${legTag}hotel`,
        sub: `${hotel.name} · ${hotel.nights} ${hotel.nights === 1 ? "night" : "nights"}`,
        cents: cents(hotel.total_price_cents),
      });
    }
  });
  if (option.return_flight) {
    const rf = option.return_flight.value;
    const out = rf?.outbound?.[0];
    if (out) {
      rows.push({
        label: "Return flight",
        sub: `${out.carrier}${out.flight_number} ${out.origin} to ${out.destination}`,
        cents: cents(rf.total_price_cents),
      });
    }
  }
  // Reconciliation: if rows + Total don't match (taxes, fees, conversion),
  // surface the residual as its own row so the card never silently lies
  // about how its math adds up.
  const totalCents = cents(option.total_price_cents);
  const summed = rows.reduce((acc, r) => acc + r.cents, 0);
  const residual = totalCents - summed;
  // Tolerance: < $1 swings are rounding noise from per-row formatting.
  const RESIDUAL_TOLERANCE_CENTS = 100;

  return (
    <div className="mt-8 pt-6" style={{ borderTop: "1px solid var(--hairline)" }}>
      <div className="text-[11px] uppercase tracking-[0.16em] text-[var(--text-faint)] mb-4">
        Cost breakdown
      </div>
      <ul className="space-y-3">
        {rows.map((r, i) => (
          <li key={i} className="flex items-baseline justify-between gap-4">
            <div className="min-w-0 flex-1">
              <div className="text-[14px] text-[var(--text-soft)]">{r.label}</div>
              {r.sub && (
                <div className="text-[12px] text-[var(--text-mute)] mt-0.5 truncate">
                  {r.sub}
                </div>
              )}
            </div>
            <div className="text-[14px] text-[var(--text)] numerals-tabular shrink-0">
              {fmtMoney(r.cents)}
            </div>
          </li>
        ))}
        {Math.abs(residual) > RESIDUAL_TOLERANCE_CENTS && (
          <li className="flex items-baseline justify-between gap-4">
            <div className="min-w-0 flex-1">
              <div className="text-[14px] text-[var(--text-soft)]">
                Taxes &amp; fees
              </div>
              <div className="text-[12px] text-[var(--text-mute)] mt-0.5">
                Difference between line items and total charged
              </div>
            </div>
            <div className="text-[14px] text-[var(--text)] numerals-tabular shrink-0">
              {residual >= 0 ? fmtMoney(residual) : `-${fmtMoney(-residual)}`}
            </div>
          </li>
        )}
      </ul>
      <div
        className="mt-4 pt-4 flex items-baseline justify-between gap-4"
        style={{ borderTop: "1px solid var(--hairline)" }}
      >
        <div className="text-[14px] font-medium text-[var(--text)]">Total</div>
        <div className="display-md text-[18px] text-[var(--text)] numerals-tabular">
          {total}
        </div>
      </div>
    </div>
  );
}

function FactUnavailable({ label, message }: { label: string; message: string }) {
  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <span className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-mute)] font-medium">
          {label}
        </span>
      </div>
      <div className="text-[13px] text-[var(--text-faint)] italic">
        {message}
      </div>
    </div>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-[0.15em] text-[var(--text-faint)] mb-1.5">
        {label}
      </div>
      <div className="leading-relaxed">{children}</div>
    </div>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`h-3 w-3 transition-transform duration-300 ${open ? "rotate-180" : ""}`}
    >
      <path d="M3.5 6L8 10.5L12.5 6" />
    </svg>
  );
}
