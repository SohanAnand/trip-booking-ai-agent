"""Adapter tests with HTTP mocked.

We test the response-mapping layer; the network is replaced by httpx.MockTransport.
This catches schema drift in our adapters without burning Amadeus quota.
"""

from __future__ import annotations

import httpx
import pytest

from tools.flights.amadeus import AmadeusFlightProvider, _map_offer
from tools.hotels.amadeus import AmadeusHotelProvider


# ---- flight mapping --------------------------------------------------------

AMA_FLIGHT_FIXTURE = {
    "data": [
        {
            "id": "1",
            "itineraries": [
                {"segments": [
                    {
                        "departure": {"iataCode": "JFK", "at": "2026-06-06T18:30:00"},
                        "arrival": {"iataCode": "LIS", "at": "2026-06-07T07:15:00"},
                        "duration": "PT6H45M",
                        "carrierCode": "TP", "number": "202",
                    }
                ]},
                {"segments": [
                    {
                        "departure": {"iataCode": "LIS", "at": "2026-06-10T11:00:00"},
                        "arrival": {"iataCode": "JFK", "at": "2026-06-10T14:25:00"},
                        "duration": "PT8H25M",
                        "carrierCode": "TP", "number": "201",
                    }
                ]},
            ],
            "price": {"grandTotal": "684.00", "currency": "USD"},
            "travelerPricings": [{"fareDetailsBySegment": [
                {"includedCheckedBags": {"quantity": 1}, "fareBasis": "YFLEX"}
            ]}],
        }
    ]
}


def test_map_flight_offer_basic():
    offer = _map_offer(AMA_FLIGHT_FIXTURE["data"][0])
    assert offer.id == "AMA-1"
    assert offer.total_price_cents == 68400
    assert offer.currency == "USD"
    assert offer.baggage_included is True
    assert len(offer.outbound) == 1
    assert offer.outbound[0].origin == "JFK"
    assert offer.outbound[0].destination == "LIS"
    assert offer.outbound[0].duration_minutes == 6 * 60 + 45
    assert offer.outbound[0].refundable is True   # YFLEX → flex


def test_map_flight_offer_iso_duration_edge_cases():
    from tools.flights.amadeus import _parse_iso_duration_minutes
    assert _parse_iso_duration_minutes("PT0M") == 0
    assert _parse_iso_duration_minutes("PT45M") == 45
    assert _parse_iso_duration_minutes("PT1H") == 60
    assert _parse_iso_duration_minutes("PT8H25M") == 505


# ---- end-to-end adapter (HTTP mocked) -------------------------------------

@pytest.mark.asyncio
async def test_flight_search_with_mock_transport(monkeypatch):
    monkeypatch.setenv("AMADEUS_CLIENT_ID", "demo")
    monkeypatch.setenv("AMADEUS_CLIENT_SECRET", "demo")
    import api.config; import importlib; importlib.reload(api.config)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
        if req.url.path.endswith("/flight-offers"):
            return httpx.Response(200, json=AMA_FLIGHT_FIXTURE)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    cli = httpx.AsyncClient(transport=transport)
    provider = AmadeusFlightProvider(client=cli)
    offers = await provider.search(
        origin="JFK", destination="LIS",
        date_start="2026-06-06", date_end="2026-06-10",
        traveler_count=1, max_price_cents=200000,
    )
    assert len(offers) == 1
    assert offers[0].outbound[0].carrier == "TP"


# ---- hotel mapping --------------------------------------------------------

AMA_HOTEL_OFFERS_FIXTURE = {
    "data": [
        {
            "hotel": {
                "hotelId": "ABCDE",
                "name": "Hotel Alfama Charm",
                "rating": "4",
                "address": {"cityName": "Lisbon"},
            },
            "offers": [{
                "price": {"total": "580.00", "currency": "USD"},
                "checkInDate": "2026-06-06",
                "checkOutDate": "2026-06-10",
                "policies": {"cancellation": {
                    "type": "FULL_STAY", "deadline": "2026-06-04T00:00:00",
                }},
            }],
        }
    ]
}

AMA_HOTEL_IDS_FIXTURE = {
    "data": [{"hotelId": "ABCDE"}, {"hotelId": "FGHIJ"}],
}


@pytest.mark.asyncio
async def test_hotel_search_with_mock_transport(monkeypatch):
    monkeypatch.setenv("AMADEUS_CLIENT_ID", "demo")
    monkeypatch.setenv("AMADEUS_CLIENT_SECRET", "demo")
    import api.config; import importlib; importlib.reload(api.config)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
        if "by-city" in req.url.path:
            return httpx.Response(200, json=AMA_HOTEL_IDS_FIXTURE)
        if "hotel-offers" in req.url.path:
            return httpx.Response(200, json=AMA_HOTEL_OFFERS_FIXTURE)
        return httpx.Response(404)

    cli = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AmadeusHotelProvider(client=cli)
    offers = await provider.search(
        destination="LIS", check_in="2026-06-06", check_out="2026-06-10",
        traveler_count=1, max_nightly_cents=20000,
    )
    assert len(offers) == 1
    assert offers[0].name == "Hotel Alfama Charm"
    assert offers[0].nightly_rate_cents == 14500    # 580/4 nights × 100
    assert offers[0].total_price_cents == 58000
    assert offers[0].refundable_until == "2026-06-04T00:00:00"
