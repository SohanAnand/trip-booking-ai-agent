"""OpenWeather forecast adapter.

Two-step flow:
  1. /geo/1.0/direct?q=<location> → (lat, lon)
  2. /data/2.5/forecast?lat=..&lon=..&units=metric → 5-day / 3-hour forecast.

We aggregate buckets that intersect the trip window into a single daily summary.

Why 2.5/forecast and not 3.0/onecall:
  data/3.0/onecall requires a paid "One Call by Call" subscription on
  OpenWeather (their free key 401s on it). The 2.5/forecast endpoint is
  in the free tier, gives 5 days × 8 buckets/day = 40 entries, and is
  sufficient for the qualitative summary the orchestrator surfaces.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import httpx

from api.schemas import WeatherSummary
from tools.flights.amadeus import _settings    # shared lazy-settings helper

GEO_URL = "https://api.openweathermap.org/geo/1.0/direct"
FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"


# Fallback geocoding for the demo cities so the M2 demo runs even if Geocoding
# returns nothing for short codes like "LIS".
GEO_FALLBACK = {
    # Iberia
    "LIS": (38.7223, -9.1393),     "LISBON": (38.7223, -9.1393),
    "OPO": (41.1579, -8.6291),     "PORTO": (41.1579, -8.6291),
    "MAD": (40.4168, -3.7038),     "MADRID": (40.4168, -3.7038),
    "BCN": (41.3874, 2.1686),      "BARCELONA": (41.3874, 2.1686),
    # Western/Central Europe
    "PAR": (48.8566, 2.3522),      "PARIS": (48.8566, 2.3522),
    "LON": (51.5074, -0.1278),     "LONDON": (51.5074, -0.1278),
    "AMS": (52.3676, 4.9041),      "AMSTERDAM": (52.3676, 4.9041),
    "BER": (52.5200, 13.4050),     "BERLIN": (52.5200, 13.4050),
    "MUC": (48.1351, 11.5820),     "MUNICH": (48.1351, 11.5820),
    "VIE": (48.2082, 16.3738),     "VIENNA": (48.2082, 16.3738),
    "PRG": (50.0755, 14.4378),     "PRAGUE": (50.0755, 14.4378),
    # Southern Europe
    "ROM": (41.9028, 12.4964),     "ROME": (41.9028, 12.4964),
    "MIL": (45.4642, 9.1900),      "MILAN": (45.4642, 9.1900),
    "ATH": (37.9838, 23.7275),     "ATHENS": (37.9838, 23.7275),
    "IST": (41.0082, 28.9784),     "ISTANBUL": (41.0082, 28.9784),
    # Middle East / Asia / Pacific
    "DXB": (25.2048, 55.2708),     "DUBAI": (25.2048, 55.2708),
    "TYO": (35.6762, 139.6503),    "TOKYO": (35.6762, 139.6503),
    "OSA": (34.6937, 135.5023),    "OSAKA": (34.6937, 135.5023),
    "SEL": (37.5665, 126.9780),    "SEOUL": (37.5665, 126.9780),
    "BKK": (13.7563, 100.5018),    "BANGKOK": (13.7563, 100.5018),
    "SIN": (1.3521, 103.8198),     "SINGAPORE": (1.3521, 103.8198),
    "HKG": (22.3193, 114.1694),    "HONG KONG": (22.3193, 114.1694),
    "SYD": (-33.8688, 151.2093),   "SYDNEY": (-33.8688, 151.2093),
    "MEL": (-37.8136, 144.9631),   "MELBOURNE": (-37.8136, 144.9631),
    # Americas
    "NYC": (40.7128, -74.0060),    "NEW YORK": (40.7128, -74.0060),
    "LAX": (34.0522, -118.2437),   "LOS ANGELES": (34.0522, -118.2437),
    "SFO": (37.7749, -122.4194),   "SAN FRANCISCO": (37.7749, -122.4194),
    "CHI": (41.8781, -87.6298),    "CHICAGO": (41.8781, -87.6298),
    "MIA": (25.7617, -80.1918),    "MIAMI": (25.7617, -80.1918),
    "BOS": (42.3601, -71.0589),    "BOSTON": (42.3601, -71.0589),
    "YTO": (43.6532, -79.3832),    "TORONTO": (43.6532, -79.3832),
    "YVR": (49.2827, -123.1207),   "VANCOUVER": (49.2827, -123.1207),
    "MEX": (19.4326, -99.1332),    "MEXICO CITY": (19.4326, -99.1332),
}


class OpenWeatherProvider:
    name = "openweather"

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def _geocode(self, location: str) -> tuple[float, float]:
        s = _settings()
        if not s.openweather_api_key:
            raise RuntimeError("OPENWEATHER_API_KEY not set")
        upper = location.upper()
        if upper in GEO_FALLBACK:
            return GEO_FALLBACK[upper]
        cli = await self._http()
        res = await cli.get(GEO_URL, params={
            "q": location, "limit": 1, "appid": s.openweather_api_key,
        })
        res.raise_for_status()
        data = res.json()
        if not data:
            raise RuntimeError(f"could not geocode {location!r}")
        return float(data[0]["lat"]), float(data[0]["lon"])

    async def forecast(
        self,
        *,
        location: str,
        window_start: str,
        window_end: str,
    ) -> WeatherSummary:
        lat, lon = await self._geocode(location)
        cli = await self._http()
        res = await cli.get(FORECAST_URL, params={
            "lat": lat, "lon": lon,
            "units": "metric",
            "appid": _settings().openweather_api_key,
        })
        res.raise_for_status()
        # 2.5/forecast returns 5 days × 8 three-hour buckets under `list[]`.
        # Each entry: {dt, main: {temp_max, temp_min}, weather: [{description}], pop}
        buckets = res.json().get("list", [])

        start = date.fromisoformat(window_start)
        end = date.fromisoformat(window_end)

        # Group buckets by date and aggregate. Daily high = max(temp_max),
        # daily low = min(temp_min), pop = mean(pop), description = mode.
        per_day: dict[date, dict] = {}
        for b in buckets:
            d = datetime.fromtimestamp(b["dt"], tz=timezone.utc).date()
            if d < start or d > end:
                continue
            slot = per_day.setdefault(d, {"high": [], "low": [], "pop": [], "desc": []})
            main = b.get("main", {})
            slot["high"].append(main.get("temp_max", main.get("temp", 0.0)))
            slot["low"].append(main.get("temp_min", main.get("temp", 0.0)))
            slot["pop"].append(b.get("pop", 0.0))
            wx = (b.get("weather") or [{}])[0].get("description", "")
            if wx:
                slot["desc"].append(wx)

        if not per_day:
            # If the trip window is past the 5-day forecast horizon, summarize
            # whatever we have so the agent still produces a useful narrative.
            for b in buckets[: min(8 * 3, len(buckets))]:
                d = datetime.fromtimestamp(b["dt"], tz=timezone.utc).date()
                slot = per_day.setdefault(d, {"high": [], "low": [], "pop": [], "desc": []})
                main = b.get("main", {})
                slot["high"].append(main.get("temp_max", main.get("temp", 0.0)))
                slot["low"].append(main.get("temp_min", main.get("temp", 0.0)))
                slot["pop"].append(b.get("pop", 0.0))
                wx = (b.get("weather") or [{}])[0].get("description", "")
                if wx:
                    slot["desc"].append(wx)

        if not per_day:
            return WeatherSummary(
                location=location, window_start=window_start, window_end=window_end,
                summary="no forecast available for this window",
                avg_high_c=0.0, avg_low_c=0.0, rain_probability=0.0,
            )

        days = list(per_day.values())
        avg_high = sum(max(d["high"]) for d in days) / len(days)
        avg_low = sum(min(d["low"]) for d in days) / len(days)
        rain_prob = sum(sum(d["pop"]) / max(len(d["pop"]), 1) for d in days) / len(days)
        all_desc: list[str] = []
        for d in days:
            all_desc.extend(d["desc"])
        summary = _summarize(all_desc, rain_prob)

        return WeatherSummary(
            location=location,
            window_start=window_start,
            window_end=window_end,
            summary=summary,
            avg_high_c=round(avg_high, 1),
            avg_low_c=round(avg_low, 1),
            rain_probability=round(rain_prob, 2),
        )


def _summarize(descriptions: list[str], rain_prob: float) -> str:
    if not descriptions:
        return "no forecast available"
    most_common = max(set(descriptions), key=descriptions.count)
    qualifier = (
        "mostly dry" if rain_prob < 0.2
        else "some chance of rain" if rain_prob < 0.5
        else "rain likely"
    )
    return f"{most_common}; {qualifier}"
