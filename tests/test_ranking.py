"""Three-option diversity property.

The picker must produce three (flight, hotel) bundles whose identity tuples
are pairwise distinct whenever the candidate sets allow. This prevents the
"option 1 and option 2 are literally the same itinerary" bug that surfaced
when LiteAPI's cheapest hotel was also its top-rated.
"""

from __future__ import annotations

from agent.orchestrator import _pick_three_options
from api.schemas import FlightOffer, FlightSegment, HotelOffer


def _flight(id_: str, price_cents: int = 50000, refundable: bool = False) -> FlightOffer:
    seg = FlightSegment(
        carrier="XX", flight_number="1",
        origin="JFK", destination="LIS",
        depart="2026-06-06T12:00:00", arrive="2026-06-06T22:00:00",
        duration_minutes=600, fare_class="Y", refundable=refundable,
    )
    return FlightOffer(
        id=id_, provider="t", outbound=[seg], inbound=[seg],
        total_price_cents=price_cents, currency="USD", baggage_included=True,
    )


def _hotel(id_: str, *, nightly: int = 15000, stars: float = 4.0,
           neighborhood: str = "Centro") -> HotelOffer:
    return HotelOffer(
        id=id_, provider="t", name=f"Hotel {id_}", neighborhood=neighborhood,
        check_in="2026-06-06", check_out="2026-06-10", nights=4,
        nightly_rate_cents=nightly, total_price_cents=nightly * 4,
        currency="USD", star_rating=stars, refundable_until=None,
        review_signals={}, public_review_url=None,
    )


def test_three_distinct_when_cheapest_hotel_is_also_top_rated():
    """Regression for the LiteAPI sandbox bug: cheapest hotel ranked highest
    too, so options 1 and 2 used to produce the same bundle."""
    flights = [_flight("F1", 40000)]
    hotels = [
        _hotel("H1", nightly=12000, stars=4.5, neighborhood="Centro"),
        _hotel("H2", nightly=15000, stars=4.0, neighborhood="Alfama"),
        _hotel("H3", nightly=18000, stars=3.5, neighborhood="Belém"),
    ]
    picks = _pick_three_options(flights, hotels)
    assert len(picks) == 3
    keys = {(f.id, h.id) for f, h in picks}
    assert len(keys) == 3, f"options collide: {keys}"


def test_diversity_with_only_two_distinct_hotels():
    """One flight + 2 hotels → 2 distinct tuples; the 3rd is a repeat. No crash."""
    flights = [_flight("F1", 40000)]
    hotels = [
        _hotel("H1", nightly=10000, stars=4.0),
        _hotel("H2", nightly=20000, stars=4.5),
    ]
    picks = _pick_three_options(flights, hotels)
    assert len(picks) == 3   # never raises, never < 3 entries


def test_alternative_prefers_refundable_flight():
    flights = [
        _flight("F1", 40000, refundable=False),
        _flight("F2", 50000, refundable=True),    # refundable, costs more
    ]
    hotels = [
        _hotel("H1", nightly=10000, stars=4.0, neighborhood="Centro"),
        _hotel("H2", nightly=15000, stars=4.5, neighborhood="Alfama"),
        _hotel("H3", nightly=18000, stars=3.5, neighborhood="Belém"),
    ]
    picks = _pick_three_options(flights, hotels)
    f3, _ = picks[2]
    assert f3.id == "F2", "alternative option should prefer the refundable flight"


def test_alternative_prefers_new_neighborhood():
    flights = [_flight("F1", 40000)]
    hotels = [
        _hotel("H1", nightly=10000, stars=4.0, neighborhood="Centro"),    # cheapest
        _hotel("H2", nightly=15000, stars=5.0, neighborhood="Centro"),    # best-rated, same neighborhood
        _hotel("H3", nightly=20000, stars=4.0, neighborhood="Alfama"),    # different
    ]
    picks = _pick_three_options(flights, hotels)
    _, h3 = picks[2]
    assert h3.neighborhood == "Alfama", "alternative should explore a new neighborhood"


def test_cheapest_combo_actually_cheapest():
    flights = [_flight("F1", 60000), _flight("F2", 30000), _flight("F3", 90000)]
    hotels = [
        _hotel("H1", nightly=12000, stars=4.0),
        _hotel("H2", nightly=20000, stars=4.5),
    ]
    picks = _pick_three_options(flights, hotels)
    f1, h1 = picks[0]
    assert f1.id == "F2"
    assert h1.id == "H1"


def test_only_one_hotel_one_flight_does_not_crash():
    flights = [_flight("F1")]
    hotels = [_hotel("H1")]
    picks = _pick_three_options(flights, hotels)
    assert len(picks) == 3
    # Only one unique tuple available; degenerate by definition.


# ---- Comparison-aware narrative -----------------------------------------

import re
from agent.orchestrator import _compare_options, _narrative_for
from api.schemas import WeatherSummary, TripRequest
from datetime import date


def _weather() -> WeatherSummary:
    return WeatherSummary(
        location="LIS", window_start="2026-06-06", window_end="2026-06-10",
        summary="mostly sunny", avg_high_c=22.0, avg_low_c=14.0,
        rain_probability=0.18,
    )


