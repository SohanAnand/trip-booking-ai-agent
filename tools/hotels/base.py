from __future__ import annotations

from typing import Protocol

from api.schemas import HotelOffer


class HotelProvider(Protocol):
    name: str

    async def search(
        self,
        *,
        destination: str,
        check_in: str,
        check_out: str,
        traveler_count: int,
        max_nightly_cents: int | None,
    ) -> list[HotelOffer]: ...
