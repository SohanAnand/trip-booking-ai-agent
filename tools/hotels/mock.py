"""Deterministic hotel fixtures.

Three Lisbon options chosen to span the 'cheapest / best-reviewed / alternative'
tradeoff axes the orchestrator looks for.
"""

from __future__ import annotations

from datetime import date

from api.schemas import HotelOffer


def _nights_between(check_in: str, check_out: str) -> int:
    a = date.fromisoformat(check_in)
    b = date.fromisoformat(check_out)
    return max((b - a).days, 1)


class MockHotelProvider:
    name = "mock"

    async def search(
        self,
        *,
        destination: str,
        check_in: str,
        check_out: str,
        traveler_count: int,
        max_nightly_cents: int | None,
    ) -> list[HotelOffer]:
        nights = _nights_between(check_in, check_out)
        offers = [
            HotelOffer(
                id="MOCK-HT-A",
                provider="mock",
                name="Hotel Alfama Charm",
                neighborhood="Alfama",
                check_in=check_in,
                check_out=check_out,
                nights=nights,
                nightly_rate_cents=14500,
                total_price_cents=14500 * nights,
                currency="USD",
                star_rating=4.0,
                refundable_until=f"{check_in}T00:00:00Z",
                review_signals={
                    "noise": "moderate (cobblestones at night)",
                    "cleanliness": "high",
                    "staff": "warm",
                    "location_accuracy": "matches description",
                    "hidden_fees": "none mentioned",
                    "summary": "quiet old-town hotel; some street noise on weekend nights",
                },
                public_review_url="https://example.com/reviews/alfama-charm",
            ),
            HotelOffer(
                id="MOCK-HT-B",
                provider="mock",
                name="Lisbon Riverside Boutique",
                neighborhood="Cais do Sodré",
                check_in=check_in,
                check_out=check_out,
                nights=nights,
                nightly_rate_cents=21000,
                total_price_cents=21000 * nights,
                currency="USD",
                star_rating=4.5,
                refundable_until=f"{check_in}T00:00:00Z",
                review_signals={
                    "noise": "very quiet rooms despite central location",
                    "cleanliness": "very high",
                    "staff": "exceptional",
                    "location_accuracy": "matches description",
                    "hidden_fees": "none mentioned",
                    "summary": "walkable to Bairro Alto; rooftop highlight",
                },
                public_review_url="https://example.com/reviews/lisbon-riverside",
            ),
            HotelOffer(
                id="MOCK-HT-C",
                provider="mock",
                name="Belém Garden Suites",
                neighborhood="Belém",
                check_in=check_in,
                check_out=check_out,
                nights=nights,
                nightly_rate_cents=11000,
                total_price_cents=11000 * nights,
                currency="USD",
                star_rating=3.5,
                refundable_until=None,
                review_signals={
                    "noise": "quiet, residential",
                    "cleanliness": "good",
                    "staff": "polite, slow check-in",
                    "location_accuracy": "further from center than photos suggest",
                    "hidden_fees": "city tax not included in nightly rate",
                    "summary": "great value; trade-off is 25-min tram to old town",
                },
                public_review_url="https://example.com/reviews/belem-garden",
            ),
        ]
        if max_nightly_cents is not None:
            offers = [o for o in offers if o.nightly_rate_cents <= max_nightly_cents]
        return offers
