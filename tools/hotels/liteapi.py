"""LiteAPI hotel search adapter.

Two-call flow:
  1. GET /v3.0/data/hotels?cityName=...&countryCode=... → hotelIds for the city
  2. POST /v3.0/hotels/rates with {hotelIds, checkin, checkout, occupancies}
     → priced offers

Auth: X-API-Key header.
Sign up free at https://www.liteapi.travel/ — self-serve, no business verification.
Sandbox base: https://api.liteapi.travel/v3.0  (some endpoints differ in prod).

We do NOT book here — this prototype surfaces offers only.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from api.schemas import HotelOffer


SANDBOX_BASE = "https://api.liteapi.travel/v3.0"
PROD_BASE = "https://api.liteapi.travel/v3.0"   # same path; differentiate via key prefix


def _settings():
    """Lazy settings lookup so test monkeypatching takes effect."""
    from api.config import settings
    return settings


def _base_url() -> str:
    return SANDBOX_BASE if _settings().liteapi_use_sandbox else PROD_BASE


# Demo cities → ISO country code. The /data/hotels endpoint requires both.
# Real product would call /data/cities to look these up dynamically.
CITY_COUNTRY = {
    # Iberia
    "LIS": ("Lisbon", "PT"),       "LISBON": ("Lisbon", "PT"),    "LISBOA": ("Lisbon", "PT"),
    "OPO": ("Porto", "PT"),        "PORTO": ("Porto", "PT"),
    "MAD": ("Madrid", "ES"),       "MADRID": ("Madrid", "ES"),
    "BCN": ("Barcelona", "ES"),    "BARCELONA": ("Barcelona", "ES"),
    # Western/Central Europe
    "PAR": ("Paris", "FR"),        "PARIS": ("Paris", "FR"),
    "LON": ("London", "GB"),       "LONDON": ("London", "GB"),
    "AMS": ("Amsterdam", "NL"),    "AMSTERDAM": ("Amsterdam", "NL"),
    "BER": ("Berlin", "DE"),       "BERLIN": ("Berlin", "DE"),
    "MUC": ("Munich", "DE"),       "MUNICH": ("Munich", "DE"),
    "VIE": ("Vienna", "AT"),       "VIENNA": ("Vienna", "AT"),
    "PRG": ("Prague", "CZ"),       "PRAGUE": ("Prague", "CZ"),
    # Southern Europe
    "ROM": ("Rome", "IT"),         "ROME": ("Rome", "IT"),        "ROMA": ("Rome", "IT"),
    "MIL": ("Milan", "IT"),        "MILAN": ("Milan", "IT"),      "MILANO": ("Milan", "IT"),
    "ATH": ("Athens", "GR"),       "ATHENS": ("Athens", "GR"),
    "IST": ("Istanbul", "TR"),     "ISTANBUL": ("Istanbul", "TR"),
    # Middle East / Asia / Pacific
    "DXB": ("Dubai", "AE"),        "DUBAI": ("Dubai", "AE"),
    "TYO": ("Tokyo", "JP"),        "TOKYO": ("Tokyo", "JP"),
    "OSA": ("Osaka", "JP"),        "OSAKA": ("Osaka", "JP"),
    "SEL": ("Seoul", "KR"),        "SEOUL": ("Seoul", "KR"),
    "BKK": ("Bangkok", "TH"),      "BANGKOK": ("Bangkok", "TH"),
    "SIN": ("Singapore", "SG"),    "SINGAPORE": ("Singapore", "SG"),
    "HKG": ("Hong Kong", "HK"),    "HONG KONG": ("Hong Kong", "HK"),
    "SYD": ("Sydney", "AU"),       "SYDNEY": ("Sydney", "AU"),
    "MEL": ("Melbourne", "AU"),    "MELBOURNE": ("Melbourne", "AU"),
    # Americas
    "NYC": ("New York", "US"),     "NEW YORK": ("New York", "US"),
    "LAX": ("Los Angeles", "US"),  "LOS ANGELES": ("Los Angeles", "US"),
    "SFO": ("San Francisco", "US"),"SAN FRANCISCO": ("San Francisco", "US"),
    "CHI": ("Chicago", "US"),      "CHICAGO": ("Chicago", "US"),
    "MIA": ("Miami", "US"),        "MIAMI": ("Miami", "US"),
    "BOS": ("Boston", "US"),       "BOSTON": ("Boston", "US"),
    "YTO": ("Toronto", "CA"),      "TORONTO": ("Toronto", "CA"),
    "YVR": ("Vancouver", "CA"),    "VANCOUVER": ("Vancouver", "CA"),
    "MEX": ("Mexico City", "MX"),  "MEXICO CITY": ("Mexico City", "MX"),
}


class LiteApiHotelProvider:
    name = "liteapi"

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    def _headers(self) -> dict:
        s = _settings()
        if not s.liteapi_key:
            raise RuntimeError(
                "LITEAPI_KEY not set. "
                "Get a free key at https://www.liteapi.travel/ → Dashboard."
            )
        return {
            "X-API-Key": s.liteapi_key,
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
        city_country = CITY_COUNTRY.get(destination.upper())
        if not city_country:
            return []
        city_name, country_code = city_country
        nights = max((date.fromisoformat(check_out) - date.fromisoformat(check_in)).days, 1)

        cli = await self._http()

        # Step 1: list hotels in the city.
        ids_res = await cli.get(
            f"{_base_url()}/data/hotels",
            params={"cityName": city_name, "countryCode": country_code, "limit": 25},
            headers=self._headers(),
        )
        if ids_res.status_code == 401:
            raise RuntimeError("LiteAPI rejected key (check LITEAPI_KEY)")
        if ids_res.status_code >= 400:
            return []
        hotel_index = {h["id"]: h for h in ids_res.json().get("data", []) if h.get("id")}
        if not hotel_index:
            return []

        # Step 2: rate the first 20 hotels for these dates.
        body = {
            "hotelIds": list(hotel_index)[:20],
            "checkin": check_in,
            "checkout": check_out,
            "currency": "USD",
            "guestNationality": "US",
            "occupancies": [{"adults": max(traveler_count, 1), "children": []}],
        }
        rates_res = await cli.post(
            f"{_base_url()}/hotels/rates",
            headers=self._headers(),
            json=body,
        )
        if rates_res.status_code >= 400:
            return []

        out: list[HotelOffer] = []
        for entry in rates_res.json().get("data", []):
            hotel_meta = hotel_index.get(entry.get("hotelId"))
            if not hotel_meta:
                continue
            try:
                offer = _map_offer(entry, hotel_meta=hotel_meta,
                                   nights=nights, dest=destination)
            except Exception:
                continue
            if max_nightly_cents and offer.nightly_rate_cents > max_nightly_cents:
                continue
            out.append(offer)
        out.sort(key=lambda h: h.nightly_rate_cents)
        return out[:10]


# ----- mapping -------------------------------------------------------------

def _map_offer(entry: dict[str, Any], *, hotel_meta: dict, nights: int,
               dest: str) -> HotelOffer:
    """Map a LiteAPI rates entry to HotelOffer.

    Rates entry shape (abbreviated):
      {
        "hotelId": "lp123",
        "roomTypes": [
          {
            "rates": [
              {
                "retailRate": {
                  "total": [{"amount": 580.00, "currency": "USD"}],
                  "suggestedSellingPrice": [...]
                },
                "cancellationPolicies": {
                  "refundableTag": "RFN",
                  "cancelPolicyInfos": [{"cancelTime": "2026-06-04T00:00:00", ...}]
                },
                "name": "Standard Double Room",
                "boardName": "Room only"
              }
            ]
          }
        ]
      }

    Hotel metadata (from /data/hotels):
      { "id": "lp123", "name": "...", "stars": 4, "address": "...",
        "city": "Lisbon", "country": "PT", "rating": 9.1 }
    """
    room_types = entry.get("roomTypes") or []
    rates = []
    for rt in room_types:
        rates.extend(rt.get("rates") or [])
    if not rates:
        raise ValueError("no rates")

    # Pick the cheapest rate.
    def _rate_total(r: dict) -> float:
        retail = r.get("retailRate", {}).get("total") or []
        if not retail:
            return float("inf")
        return float(retail[0].get("amount") or 0)

    rates.sort(key=_rate_total)
    cheapest = rates[0]
    total = _rate_total(cheapest)
    currency = (
        cheapest.get("retailRate", {}).get("total", [{}])[0].get("currency")
        or "USD"
    )
    nightly = total / nights if nights else total

    cancel_info = cheapest.get("cancellationPolicies") or {}
    refundable_tag = cancel_info.get("refundableTag", "")
    refundable_until = None
    if refundable_tag in ("RFN", "REFUNDABLE", "FREE_CANCELLATION"):
        infos = cancel_info.get("cancelPolicyInfos") or []
        if infos:
            refundable_until = infos[0].get("cancelTime")

    star_raw = hotel_meta.get("stars") or hotel_meta.get("starRating")
    star_rating = float(star_raw) if star_raw is not None else 3.5

    address_obj = hotel_meta.get("address")
    if isinstance(address_obj, dict):
        neighborhood = (
            address_obj.get("neighborhood")
            or address_obj.get("city")
            or hotel_meta.get("city")
            or dest
        )
    else:
        neighborhood = hotel_meta.get("city") or dest

    return HotelOffer(
        id=f"LITE-{entry.get('hotelId', 'unknown')}",
        provider="liteapi",
        name=hotel_meta.get("name", "Unknown Hotel"),
        neighborhood=neighborhood,
        check_in="",   # echoed back via the rates request, not always in response
        check_out="",
        nights=nights,
        nightly_rate_cents=int(round(nightly * 100)),
        total_price_cents=int(round(total * 100)),
        currency=currency,
        star_rating=star_rating,
        refundable_until=refundable_until,
        review_signals={},
        public_review_url=None,
    )
