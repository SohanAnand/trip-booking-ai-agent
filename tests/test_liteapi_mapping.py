"""LiteAPI hotel adapter tests with HTTP mocked."""

from __future__ import annotations

import httpx
import pytest

from tools.hotels.liteapi import LiteApiHotelProvider, _map_offer


# ----- fixtures -----------------------------------------------------------

LITEAPI_HOTELS_FIXTURE = {
    "data": [
        {"id": "lp_alfama", "name": "Hotel Alfama Charm", "stars": 4,
         "rating": 9.0, "city": "Lisbon", "country": "PT",
         "address": {"neighborhood": "Alfama"}},
        {"id": "lp_riverside", "name": "Lisbon Riverside Boutique", "stars": 4.5,
         "rating": 9.3, "city": "Lisbon", "country": "PT",
         "address": {"neighborhood": "Cais do Sodré"}},
        {"id": "lp_belem", "name": "Belém Garden Suites", "stars": 3.5,
         "rating": 8.4, "city": "Lisbon", "country": "PT",
         "address": {"neighborhood": "Belém"}},
    ]
}

LITEAPI_RATES_FIXTURE = {
    "data": [
        {
            "hotelId": "lp_alfama",
            "roomTypes": [{"rates": [{
                "name": "Standard Double", "boardName": "Room only",
                "retailRate": {"total": [{"amount": 580.00, "currency": "USD"}]},
                "cancellationPolicies": {
                    "refundableTag": "RFN",
                    "cancelPolicyInfos": [{"cancelTime": "2026-06-04T00:00:00"}],
                },
            }]}],
        },
        {
            "hotelId": "lp_riverside",
            "roomTypes": [{"rates": [{
                "name": "Deluxe King", "boardName": "Breakfast included",
                "retailRate": {"total": [{"amount": 840.00, "currency": "USD"}]},
                "cancellationPolicies": {"refundableTag": "RFN",
                                         "cancelPolicyInfos": [
                                             {"cancelTime": "2026-06-03T00:00:00"}
                                         ]},
            }]}],
        },
        {
            "hotelId": "lp_belem",
            "roomTypes": [{"rates": [{
                "name": "Garden Suite", "boardName": "Room only",
                "retailRate": {"total": [{"amount": 440.00, "currency": "USD"}]},
                "cancellationPolicies": {"refundableTag": "NRFN"},
            }]}],
        },
    ]
}


# ----- pure mapping --------------------------------------------------------

def test_map_offer_basic():
    entry = LITEAPI_RATES_FIXTURE["data"][0]
    meta = {"id": "lp_alfama", "name": "Hotel Alfama Charm", "stars": 4,
            "city": "Lisbon", "address": {"neighborhood": "Alfama"}}
    offer = _map_offer(entry, hotel_meta=meta, nights=4, dest="LIS")
    assert offer.id == "LITE-lp_alfama"
    assert offer.provider == "liteapi"
    assert offer.name == "Hotel Alfama Charm"
    assert offer.neighborhood == "Alfama"
    assert offer.total_price_cents == 58000
    assert offer.nightly_rate_cents == 14500
    assert offer.currency == "USD"
    assert offer.star_rating == 4.0
    assert offer.refundable_until == "2026-06-04T00:00:00"


def test_map_offer_non_refundable():
    entry = LITEAPI_RATES_FIXTURE["data"][2]   # Belém — NRFN
    meta = {"id": "lp_belem", "name": "Belém Garden Suites", "stars": 3.5,
            "city": "Lisbon", "address": {"neighborhood": "Belém"}}
    offer = _map_offer(entry, hotel_meta=meta, nights=4, dest="LIS")
    assert offer.refundable_until is None
    assert offer.nightly_rate_cents == 11000


def test_map_offer_picks_cheapest_rate_when_multiple():
    """If a hotel has several rates, pick the cheapest."""
    entry = {
        "hotelId": "lp_x",
        "roomTypes": [{
            "rates": [
                {"retailRate": {"total": [{"amount": 999.00, "currency": "USD"}]}},
                {"retailRate": {"total": [{"amount": 200.00, "currency": "USD"}]}},
                {"retailRate": {"total": [{"amount": 500.00, "currency": "USD"}]}},
            ],
        }],
    }
    meta = {"id": "lp_x", "name": "X Hotel", "stars": 3, "address": {}}
    offer = _map_offer(entry, hotel_meta=meta, nights=4, dest="LIS")
    assert offer.total_price_cents == 20000   # the $200 rate wins


