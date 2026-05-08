"""Deterministic weather fixture for M1."""

from __future__ import annotations

from api.schemas import WeatherSummary


class MockWeatherProvider:
    name = "mock"

    async def forecast(self, *, location: str, window_start: str, window_end: str) -> WeatherSummary:
        return WeatherSummary(
            location=location,
            window_start=window_start,
            window_end=window_end,
            summary="mostly sunny, light breeze; one chance of afternoon shower",
            avg_high_c=22.0,
            avg_low_c=14.5,
            rain_probability=0.18,
        )
