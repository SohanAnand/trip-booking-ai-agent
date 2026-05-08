"""OpenWeather 2.5/forecast adapter tests with mocked HTTP."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from tools.weather.openweather import OpenWeatherProvider


@pytest.mark.asyncio
async def test_openweather_aggregates_window(monkeypatch):
    monkeypatch.setenv("OPENWEATHER_API_KEY", "demo")
    import api.config; import importlib; importlib.reload(api.config)

    # 2.5/forecast returns 5 days × 8 three-hour buckets under `list[]`.
    base_dt = datetime(2026, 6, 6, 0, 0, tzinfo=timezone.utc)
    buckets = []
    for day in range(5):
        for hour_slot in range(8):
            ts = int(base_dt.timestamp()) + (day * 86400) + (hour_slot * 10800)
            buckets.append({
                "dt": ts,
                "main": {
                    "temp": 18.0 + day * 0.5,
                    "temp_max": 22.0 + day,
                    "temp_min": 14.0 + (day % 2),
                },
                "weather": [{"description": "broken clouds", "main": "Clouds"}],
                "pop": 0.10 + 0.05 * day,
            })

    def handler(req: httpx.Request) -> httpx.Response:
        if "geo/1.0" in req.url.path:
            return httpx.Response(200, json=[{"lat": 38.7, "lon": -9.1}])
        if "data/2.5/forecast" in req.url.path:
            return httpx.Response(200, json={"list": buckets})
        return httpx.Response(404)

    cli = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenWeatherProvider(client=cli)
    summary = await provider.forecast(
        location="LIS",
        window_start="2026-06-06", window_end="2026-06-10",
    )
    assert summary.location == "LIS"
    assert 22 <= summary.avg_high_c <= 27
    assert 0.10 <= summary.rain_probability <= 0.35
    assert "broken clouds" in summary.summary


@pytest.mark.asyncio
async def test_openweather_window_outside_horizon_falls_back(monkeypatch):
    """Trip is months in the future — return whatever forecast we have so the
    agent still produces a useful narrative rather than failing."""
    monkeypatch.setenv("OPENWEATHER_API_KEY", "demo")
    import api.config; import importlib; importlib.reload(api.config)

    base_dt = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    buckets = [{
        "dt": int(base_dt.timestamp()) + i * 10800,
        "main": {"temp": 10, "temp_max": 13, "temp_min": 7},
        "weather": [{"description": "scattered clouds"}],
        "pop": 0.1,
    } for i in range(40)]

    cli = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=[{"lat": 38.7, "lon": -9.1}])
                    if "geo/1.0" in req.url.path
                    else httpx.Response(200, json={"list": buckets})
    ))
    provider = OpenWeatherProvider(client=cli)
    summary = await provider.forecast(
        location="LIS", window_start="2099-06-06", window_end="2099-06-10",
    )
    # Far-future window has no overlap with the 5-day forecast → fall back to
    # whatever buckets are available, never raise.
    assert summary.summary != "no forecast available for this window"
    assert summary.avg_high_c > 0
