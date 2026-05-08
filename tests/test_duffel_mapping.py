"""Duffel adapter tests with HTTP mocked.

We verify the response-mapping layer against representative Duffel payloads.
The network is replaced by httpx.MockTransport so tests don't burn real
sandbox quota.
"""

from __future__ import annotations

import httpx
import pytest

from tools.flights.duffel import DuffelFlightProvider, _map_offer
from tools.hotels.duffel import DuffelStaysProvider


# ----- flight fixture & mapping -------------------------------------------

DUFFEL_FLIGHT_FIXTURE = {
    "data": {
        "id": "orq_abc123",
        "offers": [
            {
                "id": "off_xyz1",
                "total_amount": "684.00",
                "total_currency": "USD",
                "owner": {"iata_code": "TP"},
                "conditions": {
                    "refund_before_departure": {"allowed": True, "penalty_amount": "30.00"},
                    "change_before_departure": {"allowed": True, "penalty_amount": "75.00"},
                },
                "slices": [
                    {
                        "id": "sli_out",
                        "segments": [
                            {
                                "origin": {"iata_code": "JFK"},
                                "destination": {"iata_code": "LIS"},
                                "departing_at": "2026-06-06T18:30:00",
                                "arriving_at": "2026-06-07T07:15:00",
                                "duration": "PT6H45M",
                                "marketing_carrier": {"iata_code": "TP"},
                                "marketing_carrier_flight_number": "202",
                                "passengers": [{
                                    "cabin_class": "economy",
                                    "baggages": [
                                        {"type": "checked", "quantity": 1},
                                        {"type": "carry_on", "quantity": 1},
                                    ],
                                }],
                            }
                        ],
                    },
                    {
                        "id": "sli_ret",
                        "segments": [
                            {
                                "origin": {"iata_code": "LIS"},
                                "destination": {"iata_code": "JFK"},
                                "departing_at": "2026-06-10T11:00:00",
                                "arriving_at": "2026-06-10T14:25:00",
                                "duration": "PT8H25M",
                                "marketing_carrier": {"iata_code": "TP"},
                                "marketing_carrier_flight_number": "201",
                                "passengers": [{
                                    "cabin_class": "economy",
                                    "baggages": [{"type": "checked", "quantity": 1}],
                                }],
                            }
                        ],
                    },
                ],
            }
        ],
    },
}


def test_map_duffel_offer_basic():
    offer = _map_offer(DUFFEL_FLIGHT_FIXTURE["data"]["offers"][0])
    assert offer.id == "DFL-off_xyz1"
    assert offer.provider == "duffel"
    assert offer.total_price_cents == 68400
    assert offer.currency == "USD"
    assert offer.baggage_included is True
    assert len(offer.outbound) == 1
    assert offer.outbound[0].carrier == "TP"
    assert offer.outbound[0].flight_number == "202"
    assert offer.outbound[0].origin == "JFK"
    assert offer.outbound[0].destination == "LIS"
    assert offer.outbound[0].duration_minutes == 6 * 60 + 45
    # conditions.refund_before_departure.allowed = True propagates to all segments
    assert offer.outbound[0].refundable is True
    assert offer.inbound[0].refundable is True


def test_map_duffel_non_refundable():
    fixture = {
        **DUFFEL_FLIGHT_FIXTURE["data"]["offers"][0],
        "conditions": {"refund_before_departure": {"allowed": False}},
    }
    offer = _map_offer(fixture)
    assert offer.outbound[0].refundable is False


def test_map_duffel_no_baggage():
    no_bags = {
        **DUFFEL_FLIGHT_FIXTURE["data"]["offers"][0],
        "slices": [
            {"segments": [{
                "origin": {"iata_code": "JFK"},
                "destination": {"iata_code": "LIS"},
                "departing_at": "2026-06-06T18:30:00",
                "arriving_at": "2026-06-07T07:15:00",
                "duration": "PT6H45M",
                "marketing_carrier": {"iata_code": "TP"},
                "marketing_carrier_flight_number": "202",
                "passengers": [{"cabin_class": "economy", "baggages": []}],
            }]}
        ],
    }
    offer = _map_offer(no_bags)
    assert offer.baggage_included is False


@pytest.mark.asyncio
async def test_duffel_flight_search_via_mock_transport(monkeypatch):
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_demo")
    import api.config; import importlib; importlib.reload(api.config)

    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["headers"] = dict(req.headers)
        return httpx.Response(200, json=DUFFEL_FLIGHT_FIXTURE)

    cli = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DuffelFlightProvider(client=cli)
    offers = await provider.search(
        origin="JFK", destination="LIS",
        date_start="2026-06-06", date_end="2026-06-10",
        traveler_count=1, max_price_cents=200000,
    )
    assert len(offers) == 1
    assert offers[0].outbound[0].carrier == "TP"
    # Auth headers wired correctly
    assert captured["headers"].get("authorization") == "Bearer duffel_test_demo"
    assert captured["headers"].get("duffel-version") == "v2"
    # offer_requests endpoint with synchronous flag
    assert "/air/offer_requests" in captured["url"]
    assert "return_offers=true" in captured["url"]


