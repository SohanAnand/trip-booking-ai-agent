"""Amadeus Hotel Search adapter.

Two-call flow:
  1. /v1/reference-data/locations/hotels/by-city → list of hotelIds for a city
  2. /v3/shopping/hotel-offers?hotelIds=... → priced offers for given dates

We do NOT book here — booking is deliberately mocked at the prototype scale.
"""

from __future__ import annotations

from datetime import date

import httpx

from api.schemas import HotelOffer
from tools.flights.amadeus import _base_url, _settings   # share env helpers


# Common city-name → IATA city code mapping for the M2 demo.
# A real product would call /v1/reference-data/locations to look these up.
CITY_CODES = {
    "LIS": "LIS", "LISBON": "LIS", "LISBOA": "LIS",
    "PORTO": "OPO",
    "PARIS": "PAR", "CDG": "PAR",
    "ROME": "ROM", "FCO": "ROM",
    "BARCELONA": "BCN",
    "TOKYO": "TYO", "HND": "TYO", "NRT": "TYO",
    "NYC": "NYC", "JFK": "NYC", "LGA": "NYC", "EWR": "NYC",
}


class AmadeusHotelProvider:
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
        # Reuse the flight provider token logic by calling the same endpoint.
        import time
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        s = _settings()
        if not s.amadeus_client_id or not s.amadeus_client_secret:
            raise RuntimeError("AMADEUS_CLIENT_ID/SECRET not set")
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
        destination: str,
        check_in: str,
        check_out: str,
        traveler_count: int,
        max_nightly_cents: int | None,
    ) -> list[HotelOffer]:
        city = CITY_CODES.get(destination.upper(), destination[:3].upper())
        token = await self._get_token()
        cli = await self._http()
        # Step 1: hotel IDs by city
        ids_res = await cli.get(
            f"{_base_url()}/v1/reference-data/locations/hotels/by-city",
            params={"cityCode": city, "radius": 5, "radiusUnit": "KM"},
            headers={"Authorization": f"Bearer {token}"},
        )
        if ids_res.status_code != 200:
            return []
        hotel_ids = [h["hotelId"] for h in ids_res.json().get("data", [])][:20]
        if not hotel_ids:
            return []

        # Step 2: priced offers
        offers_res = await cli.get(
            f"{_base_url()}/v3/shopping/hotel-offers",
            params={
                "hotelIds": ",".join(hotel_ids),
                "checkInDate": check_in,
                "checkOutDate": check_out,
                "adults": traveler_count,
                "currency": "USD",
                "bestRateOnly": "true",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        if offers_res.status_code != 200:
            return []
        nights = max((date.fromisoformat(check_out) - date.fromisoformat(check_in)).days, 1)
        out: list[HotelOffer] = []
        for entry in offers_res.json().get("data", []):
            try:
                out.append(_map_offer(entry, nights=nights, dest=destination))
            except Exception:
                continue   # skip malformed entries
        if max_nightly_cents:
            out = [o for o in out if o.nightly_rate_cents <= max_nightly_cents]
        out.sort(key=lambda h: h.nightly_rate_cents)
        return out[:10]


def _map_offer(entry: dict, *, nights: int, dest: str) -> HotelOffer:
    hotel = entry.get("hotel", {})
    offers = entry.get("offers", [])
    if not offers:
        raise ValueError("no offers")
    first = offers[0]
    price = first.get("price", {})
    total = float(price.get("total") or 0)
    currency = price.get("currency", "USD")
    nightly = total / nights if nights else total
    rating = float(hotel.get("rating") or 3.5)
    policies = first.get("policies", {})
    refundable_until = None
    cancel = policies.get("cancellation") or policies.get("cancellations", [{}])[0]
    if cancel and cancel.get("type") in ("FULL_STAY", "REFUNDABLE"):
        refundable_until = cancel.get("deadline")

    return HotelOffer(
        id=f"AMA-{hotel.get('hotelId', 'unknown')}",
        provider="amadeus",
        name=hotel.get("name", "Unknown Hotel"),
        neighborhood=hotel.get("address", {}).get("cityName", dest),
        check_in=first.get("checkInDate", ""),
        check_out=first.get("checkOutDate", ""),
        nights=nights,
        nightly_rate_cents=int(round(nightly * 100)),
        total_price_cents=int(round(total * 100)),
        currency=currency,
        star_rating=rating,
        refundable_until=refundable_until,
        review_signals={},   # filled by tools/reviews/* + reviews/semantic
        public_review_url=None,
    )
