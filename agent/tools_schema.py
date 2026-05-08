"""Anthropic native tool-use schemas for the agentic loop.

The orchestrator hands these tool definitions to Claude. Claude picks which
tool to call, when, and with what arguments. Every concrete fact in the
final options must trace to a tool result Claude received: the orchestrator
validates the offer IDs Claude returns against its own registry of recorded
tool calls before constructing the ItineraryOption.

Three search tools (flights, hotels, weather) plus one terminal tool
(present_options) which Claude calls when satisfied. Replanning happens by
re-calling the search tools with broader parameters; no separate replan
tools, since the LLM can already do that with the same primitives.
"""

from __future__ import annotations

from typing import Any


SEARCH_FLIGHTS: dict[str, Any] = {
    "name": "search_flights",
    "description": (
        "Searches a single ONE-WAY flight. NEVER returns a round-trip in a "
        "single call — round-trips are assembled by calling this tool twice: "
        "once for the outbound (leg_index=0), then again for the return with "
        "origin/destination flipped (leg_index=-1).\n\n"
        "Returns up to 8 FlightOffer summaries (id, price, segments, "
        "refundability). Call once per leg of the trip, plus once with "
        "leg_index=-1 for the return-home flight whenever the last leg's "
        "destination is not the user's home origin (which is virtually "
        "always — single-leg trips need a return too).\n\n"
        "If results are sparse, re-call with a broader depart window "
        "(date_start..date_end spans more days) or a higher max_price_cents "
        "(raise by 50%). For multi-leg trips, leg N+1 originates at leg N's "
        "destination.\n\n"
        "Set max_price_cents GENEROUSLY when the user wants premium: 2-3x "
        "the leg's budget_min_usd, or 1.5x budget_max_usd. The provider "
        "returns a mix across price tiers; a tight ceiling hides the premium "
        "options.\n\n"
        "Use cabin_class='business' or 'first' when the user signals 'best "
        "experience', 'luxury', 'premium', or sets a high per-leg minimum "
        "spend (roughly $5K+ per leg suggests premium cabin)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "leg_index": {
                "type": "integer",
                "description": "Which leg of the trip this search is for (0-indexed). "
                               "Use -1 for the return-home flight after the last leg.",
            },
            "origin": {"type": "string", "description": "IATA airport code (e.g. JFK, LHR)."},
            "destination": {"type": "string", "description": "IATA airport code."},
            "date_start": {
                "type": "string",
                "description": "Earliest acceptable depart date YYYY-MM-DD. This is the "
                               "depart date for the one-way search; date_end widens it "
                               "into a flex window.",
            },
            "date_end": {
                "type": "string",
                "description": "Latest acceptable depart date YYYY-MM-DD. Set equal to "
                               "date_start for an exact-day search. NOT a return date — "
                               "this tool only does one-way.",
            },
            "max_price_cents": {
                "type": "integer",
                "description": "Reject offers above this price. Be generous when the user "
                               "wants premium so business/first offers surface.",
            },
            "traveler_count": {"type": "integer", "default": 1},
            "cabin_class": {
                "type": "string",
                "enum": ["economy", "premium_economy", "business", "first"],
                "default": "economy",
                "description": "Cabin class. Use business or first for luxury requests.",
            },
        },
        "required": ["leg_index", "origin", "destination",
                     "date_start", "date_end", "max_price_cents"],
    },
}

SEARCH_HOTELS: dict[str, Any] = {
    "name": "search_hotels",
    "description": (
        "Search hotels for a single leg's stay. Returns up to 8 HotelOffer "
        "summaries (id, name, neighborhood, star_rating, nightly_rate, "
        "total_price, refundable_until). Re-call with higher max_nightly_cents "
        "if results are sparse, or with a different destination if the leg's "
        "city has multiple metro codes.\n\n"
        "When the user signals luxury or 'best experience', set min_star_rating "
        "to 4 or 5 AND raise max_nightly_cents accordingly so 5-star inventory "
        "actually surfaces (their nightly rates are typically $400-$1500/night)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "leg_index": {"type": "integer"},
            "destination": {"type": "string", "description": "City/IATA code (e.g. LIS, LON)."},
            "check_in": {"type": "string", "description": "ISO date YYYY-MM-DD."},
            "check_out": {"type": "string", "description": "ISO date YYYY-MM-DD."},
            "max_nightly_cents": {
                "type": "integer",
                "description": "Reject offers with nightly rate above this.",
            },
            "traveler_count": {"type": "integer", "default": 1},
            "min_star_rating": {
                "type": "number",
                "minimum": 1,
                "maximum": 5,
                "description": "Reject offers below this star rating. Use 4-5 for luxury.",
            },
            "min_nightly_cents": {
                "type": "integer",
                "description": "Reject offers below this nightly rate. Use to push toward "
                               "the upper tier when the user asked to spend big.",
            },
        },
        "required": ["leg_index", "destination", "check_in", "check_out", "max_nightly_cents"],
    },
}

