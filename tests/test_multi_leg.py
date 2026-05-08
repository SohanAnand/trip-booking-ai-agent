"""Multi-leg orchestrator behavior.

Locks in the contract that when the intent parser returns >1 leg, the agent
fans out searches per leg and the resulting ItineraryOptions carry a populated
`legs[]` array with one LegOption per leg.
"""

from __future__ import annotations

from datetime import date

import pytest

from agent.orchestrator import parse_trip_request, run_agent
from api.schemas import TripRequest


# ---- parse_trip_request: single leg --------------------------------------

@pytest.mark.asyncio
async def test_parse_single_leg_populates_legs_array():
    trip = await parse_trip_request("4 days in Lisbon next month under $2,000",
                                    user_id="u1")
    assert isinstance(trip, TripRequest)
    assert len(trip.legs) == 1
    leg0 = trip.legs[0]
    assert leg0.origin == "JFK"
    assert leg0.destination == "LIS"
    # Flat fields mirror leg 0 for backward compat
    assert trip.destination == "LIS"
    assert trip.origin == "JFK"
    assert trip.date_start == leg0.date_start
    assert trip.date_end == leg0.date_end


# ---- parse_trip_request: multi-leg (regex path can't, so fake the LLM) ---

@pytest.mark.asyncio
async def test_parse_multi_leg_chains_origins(monkeypatch):
    """When intent.legs has 2 entries, leg 1's origin should equal leg 0's
    destination so the orchestrator can search the right flight segment."""
    from agent.intent import IntentSchema, LegIntent

    fake_intent = IntentSchema(
        origin="JFK",
        legs=[
            LegIntent(destination="LON", date_start="2026-07-05",
                      date_end="2026-07-10", budget_min_usd=10000.0),
            LegIntent(destination="NYC", date_start="2026-07-10",
                      date_end="2026-07-15", budget_min_usd=5000.0),
        ],
    )

    async def fake_extract(_text, today=None, **_kwargs):
        return fake_intent
    monkeypatch.setattr("agent.intent.extract_intent", fake_extract)

    trip = await parse_trip_request(
        "spend at least $10K in London then $5K in NYC starting July 5",
        user_id="u1",
    )
    assert len(trip.legs) == 2
    assert trip.legs[0].origin == "JFK"
    assert trip.legs[0].destination == "LON"
    assert trip.legs[1].origin == "LON"   # chained from leg 0's destination
    assert trip.legs[1].destination == "NYC"
    # Flat destination is None when there are multiple legs
    assert trip.destination is None
    # Flat date_start is leg 0; flat date_end is the LAST leg's end.
    assert trip.date_start == date(2026, 7, 5)
    assert trip.date_end == date(2026, 7, 15)


# ---- run_agent: multi-leg produces multi-leg options ---------------------

@pytest.mark.asyncio
async def test_run_agent_multi_leg_produces_legs_in_options(monkeypatch, log):
    """End-to-end with mock providers: a 2-leg trip yields 3 ItineraryOptions
    each with 2 LegOptions, plus a return flight back to origin."""
    from agent.intent import IntentSchema, LegIntent

    # Use LAX as home so neither leg destination resolves to the home airport.
    fake_intent = IntentSchema(
        origin="LAX",
        legs=[
            LegIntent(destination="LON", date_start="2026-07-05",
                      date_end="2026-07-10", budget_min_usd=10000.0),
            LegIntent(destination="NYC", date_start="2026-07-10",
                      date_end="2026-07-15", budget_min_usd=5000.0),
        ],
    )

    async def fake_extract(_text, today=None, **_kwargs):
        return fake_intent
    monkeypatch.setattr("agent.intent.extract_intent", fake_extract)

    session, options = await run_agent(
        raw_text="multi city LON NYC", user_id="u1", log=log,
    )

    assert len(options) == 3, "should always return 3 options"
    for opt in options:
        assert len(opt.legs) == 2, f"option {opt.rank} missing legs"
        # Each LegOption preserves the trip-level metro/IATA destination
        assert opt.legs[0].destination == "LON"
        assert opt.legs[1].destination == "NYC"
        # Leg 1's TripLeg origin is chained from leg 0's destination
        assert opt.legs[1].origin == "LON"
        # Per-leg flights MUST be one-way. The return-home flight is a separate
        # `return_flight` field; if a per-leg flight has any inbound segments,
        # the agent has regressed back to round-trip-per-leg behavior.
        for L in opt.legs:
            assert L.flight.value.inbound == [], (
                f"option {opt.rank} leg {L.leg_index} regressed to round-trip"
            )
        # Total price aggregates all legs (and the return if present)
        leg_sum = sum(L.leg_total_cents for L in opt.legs)
        if opt.return_flight is not None:
            leg_sum += opt.return_flight.value.total_price_cents
        assert opt.total_price_cents.value == leg_sum

    # Return flight should exist (last leg dest != home) and end at LAX.
    # Mock provider echoes whatever airport code it was searched with, so the
    # return search uses the resolved primary hubs (NYC -> JFK, LAX stays LAX).
    for opt in options:
        assert opt.return_flight is not None
        rf = opt.return_flight.value
        assert rf.outbound[-1].destination == "LAX"
        # Origin is resolved from "NYC" to a NYC-area airport.
        assert rf.outbound[0].origin in {"JFK", "LGA", "EWR", "NYC"}


# ---- run_agent: single-leg unchanged -------------------------------------

@pytest.mark.asyncio
async def test_run_agent_single_leg_still_populates_flat_fields(log):
    """Regression: the legacy single-destination path keeps the legacy
    flight/hotel/weather flat fields populated alongside legs[0] so
    older consumers don't break."""
    session, options = await run_agent(
        raw_text="4 days in Lisbon next month under $2,000",
        user_id="u1", log=log,
    )

    assert len(options) == 3
    for opt in options:
        assert len(opt.legs) == 1
        # Flat fields mirror legs[0]
        assert opt.flight.value.id == opt.legs[0].flight.value.id
        assert opt.hotel.value.id == opt.legs[0].hotel.value.id
        # No return flight when last leg destination equals home (LIS != JFK
        # actually, so there IS a return flight). Just check totals are valid.
        assert opt.total_price_cents.value > 0
