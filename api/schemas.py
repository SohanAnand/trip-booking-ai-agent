"""Pydantic models shared by API, agent, and tests.

Provenance is structural: every fact in an ItineraryOption carries a pointer to
the ToolCall row that produced it. The orchestrator wires provenance — the LLM
never claims it.
"""

from __future__ import annotations

from datetime import date
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class TripLeg(BaseModel):
    """One leg of a trip. A single-destination round-trip is one leg with
    origin = home airport and destination = the city being visited."""
    origin: str               # IATA, e.g. "JFK" for leg 0, prior leg's destination otherwise
    destination: str          # IATA city, e.g. "LON"
    date_start: date
    date_end: date
    budget_min_usd: float | None = None
    budget_max_usd: float | None = None


class TripRequest(BaseModel):
    request_id: str
    user_id: str
    raw_text: str
    legs: list[TripLeg] = Field(default_factory=list)
    # Legacy flat fields. Mirror legs[0] when there is exactly one leg, so
    # downstream code that hasn't been migrated yet keeps working.
    origin: str | None = None       # IATA, e.g. "JFK"
    destination: str | None = None  # city or IATA
    date_start: date | None = None
    date_end: date | None = None
    date_flexibility_days: int = 0
    traveler_count: int = 1
    budget_total_usd: float | None = None
    # When True the orchestrator skips the home-bound return-flight search
    # and consent text. Set by intent extraction from "one-way", "moving to",
    # "relocating to", "no return" phrasing.
    is_one_way: bool = False
    hard_constraints: dict = Field(default_factory=dict)
    soft_preferences: dict = Field(default_factory=dict)


class Provenance(BaseModel):
    tool_call_id: str
    json_path: str
    fetched_at: str


class Grounded(BaseModel, Generic[T]):
    value: T
    prov: Provenance

    model_config = ConfigDict(arbitrary_types_allowed=True)


class FlightSegment(BaseModel):
    carrier: str
    flight_number: str
    origin: str
    destination: str
    depart: str   # ISO datetime
    arrive: str
    duration_minutes: int
    fare_class: str
    refundable: bool


class FlightOffer(BaseModel):
    id: str
    provider: str
    outbound: list[FlightSegment]
    inbound: list[FlightSegment]
    total_price_cents: int
    currency: str
    baggage_included: bool


class HotelOffer(BaseModel):
    id: str
    provider: str
    name: str
    neighborhood: str
    check_in: str
    check_out: str
    nights: int
    nightly_rate_cents: int
    total_price_cents: int
    currency: str
    star_rating: float
    refundable_until: str | None
    review_signals: dict = Field(default_factory=dict)
    public_review_url: str | None = None


class WeatherSummary(BaseModel):
    location: str
    window_start: str
    window_end: str
    summary: str
    avg_high_c: float
    avg_low_c: float
    rain_probability: float


class LegOption(BaseModel):
    """One leg of an ItineraryOption. For a multi-city trip there is one
    LegOption per destination, each carrying the flight TO that destination
    and the hotel for the stay."""
    leg_index: int
    origin: str
    destination: str
    flight: Grounded[FlightOffer]
    hotel: Grounded[HotelOffer]
    weather: Grounded[WeatherSummary] | None = None
    leg_total_cents: int


class ItineraryOption(BaseModel):
    """One of the 3 presented options. Every fact is grounded.

    Multi-leg payload lives in `legs[]`. The `flight`/`hotel`/`weather` flat
    fields mirror legs[0] for backward compatibility with single-destination
    consumers (CLI, older tests) that haven't been migrated yet.
    """
    id: str
    request_id: str
    rank: int                              # 1, 2, 3
    tradeoff_label: str                    # short slug picked per request:
                                           # "luxury", "value", "balanced",
                                           # "refundable", "fastest", etc.
    why_this_one: str
    the_catch: list[str]                   # 3 bullets
    legs: list[LegOption] = Field(default_factory=list)
    return_flight: Grounded[FlightOffer] | None = None
    flight: Grounded[FlightOffer]
    hotel: Grounded[HotelOffer]
    weather: Grounded[WeatherSummary] | None = None
    total_price_cents: Grounded[int]
    currency: Grounded[str]


class FinalSummary(BaseModel):
    """What the user sees at the approval gate after revalidation."""
    request_id: str
    option: ItineraryOption
    consent_text: str             # exact text the user clicks OK to
    drift_detected: bool
    drift_diffs: list[str] = Field(default_factory=list)
    cancellation_policy: str
    total_price_display: str      # "$1,847.32 USD"
    payment_method_id: str


class BookingHold(BaseModel):
    booking_id: str
    state: str
    legs: list[dict]                      # one entry per component held
    expires_at: str | None = None


class BookingResult(BaseModel):
    booking_id: str
    state: str   # COMMITTED / COMPENSATED / FAILED
    confirmations: dict[str, str]        # leg_id → provider confirmation number
    total_charged_cents: int | None = None
    error: str | None = None


class ToolRequest(BaseModel):
    """Schema-validated structured output from the LLM."""
    id: str
    tool: str
    args: dict


class LLMSynthesis(BaseModel):
    """LLM's qualitative synthesis. Hard facts are stripped; orchestrator fills them in."""
    option_id: str
    why_this_one: str
    the_catch: list[str]


class LLMOutput(BaseModel):
    """One of: tool_request | synthesis | clarify | replan."""
    kind: Literal["tool_request", "synthesis", "clarify", "replan"]
    calls: list[ToolRequest] = Field(default_factory=list)
    synthesis: LLMSynthesis | None = None
    question: str | None = None
    replan_reason: str | None = None
    replan_relax: dict | None = None