GET_WEATHER: dict[str, Any] = {
    "name": "get_weather",
    "description": (
        "Fetch a weather summary for the leg's stay window. One call per leg. "
        "Returns avg high/low, summary text, rain probability."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "leg_index": {"type": "integer"},
            "location": {"type": "string"},
            "window_start": {"type": "string"},
            "window_end": {"type": "string"},
        },
        "required": ["leg_index", "location", "window_start", "window_end"],
    },
}

PRESENT_OPTIONS: dict[str, Any] = {
    "name": "present_options",
    "description": (
        "TERMINAL TOOL. Call this exactly once when you have enough search "
        "results to ship 3 distinct options to the user. Each option is a "
        "set of (flight_id, hotel_id) picks per leg, optionally with a "
        "return_flight_id. The orchestrator validates every ID against the "
        "search results you've seen — invented IDs are rejected.\n\n"
        "RANKING (pick rank by user intent, not by a fixed formula):\n"
        "  rank 1 = the option that BEST MATCHES what the user actually asked for.\n"
        "  rank 2, 3 = strong alternatives that differ on at least one axis.\n"
        "  Examples:\n"
        "    user wants 'cheapest possible' → rank 1 = lowest total\n"
        "    user wants 'best experience'   → rank 1 = most premium / highest rated\n"
        "    user gave a budget floor       → rank 1 = the option that lands\n"
        "                                     closest to the floor with the\n"
        "                                     highest quality\n"
        "    user gave nothing decisive     → rank 1 = balanced (good value AND\n"
        "                                     decent quality)\n\n"
        "LABELS: pick a short EVOCATIVE phrase per option that names what "
        "MAKES THIS PICK STAND OUT for this specific user. 1-3 words, "
        "human-readable (use spaces, not slugs). Capitalization is fine. "
        "Examples to draw from:\n"
        "  Premium / luxury angle:\n"
        "    'Most luxurious', 'Best experience', 'Top tier', 'Five-star pick',\n"
        "    'Business class', 'White-glove', 'Splurge worthy'\n"
        "  Location / setting:\n"
        "    'Best view', 'Most central', 'Quietest', 'Beachfront',\n"
        "    'Old town', 'Skyline view', 'Walk everywhere'\n"
        "  Value / balance:\n"
        "    'Best of all worlds', 'Best value', 'Smart balance',\n"
        "    'Closest to target', 'Sweet spot'\n"
        "  Practical:\n"
        "    'Fully refundable', 'Fastest route', 'Direct flights',\n"
        "    'Late checkout', 'Boutique stay'\n\n"
        "Make the label specific to WHY this option exists in the lineup. "
        "If option 1 has a hotel with rooftop views, 'Best view' is better "
        "than 'Premium pick'. If option 2 is the only refundable one, "
        "'Fully refundable' is better than 'Alternative'. Each option's "
        "label MUST be different. Avoid generic stale labels like 'Cheapest' "
        "/ 'Best reviewed' / 'Alternative' unless they actually describe "
        "what stands out about THIS pick for THIS request.\n\n"
        "BUDGET FLOOR ENFORCEMENT: when the user set budget_min_usd, ALL three "
        "options must total at or above the floor. If even your most premium "
        "combination falls short, ship the three highest-totalling combinations "
        "you can build and surface the gap loudly in why_this_one and the_catch "
        "(e.g., 'best inventory we could find tops out at $7,840 — $2,160 "
        "below your $10,000 target')."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "options": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "rank": {"type": "integer", "enum": [1, 2, 3]},
                        "label": {
                            "type": "string",
                            "description": "Short slug naming the distinguishing "
                                           "axis. See description for examples.",
                        },
                        "why_this_one": {
                            "type": "string",
                            "description": "1-2 sentences explaining the tradeoff vs the other "
                                           "options. Reference concrete numeric deltas (e.g. "
                                           "'$340 cheaper than Option 2').",
                        },
                        "the_catch": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 3,
                            "description": "Up to 3 short bullets noting downsides "
                                           "(non-refundable, longer flight, etc.).",
                        },
                        "picks_per_leg": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "leg_index": {"type": "integer"},
                                    "flight_id": {"type": "string"},
                                    "hotel_id": {"type": "string"},
                                },
                                "required": ["leg_index", "flight_id", "hotel_id"],
                            },
                        },
                        "return_flight_id": {
                            "type": ["string", "null"],
                            "description": "Required if the trip's last leg destination is not "
                                           "the user's home origin. Otherwise null.",
                        },
                    },
                    "required": ["rank", "label", "why_this_one",
                                 "the_catch", "picks_per_leg"],
                },
            },
        },
        "required": ["options"],
    },
}


ALL_TOOLS: list[dict[str, Any]] = [
    SEARCH_FLIGHTS,
    SEARCH_HOTELS,
    GET_WEATHER,
    PRESENT_OPTIONS,
]