def _trip() -> TripRequest:
    return TripRequest(
        request_id="r1", user_id="u1", raw_text="...",
        origin="JFK", destination="LIS",
        date_start=date(2026, 6, 6), date_end=date(2026, 6, 10),
        traveler_count=1, budget_total_usd=2000.0,
    )


def test_compare_options_basic_deltas():
    flights = [_flight("F1", 40000), _flight("F2", 60000, refundable=True)]
    hotels = [
        _hotel("H1", nightly=10000, stars=4.0, neighborhood="Centro"),
        _hotel("H2", nightly=20000, stars=4.5, neighborhood="Alfama"),
        _hotel("H3", nightly=15000, stars=3.5, neighborhood="Belém"),
    ]
    picks = _pick_three_options(flights, hotels)
    deltas = _compare_options(picks)

    # All three options have a delta dict
    assert len(deltas) == 3
    # Cheapest option's delta_vs_cheapest is 0
    cheapest_idx = min(range(3), key=lambda i: deltas[i]["total_cents"])
    assert deltas[cheapest_idx]["price_delta_vs_cheapest"] == 0
    # The others are positive
    for i in range(3):
        if i != cheapest_idx:
            assert deltas[i]["price_delta_vs_cheapest"] > 0


def test_cheapest_narrative_mentions_concrete_dollar_delta():
    flights = [_flight("F1", 40000)]
    hotels = [
        _hotel("H1", nightly=10000, stars=4.0, neighborhood="Centro"),
        _hotel("H2", nightly=21000, stars=4.5, neighborhood="Alfama"),
        _hotel("H3", nightly=15000, stars=3.5, neighborhood="Belém"),
    ]
    picks = _pick_three_options(flights, hotels)
    why, _ = _narrative_for("cheapest", picks, 0, _weather(), _trip())
    # Must mention a $-amount that's the savings vs another option
    assert re.search(r"\$\d", why), f"cheapest narrative lacks a $-delta: {why!r}"
    # Must reference another option by number
    assert "Option 2" in why or "Option 3" in why


def test_best_reviewed_narrative_mentions_markup_and_stars():
    flights = [_flight("F1", 40000)]
    hotels = [
        _hotel("H1", nightly=10000, stars=4.0, neighborhood="Centro"),
        _hotel("H2", nightly=21000, stars=4.5, neighborhood="Alfama"),
        _hotel("H3", nightly=15000, stars=3.5, neighborhood="Belém"),
    ]
    picks = _pick_three_options(flights, hotels)
    why, _ = _narrative_for("best_reviewed", picks, 1, _weather(), _trip())
    # Must mention dollar amount AND star rating
    assert re.search(r"\$\d", why)
    assert "★" in why


def test_alternative_narrative_calls_out_refundable():
    flights = [
        _flight("F1", 40000, refundable=False),
        _flight("F2", 55000, refundable=True),
    ]
    hotels = [
        _hotel("H1", nightly=10000, stars=4.0, neighborhood="Centro"),
        _hotel("H2", nightly=21000, stars=4.5, neighborhood="Alfama"),
        _hotel("H3", nightly=15000, stars=3.5, neighborhood="Belém"),
    ]
    picks = _pick_three_options(flights, hotels)
    # Find the index of the alternative option (third by construction)
    why, catch = _narrative_for("alternative", picks, 2, _weather(), _trip())
    assert "refundable" in why.lower() or any("refundable" in c.lower() for c in catch)


def test_three_narratives_are_distinct():
    """Even when LiteAPI sandbox surfaces near-duplicate options, the three
    narratives must NOT be identical strings."""
    flights = [_flight("F1", 40000)]
    hotels = [
        _hotel("H1", nightly=14000, stars=4.0, neighborhood="Lisbon"),
        _hotel("H2", nightly=14500, stars=4.0, neighborhood="Lisbon"),
        _hotel("H3", nightly=14100, stars=4.0, neighborhood="Lisbon"),
    ]
    picks = _pick_three_options(flights, hotels)
    whys = []
    for label, idx in [("cheapest", 0), ("best_reviewed", 1), ("alternative", 2)]:
        why, _ = _narrative_for(label, picks, idx, _weather(), _trip())
        whys.append(why)
    assert len(set(whys)) == 3, f"narratives collide: {whys}"


def test_zero_dollar_delta_does_not_say_zero():
    """When two options happen to have identical totals, the narrative for the
    'best_reviewed' branch still produces a useful sentence (no '$0 more')."""
    flights = [_flight("F1", 40000)]
    # All three hotels priced identically; differ only in stars.
    hotels = [
        _hotel("H1", nightly=15000, stars=4.0, neighborhood="A"),
        _hotel("H2", nightly=15000, stars=4.5, neighborhood="B"),
        _hotel("H3", nightly=15000, stars=3.5, neighborhood="C"),
    ]
    picks = _pick_three_options(flights, hotels)
    why, _ = _narrative_for("best_reviewed", picks, 1, _weather(), _trip())
    assert "$0" not in why, f"narrative includes '$0' delta: {why!r}"
