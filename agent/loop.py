"""Agentic tool-use loop driven by Claude's native tool-use API.

The LLM picks the next action: which search to run, with what parameters,
when to retry with broader filters, when it has enough data to ship. The
orchestrator dispatches tools, feeds slim summaries back, and stops when
Claude calls the terminal `present_options` tool.

Fact firewall (load-bearing): Claude proposes its three options as
(flight_id, hotel_id) tuples drawn from the search results it has seen.
The orchestrator looks each ID up in its own registry of recorded tool
calls and constructs the ItineraryOption with grounded provenance. Claude
never directly emits a price, hotel name, or any other fact that ends up
in the user-facing option. If Claude returns an unknown ID, the option is
rejected and we fall back to the deterministic picker on whatever data
the loop already gathered.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.budget import BudgetExceeded, RequestBudget
from agent.tools_schema import ALL_TOOLS
from api.config import settings
from api.schemas import (
    FlightOffer,
    Grounded,
    HotelOffer,
    ItineraryOption,
    LegOption,
    Provenance,
    TripRequest,
    WeatherSummary,
)
from audit.log import AuditLog
from memory.profile import UserProfile


MAX_ITERATIONS = 20    # hard cap; ~20 turns is enough for a 2-leg trip with one replan
LOOP_MODEL_MAX_TOKENS = 4000


# ----- Per-loop offer registry --------------------------------------------------

@dataclass
class OfferRegistry:
    """Maps each offer ID Claude has seen back to the tool_call_id that
    produced it. Used to (a) ground the final ItineraryOption with real
    provenance and (b) reject any offer ID Claude invents.

    Multiple search calls per leg are fine — later calls just overwrite
    earlier entries for the same offer ID, which still points at a real
    tool_call_id for that ID.
    """
    # Each offer-id -> (leg_index, tool_call_id, offer). Latest call wins on
    # collision (later searches reflect more current pricing/availability).
    # The leg_index lets the fallback path bucket offers per leg without
    # reverse-inferring from stay-window heuristics.
    flights: dict[str, tuple[int, str, FlightOffer]] = field(default_factory=dict)
    return_flights: dict[str, tuple[int, str, FlightOffer]] = field(default_factory=dict)
    hotels: dict[str, tuple[int, str, HotelOffer]] = field(default_factory=dict)
    weather_by_leg: dict[int, tuple[str, WeatherSummary]] = field(default_factory=dict)

    def record_flights(self, *, leg_index: int, tool_call_id: str,
                       offers: list[FlightOffer], is_return: bool) -> None:
        bucket = self.return_flights if is_return else self.flights
        for o in offers:
            bucket[o.id] = (leg_index, tool_call_id, o)

    def record_hotels(self, *, leg_index: int, tool_call_id: str,
                      offers: list[HotelOffer]) -> None:
        for o in offers:
            self.hotels[o.id] = (leg_index, tool_call_id, o)

    def record_weather(self, *, tool_call_id: str, leg_index: int,
                       weather: WeatherSummary) -> None:
        self.weather_by_leg[leg_index] = (tool_call_id, weather)

    def offers_for_leg(self, leg_index: int, *, kind: str) -> list[tuple[str, object]]:
        """Return [(tool_call_id, offer)] tuples for a given leg, latest only.

        kind ∈ {"flights", "hotels", "return_flights"}. Used by the fallback
        path to bucket offers by leg without reverse-inferring from dates.
        """
        bucket = getattr(self, kind)
        return [(cid, off) for (li, cid, off) in bucket.values()
                if li == leg_index]


# ----- LLM-facing summaries (slim, to keep token usage down) --------------------

def _summarize_flights(offers: list[FlightOffer], cap: int = 8) -> list[dict]:
    out: list[dict] = []
    for o in offers[:cap]:
        out.append({
            "id": o.id,
            "total_price_cents": o.total_price_cents,
            "currency": o.currency,
            "outbound": [
                {
                    "carrier": s.carrier,
                    "flight_number": s.flight_number,
                    "origin": s.origin,
                    "destination": s.destination,
                    "depart": s.depart[:16],
                    "arrive": s.arrive[:16],
                    "duration_minutes": s.duration_minutes,
                    "refundable": s.refundable,
                }
                for s in o.outbound
            ],
        })
    return out


def _summarize_hotels(offers: list[HotelOffer], cap: int = 8) -> list[dict]:
    out: list[dict] = []
    for o in offers[:cap]:
        out.append({
            "id": o.id,
            "name": o.name,
            "neighborhood": o.neighborhood,
            "star_rating": o.star_rating,
            "nightly_rate_cents": o.nightly_rate_cents,
            "total_price_cents": o.total_price_cents,
            "nights": o.nights,
            "refundable_until": o.refundable_until,
            "review_signals": o.review_signals,
        })
    return out


def _summarize_weather(w: WeatherSummary | None) -> dict | None:
    if w is None:
        return None
    return {
        "location": w.location,
        "summary": w.summary,
        "avg_high_c": w.avg_high_c,
        "avg_low_c": w.avg_low_c,
        "rain_probability": w.rain_probability,
    }


# ----- System prompt --------------------------------------------------------

def _build_system_prompt(trip: TripRequest, profile: UserProfile) -> str:
    profile_text = profile.to_prompt_summary()
    profile_section = (
        f"\n\nUser profile (apply when nothing in the current request "
        f"conflicts):\n{profile_text}"
        if profile_text else ""
    )
    luxury_signal = _luxury_signal(trip)
    luxury_block = (
        f"\n\nLUXURY SIGNAL DETECTED: {luxury_signal}\n"
        "Treat this trip as premium: search with cabin_class='business' (or "
        "'first' for ultra-premium asks), set min_star_rating=4 or 5 on hotels, "
        "and pick options that approach or exceed the budget floor rather than "
        "undercutting it."
        if luxury_signal else ""
    )

    return f"""\