def test_map_offer_raises_when_no_rates():
    entry = {"hotelId": "lp_empty", "roomTypes": []}
    meta = {"id": "lp_empty", "name": "Empty", "stars": 3, "address": {}}
    with pytest.raises(ValueError, match="no rates"):
        _map_offer(entry, hotel_meta=meta, nights=4, dest="LIS")


# ----- end-to-end with mock transport -------------------------------------

@pytest.mark.asyncio
async def test_liteapi_search_via_mock_transport(monkeypatch):
    monkeypatch.setenv("LITEAPI_KEY", "test_key_123")
    import api.config; import importlib; importlib.reload(api.config)

    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.setdefault("urls", []).append(str(req.url))
        captured["last_headers"] = dict(req.headers)
        if "data/hotels" in req.url.path:
            return httpx.Response(200, json=LITEAPI_HOTELS_FIXTURE)
        if "hotels/rates" in req.url.path:
            return httpx.Response(200, json=LITEAPI_RATES_FIXTURE)
        return httpx.Response(404, json={})

    cli = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = LiteApiHotelProvider(client=cli)
    offers = await provider.search(
        destination="LIS", check_in="2026-06-06", check_out="2026-06-10",
        traveler_count=1, max_nightly_cents=None,
    )
    assert len(offers) == 3
    # Sorted ascending by nightly rate
    assert offers[0].name == "Belém Garden Suites"        # cheapest at $110/night
    assert offers[1].name == "Hotel Alfama Charm"         # $145/night
    assert offers[2].name == "Lisbon Riverside Boutique"  # $210/night
    assert captured["last_headers"].get("x-api-key") == "test_key_123"
    # Both endpoints hit
    assert any("data/hotels" in u for u in captured["urls"])
    assert any("hotels/rates" in u for u in captured["urls"])


@pytest.mark.asyncio
async def test_liteapi_max_nightly_filter(monkeypatch):
    monkeypatch.setenv("LITEAPI_KEY", "test_key_123")
    import api.config; import importlib; importlib.reload(api.config)

    cli = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: (
        httpx.Response(200, json=LITEAPI_HOTELS_FIXTURE)
        if "data/hotels" in req.url.path
        else httpx.Response(200, json=LITEAPI_RATES_FIXTURE)
    )))
    provider = LiteApiHotelProvider(client=cli)
    # Cap at $130/night → only Belém ($110) qualifies.
    offers = await provider.search(
        destination="LIS", check_in="2026-06-06", check_out="2026-06-10",
        traveler_count=1, max_nightly_cents=13000,
    )
    assert len(offers) == 1
    assert offers[0].name == "Belém Garden Suites"


@pytest.mark.asyncio
async def test_liteapi_unknown_city_returns_empty(monkeypatch):
    """Unknown city → empty list (don't fabricate)."""
    monkeypatch.setenv("LITEAPI_KEY", "test_key_123")
    import api.config; import importlib; importlib.reload(api.config)

    cli = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"data": []})
    ))
    provider = LiteApiHotelProvider(client=cli)
    offers = await provider.search(
        destination="ZZZ", check_in="2026-06-06", check_out="2026-06-10",
        traveler_count=1, max_nightly_cents=None,
    )
    assert offers == []


@pytest.mark.asyncio
async def test_liteapi_401_raises_clear_error(monkeypatch):
    monkeypatch.setenv("LITEAPI_KEY", "bad_key")
    import api.config; import importlib; importlib.reload(api.config)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api key"})

    cli = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = LiteApiHotelProvider(client=cli)
    with pytest.raises(RuntimeError, match="rejected key"):
        await provider.search(
            destination="LIS", check_in="2026-06-06", check_out="2026-06-10",
            traveler_count=1, max_nightly_cents=None,
        )


# ----- selection precedence ------------------------------------------------

def test_orchestrator_prefers_liteapi_for_hotels(monkeypatch):
    """When both DUFFEL_ACCESS_TOKEN and LITEAPI_KEY are set, LiteAPI wins for hotels."""
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_demo")
    monkeypatch.setenv("LITEAPI_KEY", "test_key_123")
    import api.config; import importlib; importlib.reload(api.config)

    from agent.orchestrator import build_tool_registry
    r = build_tool_registry()
    assert r["search_flights"].impl.name == "duffel"
    assert r["search_hotels"].impl.name == "liteapi"
