"""Duffel Stays adapter.

Endpoint: POST https://api.duffel.com/stays/search
Docs: https://duffel.com/docs/api/v2/stays-search-results

Auth: same Duffel bearer token as flights.
Request body needs a geographic radius (lat/lon + km). We reuse the
geocoding fallback table from tools/weather/openweather.py for known cities;
for unknown cities the adapter returns [] rather than guess.

Booking is NOT performed here — we surface offers only.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from api.schemas import HotelOffer
from tools.flights.duffel import _settings, DUFFEL_API
from tools.weather.openweather import GEO_FALLBACK


class DuffelStaysProvider:
    name = "duffel"

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    def _headers(self) -> dict:
        s = _settings()
        if not s.duffel_access_token:
            raise RuntimeError("DUFFEL_ACCESS_TOKEN not set")
        return {
            "Authorization": f"Bearer {s.duffel_access_token}",
            "Duffel-Version": s.duffel_api_version,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def search(
        self,
        *,
        destination: str,
        check_in: str,
        check_out: str,
        traveler_count: int,
        max_nightly_cents: int | None,
    ) -> list[HotelOffer]:
        coords = GEO_FALLBACK.get(destination.upper())
        if not coords:
            # Unknown city → don't fabricate. The orchestrator will fall back gracefully.
            return []
        lat, lon = coords
        nights = max((date.fromisoformat(check_out) - date.fromisoformat(check_in)).days, 1)

        body = {
            "data": {
                "check_in_date": check_in,
                "check_out_date": check_out,
                "rooms": 1,
                "guests": [{"type": "adult"} for _ in range(max(traveler_count, 1))],
                "location": {
                    "radius": 5,
                    "geographic_coordinates": {"latitude": lat, "longitude": lon},
                },
            }
        }
        cli = await self._http()
        res = await cli.post(
            f"{DUFFEL_API}/stays/search",
            headers=self._headers(),
            json=body,
        )
        if res.status_code == 401:
            raise RuntimeError("Duffel rejected token (check DUFFEL_ACCESS_TOKEN)")
        if res.status_code >= 400:
            return []

        results = res.json().get("data", {}).get("results", [])
        out: list[HotelOffer] = []
        for r in results:
            try:
                offer = _map_result(r, nights=nights, dest=destination)
            except Exception:
                continue
            if max_nightly_cents and offer.nightly_rate_cents > max_nightly_cents:
                continue
            out.append(offer)
        out.sort(key=lambda h: h.nightly_rate_cents)
        return out[:10]


# ----- mapping -------------------------------------------------------------

def _map_result(r: dict[str, Any], *, nights: int, dest: str) -> HotelOffer:
    """Map a Duffel Stays result to HotelOffer.

    Result shape (abbreviated):
      {
        "accommodation": {
          "id": "acc_...",
          "name": "Hotel Alfama Charm",
          "rating": 4,                           # 1..5 (may be float)
          "location": {"address": {"city_name": "Lisbon", "neighborhood": "Alfama"}},
          "photos": [{"url": "..."}],
          "review_score": 9.1                    # optional
        },
        "cheapest_rate_total_amount": "580.00",  # STRING, major units
        "cheapest_rate_total_currency": "USD",
        "cheapest_rate_public_url": "https://...",
        "check_in_date": "2026-06-06",
        "check_out_date": "2026-06-10"
      }

    Refundable-until isn't surfaced here — it's on the rate, fetched via
    POST /stays/quotes/{id}/create when the user picks an option. For the
    prototype we leave refundable_until=None and the consent text says
    "non-refundable" until we wire the quotes call.
    """
    acc = r.get("accommodation") or {}
    addr = (acc.get("location") or {}).get("address") or {}

    total = float(r.get("cheapest_rate_total_amount") or 0)
    currency = r.get("cheapest_rate_total_currency", "USD")
    nightly = total / nights if nights else total

    rating_raw = acc.get("rating")
    star_rating = float(rating_raw) if rating_raw is not None else 3.5

    return HotelOffer(
        id=f"DFL-{acc.get('id', 'unknown')}",
        provider="duffel",
        name=acc.get("name", "Unknown Hotel"),
        neighborhood=addr.get("neighborhood") or addr.get("city_name") or dest,
        check_in=r.get("check_in_date", ""),
        check_out=r.get("check_out_date", ""),
        nights=nights,
        nightly_rate_cents=int(round(nightly * 100)),
        total_price_cents=int(round(total * 100)),
        currency=currency,
        star_rating=star_rating,
        refundable_until=None,    # populated when a quote is fetched
        review_signals={},        # filled by reviews/semantic.py
        public_review_url=r.get("cheapest_rate_public_url"),
    )