You are a trip-planning agent. Your job is to pick three concrete itineraries
for the user's request, then ship them via present_options.

You have four tools: search_flights, search_hotels, get_weather, and
present_options. The first three return real, current data; present_options
is terminal.

USER-FIRST THINKING (the most important rule):

Every decision — which searches to run, which offers to pick, how to label
each option, what to put in why_this_one — must serve THIS user's stated
intent and signals, not a generic template. Read the raw request carefully
and ask: what is this person actually picturing? A quiet boutique stay or
a flagship five-star? A direct flight or willing to connect for savings?
A view, a neighborhood, a vibe? Pick the three options that would actually
make THIS person say "yes, exactly". Generic outputs ('cheapest', 'best
reviewed', 'alternative') waste the user's attention. Specific outputs
('Rooftop pool with skyline', 'Refundable + late checkout', 'Boutique in
the old town') earn it.

How to plan:

1. Search flights and hotels for every leg. Multi-leg trips need one search
   pair per leg. If the trip ends somewhere other than the user's home origin,
   ALSO search a return flight (use leg_index=-1 to mark it as a return).
2. Search weather for each leg's stay window once.
3. If a search returns 0 results, retry with a broader window (dates + 2
   days each side) or a higher max_price_cents (raise by 50%). If a metro
   code resolves to obscure airports, try the metro's primary hub explicitly
   (LON to LHR, NYC to JFK, PAR to CDG, TYO to HND, MIL to MXP, ROM to FCO).
4. When picks for all legs feel solid, call present_options exactly once
   with three options that differ on at least one axis.

Budget interpretation (CRITICAL — this is where most agents fail):

- budget_max_usd is a CEILING. Stay below it.
- budget_min_usd is a HARD FLOOR. The user is telling you they want a trip
  that costs AT LEAST this much. They are NOT asking for the cheapest
  option below it.
- When budget_min_usd is set:
    a) Set max_price_cents on your searches to roughly 2x the per-leg floor
       so premium offers actually surface (a tight ceiling hides them).
    b) ALL THREE options you ship must total at or above budget_min_usd.
       Not just one — all three. If only one combination hits the floor,
       the user does not have three real choices.
    c) If even your most premium combination falls short of the floor,
       ship the three HIGHEST-TOTALLING combinations you can build and
       state the gap in why_this_one AND the_catch for every option
       ("$2,160 below your $10,000 target — premium inventory limited
       on these dates"). Do not silently undercut.
- For luxury trips, cabin_class='business' typically pushes a transatlantic
  flight from $700 to $4,000+. Use it. min_star_rating=5 on hotels typically
  pushes nightly rates from $200 to $700+. Combine both to hit a $10K+ trip.

Ranking (rank 1 is YOUR top recommendation, not a fixed formula):

- rank 1 = the option that BEST MATCHES the user's stated intent. If they
  asked for the best experience, rank 1 is the most premium pick. If they
  asked for the cheapest, rank 1 is the lowest total. If they gave a budget
  floor, rank 1 is the option that lands closest to the floor with the
  highest quality.
- rank 2 and 3 are strong alternatives that differ on a meaningful axis
  (price, refundability, neighborhood, cabin class, hotel rating).
- Use free-form labels per option that name the actual tradeoff axis for
  THIS request: 'luxury', 'value', 'balanced', 'refundable', 'fastest',
  'central', 'closest-to-target', etc. Each option's label should differ.

Hard rules (the orchestrator enforces these — violations bounce you back):

- Never invent a flight or hotel ID. Every ID in present_options must come
  from a search result you actually received.
- Use real numeric deltas in why_this_one — don't say "much cheaper", say
  "$340 cheaper than Option 2".
- the_catch must be 1-3 short bullets, never empty.
- One of the three options should be refundable when refundable fares are
  available.

Trip context:

- Origin (home): {trip.origin}
- Legs:
{_legs_block(trip)}
- Total budget hint: ${trip.budget_total_usd or 'unspecified'}
- Travelers: {trip.traveler_count}
- Hard constraints: {trip.hard_constraints or '(none)'}
- Soft preferences: {trip.soft_preferences or '(none)'}
- User's raw request: {trip.raw_text!r}
{luxury_block}{profile_section}

Be efficient. Two parallel tool calls per turn is fine. Aim to converge in
5-10 turns total.
"""


_LUXURY_KEYWORDS = (
    "best experience", "luxury", "premium", "five star", "5-star", "5 star",
    "first class", "business class", "high end", "high-end", "splurge",
    "luxurious", "upscale", "lavish", "fancy",
)


def _luxury_signal(trip: TripRequest) -> str:
    """Detect 'I want to spend big' signals in raw text or budget shape.

    Returns a short explanation of what triggered the signal, or "" if none.
    Used to flip the system prompt into premium-mode so Claude reaches for
    business class and 5-star hotels by default.
    """
    text = (trip.raw_text or "").lower()
    for kw in _LUXURY_KEYWORDS:
        if kw in text:
            return f"raw text contains {kw!r}"
    # High per-leg floor is itself a luxury signal: $5K+ for one leg means
    # the user is asking for premium even without using the word.
    floors = [L.budget_min_usd for L in trip.legs if L.budget_min_usd]
    if floors:
        per_leg_max = max(floors)
        if per_leg_max >= 5000:
            return f"per-leg minimum spend of ${per_leg_max:.0f} signals premium"
    return ""


def _legs_block(trip: TripRequest) -> str:
    """Render the legs section of the system prompt with semantically correct
    date labels. For multi-leg trips, leg N's date_end is when the user leaves
    for the next city, NOT a return home — calling it 'return' was misleading
    Claude into searching round-trips per leg."""
    out: list[str] = []
    n_legs = len(trip.legs)
    for i, L in enumerate(trip.legs):
        if n_legs == 1:
            date_label = f"depart {L.date_start}, head home {L.date_end}"
        elif i < n_legs - 1:
            date_label = (
                f"arrive {L.date_start}, leave for next city {L.date_end}"
            )
        else:
            date_label = f"arrive {L.date_start}, head home {L.date_end}"
        bits = [
            f"  Leg {i}: {L.origin} -> {L.destination}",
            date_label,
        ]
        if L.budget_min_usd:
            bits.append(f"min_spend ${L.budget_min_usd:.0f}")
        if L.budget_max_usd:
            bits.append(f"max_spend ${L.budget_max_usd:.0f}")
        out.append(", ".join(bits))
    if trip.legs[-1].destination != trip.origin and not getattr(trip, "is_one_way", False):
        out.append(f"  Return: {trip.legs[-1].destination} -> {trip.origin} "
                   f"on or after {trip.legs[-1].date_end}")
    elif getattr(trip, "is_one_way", False):
        out.append("  ONE-WAY trip — user does NOT want a return flight home. "
                   "Do not call search_flights with leg_index=-1.")
    return "\n".join(out)


# ----- The loop -------------------------------------------------------------

async def run_agentic_loop(
    *,
    trip: TripRequest,
    log: AuditLog,
    budget: RequestBudget,
    profile: UserProfile,
    progress: Callable[[str, dict], None] | None = None,
) -> list[ItineraryOption]:
    """Run Claude through the search/replan/synthesize loop, return 3 options."""
    import anthropic
    from agent.orchestrator import (
        ToolHandle,
        _call_tool,
        _resolve_airport,
        build_tool_registry,
        grounded,
    )

    tools_impl = build_tool_registry(use_mock=None)
    registry = OfferRegistry()

    system = _build_system_prompt(trip, profile)
    user_msg = (
        f"Plan this trip: {trip.raw_text!r}\n\n"
        "Search what you need, then call present_options with three picks."
    )
    conversation: list[dict] = [{"role": "user", "content": user_msg}]

    cli = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    log.append("agent", "loop.started", {
        "model": settings.llm_model_primary,
        "leg_count": len(trip.legs),
        "max_iterations": MAX_ITERATIONS,
    }, request_id=trip.request_id)

    final_picks: dict | None = None
    last_stop_reason: str | None = None
    iteration = 0
    iterations_exhausted = False

    for iteration in range(MAX_ITERATIONS):
        try:
            budget.charge("llm_synth")
        except BudgetExceeded as e:
            log.append("agent", "loop.budget_exceeded", {
                "iteration": iteration, "reason": str(e),
            }, request_id=trip.request_id)
            break

        # Transient API errors (rate limit, 5xx) get a short retry with
        # exponential backoff. Permanent errors and unexpected exceptions
        # break the loop and let the fallback path kick in.
        resp = None
        for attempt in range(3):
            try:
                resp = await cli.messages.create(
                    model=settings.llm_model_primary,
                    max_tokens=LOOP_MODEL_MAX_TOKENS,
                    system=system,
                    tools=ALL_TOOLS,
                    messages=conversation,
                )
                break
            except Exception as e:
                err_name = type(e).__name__
                is_retryable = err_name in (
                    "RateLimitError", "APIStatusError", "APIConnectionError",
                    "APITimeoutError",
                )
                log.append("agent", "loop.api_error", {
                    "iteration": iteration, "attempt": attempt,
                    "error": f"{err_name}: {e}",
                    "retryable": is_retryable,
                }, request_id=trip.request_id)
                if is_retryable and attempt < 2:
                    await asyncio.sleep(1.0 * (2 ** attempt))    # 1s, 2s
                    continue
                resp = None
                break
        if resp is None:
            break

        last_stop_reason = resp.stop_reason
        log.append("agent", "loop.turn", {
            "iteration": iteration,
            "stop_reason": resp.stop_reason,
            "input_tokens": getattr(resp.usage, "input_tokens", None),
            "output_tokens": getattr(resp.usage, "output_tokens", None),
        }, request_id=trip.request_id)

        if resp.stop_reason != "tool_use":
            # end_turn or max_tokens or anything else — Claude has no more tool calls.
            break

        # Persist the assistant message verbatim so the next turn keeps tool_use IDs.
        conversation.append({
            "role": "assistant",
            "content": [_block_to_dict(b) for b in resp.content],
        })

        tool_use_blocks = [b for b in resp.content if b.type == "tool_use"]

        # Terminal tool short-circuits the loop.
        terminal = next((b for b in tool_use_blocks if b.name == "present_options"), None)
        if terminal is not None:
            final_picks = terminal.input
            log.append("agent", "loop.present_options", {
                "iteration": iteration,
                "option_count": len(final_picks.get("options", [])),
            }, request_id=trip.request_id)
            break

        if progress:
            progress("loop_tools", {
                "iteration": iteration,
                "tools": [b.name for b in tool_use_blocks],
            })

        # Dispatch all non-terminal tool calls in parallel.
        results = await asyncio.gather(*[
            _dispatch(
                block=b, tools_impl=tools_impl, registry=registry,
                trip=trip, log=log, budget=budget,
                resolve_airport=_resolve_airport, call_tool=_call_tool,
            )
            for b in tool_use_blocks
        ], return_exceptions=False)

        tool_results_content: list[dict] = []
        for block, result in zip(tool_use_blocks, results):
            tool_results_content.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
                "is_error": result.get("error") is not None,
            })
        conversation.append({"role": "user", "content": tool_results_content})
    else:
        # The for-loop exhausted MAX_ITERATIONS without a `break` — Claude
        # never converged on present_options.
        iterations_exhausted = True

    log.append("agent", "loop.finished", {
        "stop_reason": last_stop_reason,
        "had_terminal_call": final_picks is not None,
        "iteration_count": iteration + 1,
        "iterations_exhausted": iterations_exhausted,
        "flights_known": len(registry.flights),
        "hotels_known": len(registry.hotels),
        "return_flights_known": len(registry.return_flights),
    }, request_id=trip.request_id)

    if final_picks is not None:
        try:
            options = _materialize_options(
                trip=trip, picks=final_picks, registry=registry,
                grounded_fn=grounded,
            )
        except _LLMPickError as e:
            log.append("agent", "loop.picks_invalid", {
                "reason": str(e),
            }, request_id=trip.request_id)
        else:
            # Fact firewall: Claude's `why_this_one` text passed through
            # verbatim until now. The system prompt asks for real numeric
            # deltas but nothing validated them, so a fabricated "$340 cheaper"
            # claim could end up on the user's card. Verify each dollar
            # amount in why_this_one matches an actual total or pairwise
            # delta within 5%; replace stray amounts with [$?] and flag.
            _scrub_fabricated_deltas(options, log=log, request_id=trip.request_id)

            # Audit any options that undercut the user's floor. We don't
            # reject them — the alternative is shipping nothing — but the
            # event makes the violation traceable.
            floor_cents = _trip_floor_cents(trip)
            if floor_cents > 0:
                undercuts = [
                    {"rank": o.rank, "total_cents": o.total_price_cents.value,
                     "gap_cents": floor_cents - o.total_price_cents.value}
                    for o in options
                    if o.total_price_cents.value < floor_cents
                ]
                if undercuts:
                    log.append("agent", "loop.floor_undercut", {
                        "floor_cents": floor_cents,
                        "undercuts": undercuts,
                    }, request_id=trip.request_id)
            return options

    # Fallback: deterministic ranking on whatever the loop gathered. Lets
    # the user still see options even if Claude bailed before present_options
    # or returned malformed picks.
    return _fallback_options(trip=trip, registry=registry, grounded_fn=grounded)


# ----- Tool dispatch --------------------------------------------------------

class _LLMPickError(ValueError):
    pass


def _block_to_dict(block: Any) -> dict:
    """Convert an Anthropic content block into the dict form expected on the
    next turn's `assistant` message. SDK blocks are typed objects; we need
    JSON-friendly dicts for messages.create."""
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {
            "type": "tool_use", "id": block.id,
            "name": block.name, "input": block.input,
        }
    # Unknown block type — round-trip via the SDK's serializer if available.
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return {"type": getattr(block, "type", "unknown")}


async def _dispatch(
    *, block: Any, tools_impl: dict, registry: OfferRegistry,
    trip: TripRequest, log: AuditLog, budget: RequestBudget,
    resolve_airport: Callable[[str], str],
    call_tool: Callable,
) -> dict:
    """Run a single tool_use block. Returns a slim dict suitable for tool_result."""
    name = block.name
    args = block.input or {}

    try:
        budget.charge(name)
    except BudgetExceeded as e:
        return {"error": f"budget exceeded: {e}", "tool": name}

    if name == "search_flights":
        leg_index = int(args.get("leg_index", 0))
        is_return = leg_index < 0
        # Resolve metro codes to primary hubs so providers don't return regional airports.
        resolved_origin = resolve_airport(str(args["origin"]))
        resolved_dest = resolve_airport(str(args["destination"]))

        # Validate that a leg_index=-1 search actually goes from the last leg's
        # destination back to the home origin. Otherwise Claude could record an
        # outbound posing as a return, and _materialize_options would happily
        # accept it. Reject the bad route up front so the registry stays clean.
        if is_return:
            expected_origin = resolve_airport(str(trip.legs[-1].destination))
            expected_dest = resolve_airport(str(trip.origin or ""))
            if (resolved_origin, resolved_dest) != (expected_origin, expected_dest):
                return {
                    "error": (
                        f"leg_index=-1 must search {expected_origin} -> "
                        f"{expected_dest} (last leg destination back to home), "
                        f"got {resolved_origin} -> {resolved_dest}"
                    ),
                    "leg_index": leg_index,
                }

        flight_args = {
            "origin": resolved_origin,
            "destination": resolved_dest,
            "date_start": str(args["date_start"]),
            "date_end": str(args["date_end"]),
            "max_price_cents": int(args["max_price_cents"]),
            "traveler_count": int(args.get("traveler_count", trip.traveler_count)),
            "cabin_class": str(args.get("cabin_class", "economy")),
            # Every flight search the agent makes is one-way by contract. Round
            # trips are assembled by calling search_flights twice (outbound +
            # return as a separate leg_index=-1 call).
            "one_way": True,
        }
        call = await call_tool(
            tools_impl["search_flights"], flight_args,
            log=log, request_id=trip.request_id,
        )
        if call.get("error"):
            return {"error": call["error"], "leg_index": leg_index}
        offers = call["result"] or []
        registry.record_flights(
            leg_index=leg_index,
            tool_call_id=call["tool_call_id"],
            offers=offers, is_return=is_return,
        )
        return {
            "leg_index": leg_index,
            "is_return": is_return,
            "result_count": len(offers),
            "offers": _summarize_flights(offers),
        }

    if name == "search_hotels":
        from agent.orchestrator import _resolve_metro
        leg_index = int(args.get("leg_index", 0))
        hotel_args = {
            # Hotels are keyed by metro, not airport — if the LLM passed LHR,
            # rewrite to LON so LiteAPI/Amadeus city maps actually find inventory.
            "destination": _resolve_metro(str(args["destination"])),
            "check_in": str(args["check_in"]),
            "check_out": str(args["check_out"]),
            "max_nightly_cents": int(args["max_nightly_cents"]),
            "traveler_count": int(args.get("traveler_count", trip.traveler_count)),
        }
        call = await call_tool(
            tools_impl["search_hotels"], hotel_args,
            log=log, request_id=trip.request_id,
        )
        if call.get("error"):
            return {"error": call["error"], "leg_index": leg_index}
        offers = call["result"] or []
        # Post-search filters: providers return mixed inventory; the loop
        # applies min_star_rating and min_nightly_cents on top so any
        # provider supports luxury filtering without protocol changes.
        min_star = args.get("min_star_rating")
        min_nightly = args.get("min_nightly_cents")
        if min_star is not None:
            try:
                threshold = float(min_star)
                offers = [o for o in offers if o.star_rating >= threshold]
            except (TypeError, ValueError):
                pass
        if min_nightly is not None:
            try:
                threshold_cents = int(min_nightly)
                offers = [o for o in offers if o.nightly_rate_cents >= threshold_cents]
            except (TypeError, ValueError):
                pass
        registry.record_hotels(
            leg_index=leg_index,
            tool_call_id=call["tool_call_id"],
            offers=offers,
        )
        return {
            "leg_index": leg_index,
            "result_count": len(offers),
            "offers": _summarize_hotels(offers),
        }

    if name == "get_weather":
        from agent.orchestrator import _resolve_metro
        leg_index = int(args.get("leg_index", 0))
        weather_args = {
            "location": _resolve_metro(str(args["location"])),
            "window_start": str(args["window_start"]),
            "window_end": str(args["window_end"]),
        }
        call = await call_tool(
            tools_impl["get_weather"], weather_args,
            log=log, request_id=trip.request_id,
        )
        if call.get("error"):
            return {"error": call["error"], "leg_index": leg_index}
        weather = call["result"]
        registry.record_weather(
            tool_call_id=call["tool_call_id"], leg_index=leg_index, weather=weather,
        )
        return {"leg_index": leg_index, "weather": _summarize_weather(weather)}

    return {"error": f"unknown tool {name}"}


# ----- Materialize Claude's picks into ItineraryOption objects --------------

def _materialize_options(
    *,
    trip: TripRequest,
    picks: dict,
    registry: OfferRegistry,
    grounded_fn: Callable,
) -> list[ItineraryOption]:
    raw_options = picks.get("options") or []
    if len(raw_options) != 3:
        raise _LLMPickError(f"expected 3 options, got {len(raw_options)}")

    # Sort by rank and validate coverage.
    raw_options = sorted(raw_options, key=lambda o: int(o.get("rank", 0)))

    # Anthropic's tool-input enums are advisory: Claude may return [1,1,1] or
    # rank=4. Reject those before they collide on opt_id and overwrite snapshots.
    ranks = [int(o.get("rank", 0)) for o in raw_options]
    if ranks != [1, 2, 3]:
        raise _LLMPickError(
            f"ranks must be exactly [1, 2, 3] with no duplicates; got {ranks}"
        )

    has_return = (
        trip.legs[-1].destination != trip.origin
        and not getattr(trip, "is_one_way", False)
    )
    options: list[ItineraryOption] = []

    for raw in raw_options:
        rank = int(raw.get("rank", 0))
        label = str(raw.get("label", "")) or ("cheapest" if rank == 1
            else "best_reviewed" if rank == 2 else "alternative")
        why = str(raw.get("why_this_one") or "").strip() or _default_why(label)
        catch_raw = raw.get("the_catch") or []
        catch = [str(c).strip() for c in catch_raw if str(c).strip()][:3] or [
            "No notable trade-offs flagged.",
        ]

        leg_picks = raw.get("picks_per_leg") or []
        if len(leg_picks) != len(trip.legs):
            raise _LLMPickError(
                f"option {rank} has {len(leg_picks)} legs, trip has {len(trip.legs)}",
            )

        leg_options: list[LegOption] = []
        leg_total = 0
        for raw_pick in sorted(leg_picks, key=lambda p: p.get("leg_index", 0)):
            leg_idx = int(raw_pick.get("leg_index"))
            if not 0 <= leg_idx < len(trip.legs):
                raise _LLMPickError(f"option {rank}: leg_index {leg_idx} out of range")
            flight_id = str(raw_pick.get("flight_id"))
            hotel_id = str(raw_pick.get("hotel_id"))
            if flight_id not in registry.flights:
                raise _LLMPickError(
                    f"option {rank} leg {leg_idx}: flight_id {flight_id} not in search results",
                )
            if hotel_id not in registry.hotels:
                raise _LLMPickError(
                    f"option {rank} leg {leg_idx}: hotel_id {hotel_id} not in search results",
                )
            _f_leg, f_call_id, flight = registry.flights[flight_id]
            _h_leg, h_call_id, hotel = registry.hotels[hotel_id]
            weather_entry = registry.weather_by_leg.get(leg_idx)
            weather_grounded = None
            if weather_entry is not None:
                w_call_id, weather = weather_entry
                weather_grounded = grounded_fn(
                    weather, tool_call_id=w_call_id, json_path="$",
                )
            leg_cost = flight.total_price_cents + hotel.total_price_cents
            leg_total += leg_cost
            leg = trip.legs[leg_idx]
            leg_options.append(LegOption(
                leg_index=leg_idx,
                origin=leg.origin,
                destination=leg.destination,
                flight=grounded_fn(
                    flight, tool_call_id=f_call_id,
                    json_path=f"$.flights[?(@.id=='{flight.id}')]",
                ),
                hotel=grounded_fn(
                    hotel, tool_call_id=h_call_id,
                    json_path=f"$.hotels[?(@.id=='{hotel.id}')]",
                ),
                weather=weather_grounded,
                leg_total_cents=leg_cost,
            ))

        return_grounded = None
        if has_return:
            rf_id = raw.get("return_flight_id")
            if rf_id and rf_id in registry.return_flights:
                _rf_leg, rf_call_id, rf_offer = registry.return_flights[rf_id]
                return_grounded = grounded_fn(
                    rf_offer, tool_call_id=rf_call_id,
                    json_path=f"$.flights[?(@.id=='{rf_offer.id}')]",
                )
                leg_total += rf_offer.total_price_cents
            elif rf_id and rf_id not in registry.return_flights:
                raise _LLMPickError(
                    f"option {rank}: return_flight_id {rf_id} not in search results",
                )

        leg0_opt = leg_options[0]
        currency = leg0_opt.flight.value.currency

        options.append(ItineraryOption(
            id=f"opt_{trip.request_id}_{rank}",
            request_id=trip.request_id,
            rank=rank,
            tradeoff_label=label,
            why_this_one=why,
            the_catch=catch,
            legs=leg_options,
            return_flight=return_grounded,
            flight=leg0_opt.flight,
            hotel=leg0_opt.hotel,
            weather=leg0_opt.weather,
            total_price_cents=grounded_fn(
                leg_total,
                tool_call_id=leg0_opt.flight.prov.tool_call_id,
                json_path="sum($.legs[].leg_total_cents) + $.return_flight.total_price_cents",
            ),
            currency=grounded_fn(
                currency,
                tool_call_id=leg0_opt.flight.prov.tool_call_id,
                json_path="$.legs[0].flight.currency",
            ),
        ))
    return options


_DOLLAR_RE = __import__("re").compile(
    r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*([Kk])?",
)


def _scrub_fabricated_deltas(
    options: list[ItineraryOption],
    *,
    log: AuditLog,
    request_id: str,
    tolerance: float = 0.05,
) -> None:
    """Verify dollar amounts in `why_this_one` match real totals or pairwise
    deltas within `tolerance`. Replace mismatches with `[$?]` and append a
    note to `the_catch`. Mutates options in place.

    The system prompt asks Claude for real numeric deltas, but until now
    nothing checked. A fabricated "$340 cheaper than Option 2" claim flowed
    straight to the user-visible card — exactly the kind of fact-firewall
    leak the orchestrator's docstring promised to prevent.
    """
    if not options:
        return
    totals = [o.total_price_cents.value for o in options]
    # Set of every amount Claude could plausibly cite, in cents.
    valid_amounts: set[int] = set()
    for t in totals:
        valid_amounts.add(t)
        for u in totals:
            if u != t:
                valid_amounts.add(abs(t - u))
    # Per-leg components are also fair game (Claude may cite a flight or
    # hotel cost in the narrative).
    for opt in options:
        for L in opt.legs:
            valid_amounts.add(L.flight.value.total_price_cents)
            valid_amounts.add(L.hotel.value.total_price_cents)
        if opt.return_flight is not None:
            valid_amounts.add(opt.return_flight.value.total_price_cents)
    valid_amounts = {a for a in valid_amounts if a > 0}

    def _within(claimed_cents: int) -> bool:
        for ref in valid_amounts:
            if ref == 0:
                continue
            if abs(claimed_cents - ref) / ref <= tolerance:
                return True
        return False

    for opt in options:
        text = opt.why_this_one or ""
        scrubbed_any = False
        new_text_parts: list[str] = []
        last = 0
        for m in _DOLLAR_RE.finditer(text):
            raw_num = m.group(1).replace(",", "")
            try:
                amount = float(raw_num)
            except ValueError:
                continue
            if (m.group(2) or "").lower() == "k":
                amount *= 1000.0
            claimed_cents = int(round(amount * 100))
            new_text_parts.append(text[last:m.start()])
            if _within(claimed_cents):
                new_text_parts.append(m.group(0))
            else:
                new_text_parts.append("[$?]")
                scrubbed_any = True
            last = m.end()
        if scrubbed_any:
            new_text_parts.append(text[last:])
            opt.why_this_one = "".join(new_text_parts)
            note = "Some prices in this option's narrative didn't match real totals; check Cost breakdown."
            opt.the_catch = ([note] + list(opt.the_catch or []))[:3]
            log.append("agent", "loop.fact_scrubbed", {
                "option_id": opt.id,
                "rank": opt.rank,
                "scrubbed_text": opt.why_this_one,
            }, request_id=request_id)


def _trip_floor_cents(trip: TripRequest) -> int:
    """Sum per-leg budget_min_usd into a trip-level floor in cents.

    Returns 0 when no leg has a min set, so the audit step is a no-op for
    requests without an explicit floor.
    """
    floors = [L.budget_min_usd for L in trip.legs if L.budget_min_usd]
    if not floors:
        return 0
    return int(sum(floors) * 100)


def _default_why(label: str) -> str:
    return {
        "cheapest": "Lowest combined total across all legs.",
        "best_reviewed": "Highest-rated hotel pick.",
        "alternative": "Different tradeoff axis from the other two.",
    }.get(label, "Distinct option.")


# ----- Fallback when Claude bails or returns invalid picks ------------------

def _fallback_options(
    *,
    trip: TripRequest,
    registry: OfferRegistry,
    grounded_fn: Callable,
) -> list[ItineraryOption]:
    """Run the deterministic picker on whatever the registry holds when
    Claude bailed out or returned malformed picks.

    Buckets offers by leg_index recorded at registration time, NOT by date or
    destination heuristics — that was wrong: when Claude broadens dates on a
    retry, the nights match would silently drop every hotel. Weather is
    optional; absence is mirrored as `None` rather than failing the trip.
    """
    from agent.orchestrator import _build_three_options

    leg_payloads: list[dict] = []
    for i, leg in enumerate(trip.legs):
        flight_entries = registry.offers_for_leg(i, kind="flights")
        hotel_entries = registry.offers_for_leg(i, kind="hotels")
        if not flight_entries or not hotel_entries:
            # Genuinely no inventory for this leg — can't build any option.
            return []

        flights = [o for (_, o) in flight_entries]
        hotels = [o for (_, o) in hotel_entries]
        # Citation tool_call_id: the most recent recorded for this leg.
        flights_call_id = flight_entries[-1][0]
        hotels_call_id = hotel_entries[-1][0]

        weather_entry = registry.weather_by_leg.get(i)
        if weather_entry is not None:
            weather_call_id, weather = weather_entry
        else:
            # Weather is decorative; missing weather is fine. Cite the flights
            # call so provenance points somewhere real.
            weather = None
            weather_call_id = flights_call_id

        leg_payloads.append({
            "leg_index": i,
            "flights": flights,
            "hotels": hotels,
            "weather": weather,
            "flights_call_id": flights_call_id,
            "hotels_call_id": hotels_call_id,
            "weather_call_id": weather_call_id,
        })

    # Reconstruct a return-flight call dict the orchestrator's
    # _build_three_options expects. Return flights are recorded under
    # leg_index = -1 by the dispatch.
    return_flight_call = None
    if trip.legs[-1].destination != trip.origin and registry.return_flights:
        rf_entries = registry.offers_for_leg(-1, kind="return_flights")
        if rf_entries:
            rf_offers = [o for (_, o) in rf_entries]
            rf_call_id = rf_entries[-1][0]
            return_flight_call = {"tool_call_id": rf_call_id, "result": rf_offers}

    return _build_three_options(
        trip=trip,
        leg_payloads=leg_payloads,
        return_flight_call=return_flight_call,
    )
