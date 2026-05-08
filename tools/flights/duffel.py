"""Duffel flight search adapter.

Endpoint: POST https://api.duffel.com/air/offer_requests?return_offers=true
Docs: https://duffel.com/docs/api/v2/offer-requests

Auth: Bearer DUFFEL_ACCESS_TOKEN (use duffel_test_... for sandbox).
Test mode is enabled by the token prefix; no separate base URL.

Test cards / sandbox ergonomics:
  - Realistic offer prices, real carrier codes (TP, UA, LH, ...).
  - "test" token returns ~30 offers per round-trip query within 2-5s.

We do NOT create orders here — booking is mocked in this prototype.
"""

from __future__ import annotations

from typing import Any

import httpx

from api.schemas import FlightOffer, FlightSegment


DUFFEL_API = "https://api.duffel.com"


def _settings():
    """Re-resolve settings on every call so tests that monkeypatch env can rebind."""
    from api.config import settings
    return settings


class DuffelFlightProvider:
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
            raise RuntimeError(
                "DUFFEL_ACCESS_TOKEN not set. "
                "Get a test token at https://app.duffel.com → Developers."
            )
        return {
            "Authorization": f"Bearer {s.duffel_access_token}",
            "Duffel-Version": s.duffel_api_version,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

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
        # Duffel accepts: economy, premium_economy, business, first.
        valid_cabins = {"economy", "premium_economy", "business", "first"}
        cabin = cabin_class if cabin_class in valid_cabins else "economy"
        slices = [
            {"origin": origin, "destination": destination,
             "departure_date": date_start},
        ]
        if not one_way:
            slices.append(
                {"origin": destination, "destination": origin,
                 "departure_date": date_end},
            )
        body = {
            "data": {
                "slices": slices,
                "passengers": [{"type": "adult"} for _ in range(max(traveler_count, 1))],
                "cabin_class": cabin,
            }
        }
        cli = await self._http()
        # `return_offers=true` makes the request synchronous (offers in same response).
        # `supplier_timeout` caps how long Duffel waits on suppliers.
        res = await cli.post(
            f"{DUFFEL_API}/air/offer_requests?return_offers=true&supplier_timeout=10000",
            headers=self._headers(),
            json=body,
        )
        if res.status_code == 401:
            raise RuntimeError("Duffel rejected token (check DUFFEL_ACCESS_TOKEN)")
        if res.status_code >= 400:
            return []
        payload = res.json().get("data", {})
        offers = payload.get("offers", [])
        out: list[FlightOffer] = []
        for o in offers:
            try:
                offer = _map_offer(o)
            except Exception:
                continue
            if max_price_cents and offer.total_price_cents > max_price_cents:
                continue
            out.append(offer)
        out.sort(key=lambda f: f.total_price_cents)
        return out[:10]


# ----- mapping -------------------------------------------------------------

def _map_offer(o: dict[str, Any]) -> FlightOffer:
    """Map a Duffel offer payload to our FlightOffer schema.

    Duffel offer shape (abbreviated):
      {
        "id": "off_...",
        "total_amount": "684.00",   # STRING in major units
        "total_currency": "USD",
        "owner": {"iata_code": "TP"},
        "slices": [{ "segments": [{ ... }, ...] }, ...],   # outbound, then inbound
        "conditions": {"refund_before_departure": {"allowed": false, "penalty_amount": "..."}},
        "passenger_identity_documents_required": false
      }

    Each segment has:
      origin.iata_code / destination.iata_code
      departing_at / arriving_at (ISO datetimes)
      duration (ISO 8601, e.g. "PT6H45M")
      marketing_carrier.iata_code / marketing_carrier_flight_number
      passengers[].cabin_class (e.g. "economy")
      passengers[].baggages[] = [{"type": "checked", "quantity": 1}, ...]
    """
    slices = o.get("slices", [])
    outbound = [_map_segment(s) for s in slices[0].get("segments", [])] if slices else []
    inbound = [_map_segment(s) for s in slices[1].get("segments", [])] if len(slices) > 1 else []

    total = float(o.get("total_amount", 0))
    currency = o.get("total_currency", "USD")

    cond = o.get("conditions") or {}
    refund_before_dep = (cond.get("refund_before_departure") or {})
    refundable = bool(refund_before_dep.get("allowed", False))
    for seg in outbound + inbound:
        seg.refundable = refundable

    # Baggage: any segment that includes checked bags counts as baggage_included.
    baggage_included = False
    for sl in slices:
        for seg in sl.get("segments", []):
            for px in seg.get("passengers", []):
                for bag in px.get("baggages", []):
                    if bag.get("type") == "checked" and bag.get("quantity", 0) > 0:
                        baggage_included = True
                        break

    return FlightOffer(
        id=f"DFL-{o.get('id', 'unknown')}",
        provider="duffel",
        outbound=outbound,
        inbound=inbound,
        total_price_cents=int(round(total * 100)),
        currency=currency,
        baggage_included=baggage_included,
    )


def _map_segment(s: dict[str, Any]) -> FlightSegment:
    duration = s.get("duration", "PT0M")
    minutes = _parse_iso_duration_minutes(duration)
    px = (s.get("passengers") or [{}])[0]
    return FlightSegment(
        carrier=(s.get("marketing_carrier") or {}).get("iata_code", "??"),
        flight_number=str(s.get("marketing_carrier_flight_number", "")),
        origin=(s.get("origin") or {}).get("iata_code", ""),
        destination=(s.get("destination") or {}).get("iata_code", ""),
        depart=s.get("departing_at", ""),
        arrive=s.get("arriving_at", ""),
        duration_minutes=minutes,
        fare_class=px.get("cabin_class", "economy"),
        refundable=False,    # set per-offer at the call site
    )


def _parse_iso_duration_minutes(d: str) -> int:
    """Parse 'PT4H30M' → 270."""
    d = (d or "").removeprefix("PT")
    h = m = 0
    if "H" in d:
        h_str, rest = d.split("H", 1)
        h = int(h_str)
        d = rest
    if "M" in d:
        m_str, _ = d.split("M", 1)
        m = int(m_str)
    return h * 60 + m