@pytest.mark.asyncio
async def test_duffel_max_price_filter(monkeypatch):
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_demo")
    import api.config; import importlib; importlib.reload(api.config)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=DUFFEL_FLIGHT_FIXTURE)

    cli = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DuffelFlightProvider(client=cli)
    # $684 offer; cap at $500
    offers = await provider.search(
        origin="JFK", destination="LIS",
        date_start="2026-06-06", date_end="2026-06-10",
        traveler_count=1, max_price_cents=50000,
    )
    assert offers == []


# ----- stays fixture & mapping --------------------------------------------

DUFFEL_STAYS_FIXTURE = {
    "data": {
        "results": [
            {
                "accommodation": {
                    "id": "acc_abc",
                    "name": "Hotel Alfama Charm",
                    "rating": 4,
                    "location": {
                        "address": {"city_name": "Lisbon", "neighborhood": "Alfama"},
                    },
                    "review_score": 9.1,
                },
                "cheapest_rate_total_amount": "580.00",
                "cheapest_rate_total_currency": "USD",
                "cheapest_rate_public_url": "https://example.com/duffel/acc_abc",
                "check_in_date": "2026-06-06",
                "check_out_date": "2026-06-10",
            },
            {
                "accommodation": {
                    "id": "acc_def",
                    "name": "Riverside Boutique",
                    "rating": 4.5,
                    "location": {
                        "address": {"city_name": "Lisbon", "neighborhood": "Cais do Sodré"},
                    },
                },
                "cheapest_rate_total_amount": "840.00",
                "cheapest_rate_total_currency": "USD",
                "check_in_date": "2026-06-06",
                "check_out_date": "2026-06-10",
            },
        ]
    }
}


@pytest.mark.asyncio
async def test_duffel_stays_search_via_mock_transport(monkeypatch):
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_demo")
    import api.config; import importlib; importlib.reload(api.config)

    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(200, json=DUFFEL_STAYS_FIXTURE)

    cli = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DuffelStaysProvider(client=cli)
    offers = await provider.search(
        destination="LIS", check_in="2026-06-06", check_out="2026-06-10",
        traveler_count=1, max_nightly_cents=None,
    )
    assert len(offers) == 2
    # Sorted by nightly rate ascending
    assert offers[0].name == "Hotel Alfama Charm"
    assert offers[0].nightly_rate_cents == 14500    # 580/4 * 100
    assert offers[0].total_price_cents == 58000
    assert offers[0].star_rating == 4.0
    assert offers[0].neighborhood == "Alfama"
    assert offers[0].public_review_url == "https://example.com/duffel/acc_abc"
    assert offers[1].name == "Riverside Boutique"
    assert "/stays/search" in captured["url"]


@pytest.mark.asyncio
async def test_duffel_stays_unknown_city_returns_empty(monkeypatch):
    """We don't fabricate coords for unknown cities — the orchestrator falls back."""
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_demo")
    import api.config; import importlib; importlib.reload(api.config)

    cli = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=DUFFEL_STAYS_FIXTURE)
    ))
    provider = DuffelStaysProvider(client=cli)
    offers = await provider.search(
        destination="ZZZ", check_in="2026-06-06", check_out="2026-06-10",
        traveler_count=1, max_nightly_cents=None,
    )
    assert offers == []


@pytest.mark.asyncio
async def test_duffel_stays_max_nightly_filter(monkeypatch):
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_demo")
    import api.config; import importlib; importlib.reload(api.config)

    cli = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=DUFFEL_STAYS_FIXTURE)
    ))
    provider = DuffelStaysProvider(client=cli)
    # Cap at $150/night → only the $145/night Alfama option qualifies
    offers = await provider.search(
        destination="LIS", check_in="2026-06-06", check_out="2026-06-10",
        traveler_count=1, max_nightly_cents=15000,
    )
    assert len(offers) == 1
    assert offers[0].name == "Hotel Alfama Charm"


# ----- token-rejection path ------------------------------------------------

@pytest.mark.asyncio
async def test_duffel_401_raises_clear_error(monkeypatch):
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_bad")
    import api.config; import importlib; importlib.reload(api.config)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errors": [{"message": "invalid token"}]})

    cli = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DuffelFlightProvider(client=cli)
    with pytest.raises(RuntimeError, match="rejected token"):
        await provider.search(
            origin="JFK", destination="LIS",
            date_start="2026-06-06", date_end="2026-06-10",
            traveler_count=1, max_price_cents=None,
        )
