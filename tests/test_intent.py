"""Intent extraction tests.

Covers the regex fallback for budgets, dates, multi-word cities, and the
backfill that mirrors legs[0] into the legacy flat fields. The LLM path is
exercised by a single live test that skips when ANTHROPIC_API_KEY is unset.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from agent.intent import (
    IntentSchema,
    LegIntent,
    _backfill_flat_from_legs,
    _parse_budgets,
    _parse_start_date,
    _regex_fallback,
)


# ---- Budget parsing -------------------------------------------------------

def test_under_budget_sets_max_only():
    lo, hi = _parse_budgets("4 days in lisbon under $2,000")
    assert lo is None
    assert hi == 2000.0


def test_at_least_budget_sets_min_only():
    lo, hi = _parse_budgets("a week in london at least $5,000")
    assert lo == 5000.0
    assert hi is None


def test_minimum_budget_sets_min():
    lo, hi = _parse_budgets("minimum $10,000 in london")
    assert lo == 10000.0
    assert hi is None


def test_around_budget_sets_soft_range():
    lo, hi = _parse_budgets("around $3,000 in tokyo")
    assert lo == pytest.approx(2700.0)
    assert hi == pytest.approx(3300.0)


def test_explicit_range_sets_both():
    lo, hi = _parse_budgets("spend $5,000 to $10,000 in europe")
    assert lo == 5000.0
    assert hi == 10000.0


def test_missing_budget_returns_none():
    lo, hi = _parse_budgets("4 days in lisbon next month")
    assert lo is None
    assert hi is None


# ---- Date parsing ---------------------------------------------------------

ANCHOR = date(2026, 5, 7)   # session "today"


def test_iso_date_passthrough():
    assert _parse_start_date("4 days starting 2026-07-05", ANCHOR) == date(2026, 7, 5)


def test_natural_date_july_5_resolves_to_next_july():
    # July 5 is after May 7, so it should resolve to 2026-07-05, not 2027.
    assert _parse_start_date("starting july 5", ANCHOR) == date(2026, 7, 5)


def test_natural_date_in_past_jumps_to_next_year():
    # March 1 is before May 7, so it should resolve to 2027-03-01.
    assert _parse_start_date("starting march 1", ANCHOR) == date(2027, 3, 1)


def test_next_month_resolves_to_first_of_next_month():
    assert _parse_start_date("4 days in london next month", ANCHOR) == date(2026, 6, 1)


def test_default_date_is_today_plus_30():
    # No date phrase: fall through to today + 30 days.
    result = _parse_start_date("a long weekend in porto", ANCHOR)
    assert (result - ANCHOR).days == 30


# ---- Regex fallback end-to-end -------------------------------------------

def test_fallback_parses_known_city():
    intent = _regex_fallback("4 days in london under $3,000", ANCHOR)
    assert intent.destination == "LON"
    assert intent.legs[0].destination == "LON"
    assert intent.legs[0].budget_max_usd == 3000.0


def test_fallback_unknown_city_returns_none():
    intent = _regex_fallback("4 days in atlantis", ANCHOR)
    assert intent.destination is None
    assert intent.legs[0].destination is None


def test_fallback_min_spend_populates_budget_min_per_leg():
    intent = _regex_fallback("a week in london minimum $10,000", ANCHOR)
    assert intent.legs[0].budget_min_usd == 10000.0
    assert intent.legs[0].budget_max_usd is None
    # budget_total_usd mirrors the dominant ceiling, falling back to min.
    assert intent.budget_total_usd == 10000.0


def test_fallback_explicit_iso_date_propagates_to_leg():
    intent = _regex_fallback("4 days in london starting 2026-07-05", ANCHOR)
    assert intent.legs[0].date_start == "2026-07-05"
    assert intent.legs[0].date_end == "2026-07-09"
    # Flat field mirrors the single-leg case.
    assert intent.date_start == "2026-07-05"


# ---- Backfill helper ------------------------------------------------------

def test_backfill_mirrors_single_leg_into_flat_fields():
    intent = IntentSchema(
        origin="JFK",
        legs=[LegIntent(
            destination="LON",
            date_start="2026-07-05",
            date_end="2026-07-12",
            budget_max_usd=4000.0,
        )],
    )
    out = _backfill_flat_from_legs(intent)
    assert out.destination == "LON"
    assert out.date_start == "2026-07-05"
    assert out.date_end == "2026-07-12"
    assert out.budget_total_usd == 4000.0


def test_backfill_does_not_mirror_when_multi_leg():
    intent = IntentSchema(
        origin="JFK",
        legs=[
            LegIntent(destination="LON", budget_min_usd=10000),
            LegIntent(destination="NYC", budget_min_usd=5000),
        ],
    )
    out = _backfill_flat_from_legs(intent)
    # Multi-leg leaves flat fields null since they can't represent both legs.
    assert out.destination is None
    assert out.budget_total_usd is None


# ---- Live LLM (skipped when no key) --------------------------------------

@pytest.mark.asyncio
async def test_llm_multi_city_extraction():
    """Live test: requires real ANTHROPIC_API_KEY in env. Skipped otherwise.

    Validates the user's exact use case: multi-city with per-leg minimum
    budgets and an explicit start date.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set; live LLM test skipped")
    if os.environ.get("MOCK_LLM") == "1":
        pytest.skip("MOCK_LLM=1 forces regex path; this test exercises the LLM")

    from agent.intent import extract_intent

    intent = await extract_intent(
        "I want to spend at least $10,000 in London and at least $5,000 in "
        "New York, starting July 5 for 10 days total",
        today=ANCHOR,
    )
    # Expect two legs in order
    assert len(intent.legs) == 2
    dests = [leg.destination for leg in intent.legs]
    assert "LON" in dests or "London" in str(dests)
    assert "NYC" in dests or "New York" in str(dests)
    # Min budgets surface per leg
    london = next(leg for leg in intent.legs if leg.destination in ("LON", "London"))
    assert london.budget_min_usd == 10000.0
