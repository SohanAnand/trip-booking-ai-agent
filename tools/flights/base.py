"""FlightProvider protocol — Mock and Amadeus impls implement this."""

from __future__ import annotations

from typing import Protocol

from api.schemas import FlightOffer


class FlightProvider(Protocol):
    name: str

    async def search(
        self,
        *,
        origin: str,
        destination: str,
        date_start: str,
        date_end: str,
        traveler_count: int,
        max_price_cents: int | None,
    ) -> list[FlightOffer]: ...
