"""Agent orchestrator: state-machine-driven tool-use loop.

Two execution modes:

  - MOCK_LLM=1: deterministic scripted plan (search flights, search hotels, get
    weather, then synthesize). Used for the offline demo and for tests.
  - MOCK_LLM=0: real Anthropic API tool-use loop with structured outputs.

The orchestrator never trusts the LLM with hard facts. After tools execute, the
orchestrator BUILDS the ItineraryOption itself — the LLM only contributes the
qualitative narrative (why_this_one, the_catch).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from agent.budget import BudgetExceeded, RequestBudget
from agent.state import AgentSession, AgentState
from api.schemas import (
    FlightOffer,
    Grounded,
    HotelOffer,
    ItineraryOption,
    LegOption,
    Provenance,
    TripLeg,
    TripRequest,
    WeatherSummary,
)
from audit.log import AuditLog, jcs_canonical, sha256_hex


PROMPT_DIR = Path(__file__).parent / "prompts"


# ----- Tool registry --------------------------------------------------------

@dataclass
class ToolHandle:
    name: str
    impl: Any


class _ChainedProvider:
    """Try each provider in declared order; return the first non-empty result.

    Plays the role of a single FlightProvider / HotelProvider / WeatherProvider
    so the rest of the orchestrator (and the agentic loop) is unchanged. Without
    this wrapper, the registry picked exactly one provider per tool at
    construction time — so a LiteAPI miss on Reykjavik silently returned []
    and Amadeus / Mock were never tried even when credentialed.

    For weather (which returns a single object, not a list), we treat None as
    "empty" and keep trying.
    """

    def __init__(self, providers: list[Any], *, kind: str) -> None:
        # Filter out duplicates (caller may pass the same MockFlightProvider
        # twice when no credentialed providers exist — the chain still
        # functions, just no-op).
        seen: set[str] = set()
        deduped: list[Any] = []
        for p in providers:
            key = f"{type(p).__module__}.{type(p).__name__}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(p)
        self._providers = deduped
        self._kind = kind
        # Expose the primary (first) provider's name so audit logs and tests
        # see the intent ("duffel" wins for flights), not the implementation
        # detail of the Mock fallback at the end of every chain.
        self.name = getattr(deduped[0], "name", "?") if deduped else "empty"
        self.chain = [getattr(p, "name", "?") for p in deduped]

    def _is_empty(self, result: Any) -> bool:
        if self._kind == "weather":
            return result is None
        # Flights / hotels return list[...]
        return not result

    async def search(self, **kwargs: Any) -> Any:
        last_result: Any = [] if self._kind != "weather" else None
        for p in self._providers:
            try:
                result = await p.search(**kwargs)
            except Exception:
                # Provider blew up — try the next one. The orchestrator's
                # _call_tool already records errors per-provider for audit.
                continue
            if not self._is_empty(result):
                return result
            last_result = result
        return last_result

    async def forecast(self, **kwargs: Any) -> Any:
        # Weather provider exposes .forecast(), not .search().
        last_result: Any = None
        for p in self._providers:
            try:
                result = await p.forecast(**kwargs)
            except Exception:
                continue
            if result is not None:
                return result
            last_result = result
        return last_result


def build_tool_registry(*, use_mock: bool | None = None) -> dict[str, ToolHandle]:
    """Construct the tool registry.

    use_mock=True       → always mock
    use_mock=False      → always real (raises if keys missing)
    use_mock=None       → auto: chain credentialed providers in declared
                          order, falling back to Mock on empty results.
    """
    from tools.flights.mock import MockFlightProvider
    from tools.hotels.mock import MockHotelProvider
    from tools.weather.mock import MockWeatherProvider

    if use_mock is True:
        return {
            "search_flights": ToolHandle("search_flights", MockFlightProvider()),
            "search_hotels": ToolHandle("search_hotels", MockHotelProvider()),
            "get_weather": ToolHandle("get_weather", MockWeatherProvider()),
        }

    # Re-resolve settings so test-time monkeypatching of env vars takes effect.
    from api.config import settings as cur_settings

    # Build provider chains in declared preference order. Mock is always
    # last so a credentialed-but-empty result still degrades to deterministic
    # fixtures rather than failing the whole request.
    flight_chain: list[Any] = []
    hotel_chain: list[Any] = []
    weather_chain: list[Any] = []

    if cur_settings.duffel_access_token:
        from tools.flights.duffel import DuffelFlightProvider
        flight_chain.append(DuffelFlightProvider())
    if cur_settings.amadeus_client_id and cur_settings.amadeus_client_secret:
        from tools.flights.amadeus import AmadeusFlightProvider
        flight_chain.append(AmadeusFlightProvider())
    flight_chain.append(MockFlightProvider())

    if cur_settings.liteapi_key:
        from tools.hotels.liteapi import LiteApiHotelProvider
        hotel_chain.append(LiteApiHotelProvider())
    if cur_settings.duffel_access_token and cur_settings.duffel_stays_enabled:
        from tools.hotels.duffel import DuffelStaysProvider
        hotel_chain.append(DuffelStaysProvider())
    if cur_settings.amadeus_client_id and cur_settings.amadeus_client_secret:
        from tools.hotels.amadeus import AmadeusHotelProvider
        hotel_chain.append(AmadeusHotelProvider())
    hotel_chain.append(MockHotelProvider())

    if cur_settings.openweather_api_key:
        from tools.weather.openweather import OpenWeatherProvider
        weather_chain.append(OpenWeatherProvider())
    weather_chain.append(MockWeatherProvider())

    return {
        "search_flights": ToolHandle(
            "search_flights", _ChainedProvider(flight_chain, kind="flights"),
        ),
        "search_hotels": ToolHandle(
            "search_hotels", _ChainedProvider(hotel_chain, kind="hotels"),
        ),
        "get_weather": ToolHandle(
            "get_weather", _ChainedProvider(weather_chain, kind="weather"),
        ),
    }


# ----- Trip request parsing -------------------------------------------------

async def parse_trip_request(
    raw_text: str, *, user_id: str, profile_summary: str = "",
) -> TripRequest:
    """Extract structured trip request via LLM (or regex fallback in MOCK_LLM).

    The intent parser may return one or more legs. We chain origins so that
    leg N's origin is leg N-1's destination, with leg 0's origin defaulting
    to "JFK" when the user didn't supply one.

    profile_summary: optional one-paragraph user-history hint passed through
    to the LLM intent prompt so ambiguous requests resolve against history.
    """
    from agent.intent import extract_intent
    intent = await extract_intent(raw_text, profile_summary=profile_summary)
    request_id = f"req_{uuid.uuid4().hex[:12]}"

    # Surface a clear error rather than silently routing to a default city.
    has_any_destination = (
        intent.destination
        or (intent.legs and any(leg.destination for leg in intent.legs))
    )
    if not has_any_destination:
        raise ValueError(
            f"Could not identify a destination in: {raw_text!r}. "
            f"Try naming a major city, e.g. 'London', 'Tokyo', 'Barcelona'."
        )

    home = intent.origin or "JFK"

    # Normalize: if the LLM/regex returned legs use them; otherwise synthesize
    # a single leg from the legacy flat fields.
    raw_legs = list(intent.legs) if intent.legs else [None]
    today = date.today()
    fallback_start = today + timedelta(days=30)

    # First pass: parse each leg_intent into (destination, dates, budget) WITHOUT
    # chaining origins. The LLM occasionally returns legs in non-chronological
    # order; if we chain origins in that order the chain becomes wrong (leg N+1
    # would originate at the wrong city).
    parsed: list[tuple[str, date, date, float | None, float | None]] = []
    last_end: date | None = None
    for leg_intent in raw_legs:
        if leg_intent is not None:
            destination = leg_intent.destination or intent.destination
            ds = leg_intent.date_start or intent.date_start
            de = leg_intent.date_end or intent.date_end
            bmin = leg_intent.budget_min_usd
            bmax = leg_intent.budget_max_usd
        else:
            destination = intent.destination
            ds = intent.date_start
            de = intent.date_end
            bmin = None
            bmax = intent.budget_total_usd

        if not destination:
            continue

        leg_start = date.fromisoformat(ds) if ds else (last_end or fallback_start)
        leg_end = date.fromisoformat(de) if de else (leg_start + timedelta(days=4))
        if leg_end <= leg_start:
            leg_end = leg_start + timedelta(days=1)
        last_end = leg_end
        parsed.append((destination, leg_start, leg_end, bmin, bmax))

    if not parsed:
        raise ValueError(
            f"Could not build any legs from: {raw_text!r}. Try naming a major city."
        )

    # Sort chronologically so chained origins reflect the actual travel order,
    # not the order Claude happened to emit them in. Stable sort preserves the
    # original order for ties (e.g., two legs starting same day — rare).
    parsed.sort(key=lambda p: p[1])

    # Second pass: chain origins now that the order is correct.
    legs: list[TripLeg] = []
    cursor_origin = home
    for destination, leg_start, leg_end, bmin, bmax in parsed:
        legs.append(TripLeg(
            origin=cursor_origin,
            destination=destination,
            date_start=leg_start,
            date_end=leg_end,
            budget_min_usd=bmin,
            budget_max_usd=bmax,
        ))
        cursor_origin = destination

    # Defensive: if the LLM gave overlapping date ranges across legs (after
    # sort), sequence them back-to-back. A traveler can't be in two cities at
    # the same time, so each leg's start must be at least the previous leg's
    # end. We preserve each leg's original duration when possible.
    legs = _sequence_legs(legs)

    leg0 = legs[0]
    legN = legs[-1]
    flat_destination = leg0.destination if len(legs) == 1 else None

    # Smart default budget: when nothing was extracted, the deterministic
    # path was hard-coding $2000 — far too low for luxury asks ("best
    # experience to Tokyo" with no number). Detect a luxury signal in the
    # raw text and bump the default so per-leg budget caps don't truncate
    # premium inventory before it surfaces.
    luxury_keywords = (
        "best experience", "luxury", "premium", "five star", "5-star",
        "5 star", "first class", "business class", "splurge", "lavish",
        "high end", "high-end", "upscale", "fancy",
    )
    raw_lower = (raw_text or "").lower()
    has_luxury_signal = any(k in raw_lower for k in luxury_keywords) or any(
        (L.budget_min_usd or 0) >= 5000 for L in legs
    )
    default_budget = 10000.0 if has_luxury_signal else 2000.0
    flat_budget = (
        intent.budget_total_usd
        or leg0.budget_max_usd
        or leg0.budget_min_usd
        or default_budget
    )

    return TripRequest(
        request_id=request_id,
        user_id=user_id,
        raw_text=raw_text,
        legs=legs,
        origin=home,
        destination=flat_destination,
        date_start=leg0.date_start,
        date_end=legN.date_end,
        date_flexibility_days=intent.date_flexibility_days,
        traveler_count=intent.traveler_count,
        budget_total_usd=flat_budget,
        is_one_way=intent.is_one_way,
        hard_constraints=intent.hard_constraints,
        soft_preferences=intent.soft_preferences,
    )


# Metro city codes that Duffel and Amadeus may resolve to obscure airports
# (e.g. LON → BQH/Biggin Hill). Map to the primary hub for nicer results.
PRIMARY_AIRPORT = {
    "LON": "LHR", "NYC": "JFK", "PAR": "CDG", "TYO": "HND",
    "MIL": "MXP", "ROM": "FCO", "OSA": "KIX", "SEL": "ICN",
    "CHI": "ORD", "YTO": "YYZ",
}

# Inverse: airport → metro/city code, used so hotel searches always receive a
# metro code (LiteAPI's CITY_COUNTRY map is keyed by metro, not airport, so
# `LHR` would silently return zero results otherwise). Includes both common
# airport codes per metro and the metro itself as a self-mapping.
AIRPORT_TO_METRO = {
    "LHR": "LON", "LGW": "LON", "STN": "LON", "LTN": "LON", "LCY": "LON", "BQH": "LON",
    "JFK": "NYC", "LGA": "NYC", "EWR": "NYC",
    "CDG": "PAR", "ORY": "PAR",
    "HND": "TYO", "NRT": "TYO",
    "MXP": "MIL", "LIN": "MIL",
    "FCO": "ROM", "CIA": "ROM",
    "KIX": "OSA", "ITM": "OSA",
    "ICN": "SEL", "GMP": "SEL",
    "ORD": "CHI", "MDW": "CHI",
    "YYZ": "YTO", "YTZ": "YTO",
}


def _resolve_airport(code: str) -> str:
    """Map a metro/city code to a primary airport so flight providers don't
    return obscure regional airports (e.g. London Biggin Hill for LON)."""
    return PRIMARY_AIRPORT.get(code.upper(), code)


def _resolve_metro(code: str) -> str:
    """Map an airport code back to its metro code so hotel providers (whose
    city maps are keyed by metro) actually find inventory. If the input is
    already a metro code or unknown, returns it unchanged."""
    return AIRPORT_TO_METRO.get(code.upper(), code)


def _sequence_legs(legs: list[TripLeg]) -> list[TripLeg]:
    """Force legs to be back-to-back so no two legs overlap.

    Why: even with a tightened intent prompt, the LLM occasionally hands back
    identical date ranges to every leg ("both legs are 7/5 to 7/15"), which
    means the agent would book hotels in two cities for the same nights. We
    detect overlap (leg_i.date_start < leg_{i-1}.date_end) and re-anchor each
    subsequent leg to start where the previous one ended, preserving the
    original duration of each leg.
    """
    if len(legs) <= 1:
        return legs

    fixed: list[TripLeg] = [legs[0]]
    for leg in legs[1:]:
        prev_end = fixed[-1].date_end
        if leg.date_start < prev_end:
            duration = max((leg.date_end - leg.date_start).days, 1)
            new_start = prev_end
            new_end = new_start + timedelta(days=duration)
            fixed.append(leg.model_copy(update={
                "date_start": new_start,
                "date_end": new_end,
            }))
        else:
            fixed.append(leg)
    return fixed


# ----- Provenance helper ----------------------------------------------------

def _now() -> str:
    return datetime.now(UTC).isoformat()


def grounded(value, *, tool_call_id: str, json_path: str) -> Grounded:
    return Grounded(
        value=value,
        prov=Provenance(tool_call_id=tool_call_id, json_path=json_path, fetched_at=_now()),
    )


# ----- Main entry point -----------------------------------------------------

async def run_agent(
    *,
    raw_text: str,
    user_id: str,
    log: AuditLog,
    progress: callable | None = None,
) -> tuple[AgentSession, list[ItineraryOption]]:
    """Run the agent loop end-to-end. Returns (session, [3 options]).

    Two execution paths:

      - Real LLM path (MOCK_LLM=0 and ANTHROPIC_API_KEY set): drives Claude
        through an agentic tool-use loop. Claude picks which searches to
        run, when to retry with broader filters, and which 3 options to
        ship. The orchestrator validates every offer ID Claude returns
        against its own registry of recorded tool calls before constructing
        the ItineraryOption with grounded provenance.

      - Mock / no-key path: deterministic fan-out. One search per leg in
        parallel, then the diversity-aware picker. Used by the test suite
        and as a fallback when the loop's API calls fail.
    """
    started = time.time()
    # Re-resolve settings to pick up test-time monkeypatches.
    from api.config import settings as cur_settings
    from memory.profile import load_profile
    budget = RequestBudget.new(
        max_dollars=cur_settings.request_max_dollars,
        max_seconds=cur_settings.request_max_seconds,
    )
    profile = load_profile(user_id)
    trip = await parse_trip_request(
        raw_text, user_id=user_id,
        profile_summary=profile.to_prompt_summary(),
    )
    budget.charge("llm_intent")
    session = AgentSession(request_id=trip.request_id, user_id=user_id)
    log.append("agent", "request.opened", {
        "request_id": trip.request_id, "raw_text": raw_text, "user_id": user_id,
        "parsed": {
            "origin": trip.origin,
            "legs": [
                {"origin": L.origin, "destination": L.destination,
                 "date_start": str(L.date_start), "date_end": str(L.date_end),
                 "budget_min_usd": L.budget_min_usd, "budget_max_usd": L.budget_max_usd}
                for L in trip.legs
            ],
            "destination": trip.destination,
            "date_start": str(trip.date_start), "date_end": str(trip.date_end),
            "budget_usd": trip.budget_total_usd,
        },
    }, request_id=trip.request_id)
    if progress:
        if len(trip.legs) == 1:
            progress("parsed", {"destination": trip.destination, "budget": trip.budget_total_usd})
        else:
            progress("parsed", {
                "legs": [L.destination for L in trip.legs],
                "budget": trip.budget_total_usd,
            })

    # use_mock=None enables auto-detection: real providers per-tool when their
    # API keys are set, mocks otherwise. Tests opt in to all-mocks via env.
    tools = build_tool_registry(use_mock=None)
    session.transition(AgentState.SEARCHING)

    if progress: progress("searching", {})

    # Real-LLM path: drive the agentic tool-use loop. Mock-LLM and tests
    # fall through to the deterministic fan-out below.
    use_real_loop = (not cur_settings.mock_llm) and bool(cur_settings.anthropic_api_key)
    if use_real_loop:
        from agent.loop import run_agentic_loop
        try:
            options = await run_agentic_loop(
                trip=trip, log=log, budget=budget,
                profile=profile, progress=progress,
            )
        except Exception as e:
            log.append("agent", "loop.unhandled_error", {
                "error": f"{type(e).__name__}: {str(e)[:300]}",
            }, request_id=trip.request_id)
            options = []

        if not options:
            session.transition(AgentState.FAILED)
            session.elapsed_ms = int((time.time() - started) * 1000)
            return session, []

        for opt in options:
            log.store_option_snapshot(
                opt.id, trip.request_id, opt.rank, opt.model_dump(mode="json"),
            )
        session.transition(AgentState.RANKING)
        session.transition(AgentState.PRESENTING)
        log.append("agent", "options.presented", {
            "request_id": trip.request_id,
            "options": [
                {"id": o.id, "rank": o.rank,
                 "tradeoff": o.tradeoff_label,
                 "total_cents": o.total_price_cents.value}
                for o in options
            ],
        }, request_id=trip.request_id)
        session.transition(AgentState.AWAITING_SELECTION)
        session.elapsed_ms = int((time.time() - started) * 1000)
        if progress:
            progress("presented", {
                "options": [
                    {"rank": o.rank, "tradeoff": o.tradeoff_label,
                     "total_usd": o.total_price_cents.value / 100}
                    for o in options
                ],
            })
        return session, options

    try:
        for _ in trip.legs:
            budget.charge("search_flights")
            budget.charge("search_hotels")
            budget.charge("get_weather")
        # Return flight if the trip doesn't end at home
        if trip.legs[-1].destination != trip.origin:
            budget.charge("search_flights")
    except BudgetExceeded as e:
        log.append("agent", "budget.exceeded", {"reason": str(e)},
                   request_id=trip.request_id)
        session.transition(AgentState.FAILED)
        return session, []

    # Fan out per leg: flights TO this leg's destination, hotel for the stay,
    # weather at the destination. All legs run in parallel. Flight searches
    # use the primary-hub airport code so providers don't resolve metro codes
    # (LON, NYC) to obscure regional airports.
    leg_tasks: list = []
    for leg in trip.legs:
        leg_budget = leg.budget_max_usd or trip.budget_total_usd or 2000.0
        flight_origin = _resolve_airport(leg.origin)
        flight_dest = _resolve_airport(leg.destination)
        leg_tasks.append(asyncio.gather(
            _call_tool(tools["search_flights"], {
                "origin": flight_origin, "destination": flight_dest,
                "date_start": str(leg.date_start), "date_end": str(leg.date_start),
                "traveler_count": trip.traveler_count,
                "max_price_cents": int(leg_budget * 100 * 0.4),
                "one_way": True,
            }, log=log, request_id=trip.request_id),
            _call_tool(tools["search_hotels"], {
                # Hotels are keyed by metro, not airport — resolve LHR→LON etc.
                "destination": _resolve_metro(leg.destination),
                "check_in": str(leg.date_start), "check_out": str(leg.date_end),
                "traveler_count": trip.traveler_count,
                "max_nightly_cents": int(leg_budget * 100 * 0.5),
            }, log=log, request_id=trip.request_id),
            _call_tool(tools["get_weather"], {
                "location": _resolve_metro(leg.destination),
                "window_start": str(leg.date_start), "window_end": str(leg.date_end),
            }, log=log, request_id=trip.request_id),
        ))

    return_flight_task = None
    # Always search a return-home flight unless the last leg already lands at
    # home origin OR the user explicitly wanted one-way. Single-leg trips end
    # somewhere other than home by construction (you don't fly to your own
    # airport), so this fires for them too.
    if trip.legs[-1].destination != trip.origin and not trip.is_one_way:
        last_leg = trip.legs[-1]
        return_flight_task = _call_tool(tools["search_flights"], {
            "origin": _resolve_airport(last_leg.destination),
            "destination": _resolve_airport(trip.origin),
            "date_start": str(last_leg.date_end),
            "date_end": str(last_leg.date_end),
            "traveler_count": trip.traveler_count,
            "max_price_cents": int((trip.budget_total_usd or 2000) * 100 * 0.4),
            "one_way": True,
        }, log=log, request_id=trip.request_id)

    if return_flight_task is not None:
        gathered = await asyncio.gather(*leg_tasks, return_flight_task)
        per_leg_results = gathered[:-1]
        return_flight_call = gathered[-1]
    else:
        per_leg_results = await asyncio.gather(*leg_tasks)
        return_flight_call = None

    # Unpack per-leg results, weather fallback per leg
    leg_payloads: list[dict] = []
    for i, (flights_call, hotels_call, weather_call) in enumerate(per_leg_results):
        flight_offers: list[FlightOffer] = flights_call["result"] or []
        hotel_offers: list[HotelOffer] = hotels_call["result"] or []
        weather: WeatherSummary | None = weather_call["result"]

        if weather is None:
            from tools.weather.mock import MockWeatherProvider
            mock_call = await _call_tool(
                ToolHandle("get_weather", MockWeatherProvider()),
                {"location": trip.legs[i].destination,
                 "window_start": str(trip.legs[i].date_start),
                 "window_end": str(trip.legs[i].date_end)},
                log=log, request_id=trip.request_id,
            )
            weather = mock_call["result"]
            weather_call = mock_call

        if not flight_offers or not hotel_offers:
            session.transition(AgentState.FAILED)
            log.append("agent", "search.no_results", {
                "leg_index": i, "destination": trip.legs[i].destination,
                "flights": len(flight_offers), "hotels": len(hotel_offers),
            }, request_id=trip.request_id)
            return session, []

        leg_payloads.append({
            "leg_index": i,
            "flights": flight_offers,
            "hotels": hotel_offers,
            "weather": weather,
            "flights_call_id": flights_call["tool_call_id"],
            "hotels_call_id": hotels_call["tool_call_id"],
            "weather_call_id": weather_call["tool_call_id"],
        })

    session.transition(AgentState.RANKING)
    if progress: progress("ranking", {
        "leg_count": len(trip.legs),
        "flight_count": sum(len(p["flights"]) for p in leg_payloads),
        "hotel_count": sum(len(p["hotels"]) for p in leg_payloads),
    })

    options = _build_three_options(
        trip=trip,
        leg_payloads=leg_payloads,
        return_flight_call=return_flight_call,
    )

    # Persist option snapshots so the approval gate can bind option_hash
    for opt in options:
        snap = opt.model_dump(mode="json")
        log.store_option_snapshot(opt.id, trip.request_id, opt.rank, snap)

    session.transition(AgentState.PRESENTING)
    log.append("agent", "options.presented", {
        "request_id": trip.request_id,
        "options": [{"id": o.id, "rank": o.rank,
                     "tradeoff": o.tradeoff_label,
                     "total_cents": o.total_price_cents.value} for o in options],
    }, request_id=trip.request_id)

    session.transition(AgentState.AWAITING_SELECTION)
    session.elapsed_ms = int((time.time() - started) * 1000)
    if progress: progress("presented", {
        "options": [{"rank": o.rank, "tradeoff": o.tradeoff_label,
                     "total_usd": o.total_price_cents.value / 100} for o in options],
    })

    return session, options


# ----- Internals ------------------------------------------------------------

async def _call_tool(handle: ToolHandle, args: dict, *,
                     log: AuditLog, request_id: str) -> dict:
    """Invoke a tool, record it, return both the result and the tool_call_id.

    On failure, returns {"tool_call_id": id, "result": None, "error": "..."}
    rather than raising, so the agent can degrade gracefully on a partial
    set of tool results instead of failing the whole request.
    """
    started = time.time()
    impl = handle.impl
    try:
        if handle.name == "search_flights":
            result = await impl.search(**args)
            result_serializable = [r.model_dump(mode="json") for r in result]
        elif handle.name == "search_hotels":
            result = await impl.search(**args)
            result_serializable = [r.model_dump(mode="json") for r in result]
        elif handle.name == "get_weather":
            result = await impl.forecast(**args)
            result_serializable = result.model_dump(mode="json")
        else:
            raise ValueError(f"unknown tool {handle.name}")
    except Exception as e:
        latency_ms = int((time.time() - started) * 1000)
        tool_call_id = log.record_tool_call(
            tool_name=handle.name, args=args, result=None,
            request_id=request_id, latency_ms=latency_ms, status="error",
        )
        log.append("agent", "tool.failed", {
            "tool_call_id": tool_call_id, "tool": handle.name,
            "error": str(e)[:200], "provider": getattr(impl, "name", "?"),
        }, request_id=request_id)
        return {"tool_call_id": tool_call_id, "result": None,
                "error": str(e), "latency_ms": latency_ms}

    latency_ms = int((time.time() - started) * 1000)
    tool_call_id = log.record_tool_call(
        tool_name=handle.name, args=args, result=_to_jsonable(result_serializable),
        request_id=request_id, latency_ms=latency_ms, status="ok",
    )
    log.append("agent", "tool.called", {
        "tool_call_id": tool_call_id, "tool": handle.name,
        "args_hash": sha256_hex(jcs_canonical(args)), "latency_ms": latency_ms,
        "provider": getattr(impl, "name", "?"),
    }, request_id=request_id)
    return {"tool_call_id": tool_call_id, "result": result, "latency_ms": latency_ms}


def _to_jsonable(x):
    if isinstance(x, list):
        return [_to_jsonable(i) for i in x]
    if isinstance(x, dict):
        return {k: _to_jsonable(v) for k, v in x.items()}
    return x


def _build_three_options(
    *,
    trip: TripRequest,
    leg_payloads: list[dict],
    return_flight_call: dict | None,
) -> list[ItineraryOption]:
    """Construct cheapest / best-reviewed / alternative options across all legs.

    For each leg, run the existing diversity-aware picker to produce three
    (flight, hotel) bundles. The trip-level option N is the composition of
    bundle N from every leg, so:
      Option 1 (cheapest)      = leg0.cheapest      + leg1.cheapest      + ...
      Option 2 (best_reviewed) = leg0.best_reviewed + leg1.best_reviewed + ...
      Option 3 (alternative)   = leg0.alternative   + leg1.alternative   + ...

    Trip-level totals sum across legs plus an optional return flight (cheapest
    of the return search).

    Hard facts (price, dates, hotel name) are filled from tool results.
    Qualitative narrative (why_this_one, the_catch) is templated from the
    cheapest leg's data so single-leg demos read identically to before.
    """
    # Defensive: if no leg payloads were assembled (e.g. the agentic-loop
    # fallback couldn't bucket any inventory) we'd otherwise crash on
    # leg_options[0] below. Bail with [] so the caller surfaces an empty-
    # results path instead of a 500.
    if not leg_payloads:
        return []

    # Per-leg picks: leg_picks[leg_index] is a list of 3 (flight, hotel) pairs.
    leg_picks_per_leg: list[list[tuple[FlightOffer, HotelOffer]]] = []
    for payload in leg_payloads:
        leg_picks_per_leg.append(_pick_three_options(payload["flights"], payload["hotels"]))

    return_flight: FlightOffer | None = None
    return_flight_id: str | None = None
    if return_flight_call is not None and return_flight_call.get("result"):
        return_offers = sorted(
            return_flight_call["result"], key=lambda f: f.total_price_cents,
        )
        if return_offers:
            return_flight = return_offers[0]
            return_flight_id = return_flight_call["tool_call_id"]

    labels = ("cheapest", "best_reviewed", "alternative")
    options: list[ItineraryOption] = []

    for rank, label in enumerate(labels, start=1):
        opt_id = f"opt_{trip.request_id}_{rank}"
        leg_options: list[LegOption] = []
        leg_total = 0

        for leg_idx, payload in enumerate(leg_payloads):
            flight, hotel = leg_picks_per_leg[leg_idx][rank - 1]
            leg_cost = flight.total_price_cents + hotel.total_price_cents
            leg_total += leg_cost
            leg = trip.legs[leg_idx]
            leg_weather = payload["weather"]
            leg_options.append(LegOption(
                leg_index=leg_idx,
                origin=leg.origin,
                destination=leg.destination,
                flight=grounded(flight, tool_call_id=payload["flights_call_id"],
                                json_path=f"$.flights[?(@.id=='{flight.id}')]"),
                hotel=grounded(hotel, tool_call_id=payload["hotels_call_id"],
                               json_path=f"$.hotels[?(@.id=='{hotel.id}')]"),
                weather=grounded(leg_weather,
                                 tool_call_id=payload["weather_call_id"],
                                 json_path="$") if leg_weather else None,
                leg_total_cents=leg_cost,
            ))

        return_grounded = None
        if return_flight is not None and return_flight_id is not None:
            return_grounded = grounded(
                return_flight, tool_call_id=return_flight_id,
                json_path=f"$.flights[?(@.id=='{return_flight.id}')]",
            )
            leg_total += return_flight.total_price_cents

        # Narrative is anchored on the first leg for now. Multi-leg trips
        # also surface a leg summary in the_catch via _multi_leg_catch().
        first_leg_picks = leg_picks_per_leg[0]
        first_leg_weather = leg_payloads[0]["weather"]
        why, catch = _narrative_for(
            label, first_leg_picks, rank - 1, first_leg_weather, trip,
        )
        if len(trip.legs) > 1:
            catch = _multi_leg_catch(
                trip=trip, leg_options=leg_options, leg_total=leg_total,
                return_flight=return_flight,
            ) + catch
            catch = catch[:3]

        # Legacy flat fields mirror leg 0 so single-leg consumers still work.
        leg0_opt = leg_options[0]
        currency = leg0_opt.flight.value.currency

        options.append(ItineraryOption(
            id=opt_id,
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
            total_price_cents=grounded(
                leg_total,
                tool_call_id=leg_payloads[0]["flights_call_id"],
                json_path="sum($.legs[].leg_total_cents) + $.return_flight.total_price_cents",
            ),
            currency=grounded(
                currency,
                tool_call_id=leg_payloads[0]["flights_call_id"],
                json_path="$.legs[0].flight.currency",
            ),
        ))
    return options


def _multi_leg_catch(*, trip, leg_options, leg_total, return_flight):
    """Generate up to two extra catch bullets that summarize the multi-leg
    structure (e.g. 'London 5 nights then New York 4 nights, $14,820 total')
    and surface a budget-floor mismatch when the total falls below what the
    user said they wanted to spend.
    """
    parts = []
    leg_summary = " then ".join(
        f"{lo.destination} {trip.legs[lo.leg_index].date_end.toordinal() - trip.legs[lo.leg_index].date_start.toordinal()} nights"
        for lo in leg_options
    )
    parts.append(f"{leg_summary}, {_fmt_dollars(leg_total)} all in.")

    # Budget-floor surfacing: if the user said "at least $X" per leg or for
    # the whole trip and the picked combo undershoots, tell them so.
    floor_cents = _budget_floor_cents(trip)
    if floor_cents and leg_total < floor_cents:
        gap = floor_cents - leg_total
        parts.append(
            f"You asked for at least {_fmt_dollars(floor_cents)} of spend; "
            f"the cheapest real-data combo we found is {_fmt_dollars(gap)} below that."
        )

    if return_flight is not None:
        parts.append(
            f"Return on {return_flight.outbound[0].carrier}"
            f"{return_flight.outbound[0].flight_number} "
            f"{return_flight.outbound[0].origin} to {return_flight.outbound[0].destination}."
        )
    return parts


def _budget_floor_cents(trip: TripRequest) -> int:
    """Sum per-leg budget_min_usd if any are set. Returns 0 when no floor."""
    floors = [L.budget_min_usd for L in trip.legs if L.budget_min_usd]
    if not floors:
        return 0
    return int(sum(floors) * 100)


def _pick_three_options(
    flights: list[FlightOffer],
    hotels: list[HotelOffer],
) -> list[tuple[FlightOffer, HotelOffer]]:
    """Pick three (flight, hotel) bundles with pairwise-distinct identity.

    Ensures the three options aren't accidentally identical when the cheapest
    hotel is also the highest-rated, or the only refundable flight is the same
    as the cheapest. Each bundle still emphasizes a distinct tradeoff axis:
        1. cheapest         — lowest combined price
        2. best_reviewed    — highest-rated hotel that's distinct from #1
        3. alternative      — refundable flight + a hotel from a different
                              neighborhood when possible

    Diversity rule: the (flight_id, hotel_id) tuples are pairwise distinct
    whenever the candidate sets allow. If there aren't enough candidates
    (e.g., one flight + one hotel), we fall back to repeating — no crash.
    """
    flights_by_price = sorted(flights, key=lambda f: f.total_price_cents)
    hotels_by_price = sorted(hotels, key=lambda h: h.nightly_rate_cents)
    hotels_by_rating = sorted(
        hotels, key=lambda h: (-h.star_rating, h.nightly_rate_cents),
    )
    refundable_flights = [
        f for f in flights if any(s.refundable for s in f.outbound)
    ]

    used: set[tuple[str, str]] = set()
    picks: list[tuple[FlightOffer, HotelOffer]] = []

    def find_pair(
        prefer_flight: FlightOffer,
        prefer_hotel: HotelOffer,
        flight_pool: list[FlightOffer],
        hotel_pool: list[HotelOffer],
    ) -> tuple[FlightOffer, HotelOffer]:
        # 1. preferred pair
        if (prefer_flight.id, prefer_hotel.id) not in used:
            return prefer_flight, prefer_hotel
        # 2. hold flight, swap hotel
        for h in hotel_pool:
            if (prefer_flight.id, h.id) not in used:
                return prefer_flight, h
        # 3. hold hotel, swap flight
        for f in flight_pool:
            if (f.id, prefer_hotel.id) not in used:
                return f, prefer_hotel
        # 4. any unused tuple
        for f in flight_pool:
            for h in hotel_pool:
                if (f.id, h.id) not in used:
                    return f, h
        # 5. degenerate — fewer distinct tuples than needed; repeat
        return prefer_flight, prefer_hotel

    # Option 1: cheapest combo
    f, h = flights_by_price[0], hotels_by_price[0]
    used.add((f.id, h.id))
    picks.append((f, h))

    # Option 2: top-rated hotel + cheapest flight (with diversity fallback)
    f, h = find_pair(
        prefer_flight=flights_by_price[0],
        prefer_hotel=hotels_by_rating[0],
        flight_pool=flights_by_price,
        hotel_pool=hotels_by_rating,
    )
    used.add((f.id, h.id))
    picks.append((f, h))

    # Option 3: refundable flight if available + hotel from a new neighborhood
    seen_neighborhoods = {h.neighborhood for _, h in picks}
    alt_pref_flight = (
        refundable_flights[0] if refundable_flights
        else flights_by_price[1] if len(flights_by_price) > 1
        else flights_by_price[0]
    )
    alt_pref_hotel = next(
        (h for h in hotels_by_rating if h.neighborhood not in seen_neighborhoods),
        hotels_by_rating[0],
    )
    f, h = find_pair(
        prefer_flight=alt_pref_flight,
        prefer_hotel=alt_pref_hotel,
        flight_pool=flights_by_price,
        hotel_pool=hotels_by_rating,
    )
    used.add((f.id, h.id))
    picks.append((f, h))

    return picks


def _compare_options(picks: list[tuple]) -> list[dict]:
    """For each picked (flight, hotel), compute deltas against the other picks.

    Returns one dict per option with keys:
      total_cents              : flight + hotel
      price_delta_vs_cheapest  : 0 for the cheapest option, positive for others
      price_delta_vs_others    : list of deltas to each other option (same order as picks)
      stars                    : hotel star rating
      stars_delta_vs_best      : 0 for the highest-rated, negative for others
      refundable               : bool (any outbound segment refundable)
      duration_minutes         : sum of outbound segment minutes
      duration_delta_vs_short  : positive if longer than the shortest option
      neighborhood             : hotel neighborhood
      unique_neighborhood      : True if this is the only option in this neighborhood
    """
    totals = [f.total_price_cents + h.total_price_cents for f, h in picks]
    cheapest_total = min(totals)
    stars = [h.star_rating for _, h in picks]
    best_stars = max(stars)
    durations = [sum(s.duration_minutes for s in f.outbound) for f, _ in picks]
    shortest_duration = min(durations)
    neighborhoods = [h.neighborhood for _, h in picks]

    out: list[dict] = []
    for idx, (f, h) in enumerate(picks):
        out.append({
            "total_cents": totals[idx],
            "price_delta_vs_cheapest": totals[idx] - cheapest_total,
            "price_delta_vs_others": [totals[idx] - t for t in totals],
            "stars": stars[idx],
            "stars_delta_vs_best": stars[idx] - best_stars,
            "refundable": any(s.refundable for s in f.outbound),
            "duration_minutes": durations[idx],
            "duration_delta_vs_short": durations[idx] - shortest_duration,
            "neighborhood": neighborhoods[idx],
            "unique_neighborhood": neighborhoods.count(neighborhoods[idx]) == 1,
        })
    return out


def _fmt_dollars(cents: int) -> str:
    return f"${abs(cents) / 100:,.0f}"


def _fmt_duration(mins: int) -> str:
    h, m = divmod(mins, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _narrative_for(label, picks, idx, weather, trip):
    """Deterministic comparison-aware narrative for MOCK_LLM mode.

    Each option's narrative references concrete deltas vs the OTHER options
    (price, stars, refundability, duration, neighborhood). In live mode the
    LLM emits these via `synthesis`, but the orchestrator still strips any
    concrete facts and keeps only the qualitative bits.
    """
    deltas = _compare_options(picks)
    me = deltas[idx]
    flight, hotel = picks[idx]
    others = [(i, picks[i], deltas[i]) for i in range(len(picks)) if i != idx]

    def _ref_to(other_idx: int) -> str:
        return f"Option {other_idx + 1}"

    if label == "cheapest":
        # Compare to the most expensive of the other two so the narrative is concrete.
        most_exp_idx, _, most_exp = max(others, key=lambda t: t[2]["total_cents"])
        savings = most_exp["total_cents"] - me["total_cents"]
        if savings > 0:
            why = (
                f"{_fmt_dollars(savings)} cheaper than {_ref_to(most_exp_idx)}: "
                f"{flight.outbound[0].carrier}{flight.outbound[0].flight_number} flights "
                f"and {hotel.name} ({hotel.neighborhood}) at "
                f"${hotel.nightly_rate_cents/100:.0f}/night."
            )
        else:
            # Prices tied — emphasize a non-price axis
            why = (
                f"Cheapest tier (tied on total): {hotel.name} in {hotel.neighborhood} "
                f"at ${hotel.nightly_rate_cents/100:.0f}/night."
            )
        catch: list[str] = []
        if me["stars_delta_vs_best"] < 0:
            catch.append(
                f"{me['stars']:.1f}★ vs {_ref_to(_argmax_idx_excl([d['stars'] for d in deltas], -1))}'s "
                f"{max(d['stars'] for d in deltas):.1f}★ pick."
            )
        if not me["refundable"]:
            ref_other = next((i for i, _, d in others if d["refundable"]), None)
            if ref_other is not None:
                catch.append(f"Non-refundable (vs refundable in {_ref_to(ref_other)}).")
            else:
                catch.append("Non-refundable across all three options at this price.")
        if me["duration_delta_vs_short"] > 0:
            catch.append(
                f"+{_fmt_duration(me['duration_delta_vs_short'])} flight time vs the shortest option."
            )
        if not catch:
            catch.append(
                f"Hotel is in {hotel.neighborhood} — a real neighborhood, "
                f"but a tram ride from old town." if "Belém" in hotel.neighborhood
                else f"Standard 1-stop return; check baggage policy."
            )
        # Always include something about hotel signals if available
        hidden = hotel.review_signals.get("hidden_fees")
        if hidden and hidden != "none mentioned":
            catch.append(f"Reviewers flag: {hidden}.")
        return why, catch[:3] or ["No notable trade-offs flagged."]

    if label == "best_reviewed":
        # Compare to the cheapest option
        cheap_idx, _, cheap = min(others, key=lambda t: t[2]["total_cents"])
        markup = me["total_cents"] - cheap["total_cents"]
        cheap_hotel = picks[cheap_idx][1]
        if markup > 0:
            why = (
                f"{_fmt_dollars(markup)} more than {_ref_to(cheap_idx)} for "
                f"{hotel.name} ({me['stars']:.1f}★) in {hotel.neighborhood} — "
                f"vs {cheap_hotel.name} ({cheap_hotel.star_rating:.1f}★) in {cheap_hotel.neighborhood}."
            )
        else:
            why = (
                f"{hotel.name} ({me['stars']:.1f}★) in {hotel.neighborhood}, same total as "
                f"{_ref_to(cheap_idx)} but a higher-rated room."
            )
        catch = []
        if markup > 0:
            catch.append(f"{_fmt_dollars(markup)} more than the cheapest pick.")
        if not me["refundable"]:
            catch.append("Non-refundable at this fare.")
        # Quote a real review signal if the LiteAPI/mock surfaced one
        review_summary = hotel.review_signals.get("summary")
        if review_summary:
            catch.append(f"Reviewers note: {review_summary}.")
        else:
            catch.append(f"{me['stars']:.1f}★ rating; book early — top-rated hotels sell out.")
        return why, catch[:3]

    # alternative
    # Pick the most distinguishing axis
    cheap_idx, _, cheap = min(others, key=lambda t: t[2]["total_cents"])
    markup = me["total_cents"] - cheap["total_cents"]
    if me["refundable"] and not all(d["refundable"] for d in deltas):
        why = (
            f"Only refundable option ({flight.outbound[0].carrier}{flight.outbound[0].flight_number}). "
            f"{_fmt_dollars(markup)} more than {_ref_to(cheap_idx)} — "
            f"buy the flexibility if your dates may shift."
        )
    elif me["unique_neighborhood"]:
        why = (
            f"Different neighborhood: {hotel.name} in {me['neighborhood']} "
            f"({_fmt_dollars(markup)} more than {_ref_to(cheap_idx)}'s {cheap['neighborhood']} pick)."
        )
    elif me["duration_delta_vs_short"] > 0:
        why = (
            f"Longer connection but {flight.outbound[0].carrier} carrier: "
            f"+{_fmt_duration(me['duration_delta_vs_short'])} vs the shortest option, "
            f"{_fmt_dollars(markup)} more than {_ref_to(cheap_idx)}."
        )
    else:
        why = (
            f"Alternative axis: {hotel.name} ({me['stars']:.1f}★) in {hotel.neighborhood}, "
            f"{_fmt_dollars(markup)} more than {_ref_to(cheap_idx)}."
        )

    catch = []
    if me["refundable"] and markup > 0:
        catch.append(f"Refundable flights add {_fmt_dollars(markup)} vs {_ref_to(cheap_idx)}.")
    if len(flight.outbound) > 1:
        connect_via = flight.outbound[0].destination
        connect_h = sum(s.duration_minutes for s in flight.outbound) // 60
        catch.append(f"Connecting via {connect_via} adds {connect_h}h+ vs nonstop.")
    if weather:
        catch.append(
            f"Weather window: {weather.summary} (avg high {weather.avg_high_c:.0f}°C)."
        )
    return why, catch[:3] or ["No notable trade-offs flagged."]


def _argmax_idx_excl(values: list[float], excluded_idx: int) -> int:
    """Index of the max value, ignoring `excluded_idx`. Returns 0 if all excluded."""
    best_idx = 0
    best_val = float("-inf")
    for i, v in enumerate(values):
        if i == excluded_idx:
            continue
        if v > best_val:
            best_val = v
            best_idx = i
    return best_idx


def system_prompt() -> str:
    return (PROMPT_DIR / "system.md").read_text(encoding="utf-8")
