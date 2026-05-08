"""Amadeus Self-Service flight search adapter.

Endpoint: GET /v2/shopping/flight-offers
Docs: https://developers.amadeus.com/self-service/category/flights/api-doc/flight-offers-search

Auth: OAuth2 client_credentials → access_token (cached in-process).
Free tier: 2,000 requests/month.

For tests we inject an httpx.AsyncClient (or a transport) so HTTP can be mocked.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from api.schemas import FlightOffer, FlightSegment


def _settings():
    """Re-resolve settings on every call so tests that monkeypatch env can rebind."""
    from api.config import settings
    return settings

PROD = "https://api.amadeus.com"
TEST = "https://test.api.amadeus.com"


def _base_url() -> str:
    return TEST if os.environ.get("AMADEUS_ENV", "test") != "prod" else PROD


class AmadeusFlightProvider:
    name = "amadeus"

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._token: str | None = None
        self._token_expires: float = 0.0

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        s = _settings()
        if not s.amadeus_client_id or not s.amadeus_client_secret:
            raise RuntimeError(
                "AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET not set. "
                "Get free creds at developers.amadeus.com (Self-Service)."
            )
        cli = await self._http()
        res = await cli.post(
            f"{_base_url()}/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": s.amadeus_client_id,
                "client_secret": s.amadeus_client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        res.raise_for_status()
        data = res.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + int(data.get("expires_in", 1800))
        return self._token

    async def search(
        self,
        *,
        origin: str,
        destination: str,
        date_start: str,
        date_end: str,
        traveler_count: int,
        max_price_cents: int | None,
        cabin_class: str = "economy",
        one_way: bool = False,
    ) -> list[FlightOffer]:
        """Return up to 10 mapped FlightOffers, sorted by price ascending.

        cabin_class is forwarded to Amadeus as the `travelClass` param. The
        self-service tier accepts ECONOMY / PREMIUM_ECONOMY / BUSINESS / FIRST.
        Without this the agent's luxury asks silently degraded to economy.

        one_way=True omits the returnDate parameter so Amadeus returns
        outbound-only offers; the agent assembles round-trips by calling
        search twice (outbound, then return with origin/destination flipped).
        """
        token = await self._get_token()
        params: dict[str, Any] = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": date_start,
            "adults": traveler_count,
            "currencyCode": "USD",
            "max": 10,
        }
        if not one_way:
            params["returnDate"] = date_end
        # Amadeus expects travelClass = ECONOMY | PREMIUM_ECONOMY | BUSINESS | FIRST.
        valid_cabins = {"economy", "premium_economy", "business", "first"}
        cabin = (cabin_class or "economy").lower()
        if cabin in valid_cabins and cabin != "economy":
            params["travelClass"] = cabin.upper()
        if max_price_cents:
            params["maxPrice"] = max_price_cents // 100
        cli = await self._http()
        res = await cli.get(
            f"{_base_url()}/v2/shopping/flight-offers",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if res.status_code == 400:
            # Amadeus rejects some city codes; surface gracefully
            return []
        res.raise_for_status()
        return [_map_offer(o) for o in res.json().get("data", [])]


def _map_offer(o: dict) -> FlightOffer:
    """Map an Amadeus FlightOffer payload to our schema.

    Amadeus offer shape (abbreviated):
      { id, itineraries: [ { segments: [...] }, { segments: [...] } ],
        price: { total, currency, grandTotal },
        travelerPricings: [ { fareDetailsBySegment: [ { includedCheckedBags, fareBasis } ] } ] }
    """
    itins = o.get("itineraries", [])
    outbound = [_map_segment(s) for s in itins[0]["segments"]] if itins else []
    inbound = [_map_segment(s) for s in itins[1]["segments"]] if len(itins) > 1 else []

    price = o.get("price", {})
    total = float(price.get("grandTotal") or price.get("total") or 0)
    currency = price.get("currency", "USD")

    fare_details = (
        o.get("travelerPricings", [{}])[0]
         .get("fareDetailsBySegment", [{}])
    )
    bags = (fare_details[0] or {}).get("includedCheckedBags", {}) if fare_details else {}
    baggage_included = bool(bags.get("quantity", 0)) or bool(bags.get("weight", 0))
    fare_basis = (fare_details[0] or {}).get("fareBasis", "") if fare_details else ""
    refundable = "FLEX" in fare_basis or "REFUND" in fare_basis
    for seg in outbound + inbound:
        seg.refundable = refundable

    return FlightOffer(
        id=f"AMA-{o.get('id', 'unknown')}",
        provider="amadeus",
        outbound=outbound,
        inbound=inbound,
        total_price_cents=int(round(total * 100)),
        currency=currency,
        baggage_included=baggage_included,
    )


def _map_segment(s: dict) -> FlightSegment:
    duration = s.get("duration", "PT0M")    # ISO 8601 duration
    minutes = _parse_iso_duration_minutes(duration)
    return FlightSegment(
        carrier=s.get("carrierCode", "??"),
        flight_number=str(s.get("number", "")),
        origin=s["departure"]["iataCode"],
        destination=s["arrival"]["iataCode"],
        depart=s["departure"]["at"],
        arrive=s["arrival"]["at"],
        duration_minutes=minutes,
        fare_class=s.get("co2Emissions", [{}])[0].get("cabin", "Y") if s.get("co2Emissions") else "Y",
        refundable=False,    # set per-offer above
    )


def _parse_iso_duration_minutes(d: str) -> int:
    """Parse 'PT4H30M' → 270."""
    d = d.removeprefix("PT")
    h = m = 0
    if "H" in d:
        h_str, rest = d.split("H", 1)
        h = int(h_str)
        d = rest
    if "M" in d:
        m_str, _ = d.split("M", 1)
        m = int(m_str)
    return h * 60 + m
