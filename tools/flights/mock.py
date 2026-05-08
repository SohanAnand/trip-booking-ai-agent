"""Deterministic flight fixtures for M1 demo and tests.

Returns 3 candidates for the canonical "Lisbon" demo prompt and reasonable
defaults for other destinations. Prices are stable so tests can pin them.
"""

from __future__ import annotations

from api.schemas import FlightOffer, FlightSegment


class MockFlightProvider:
    name = "mock"

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
        # One-way bundles cost ~55% of round-trip on real providers; we mirror
        # that ratio so totals stay realistic when the agent assembles
        # outbound + return separately.
        rt_inbound_001 = [FlightSegment(
            carrier="TAP", flight_number="TP201",
            origin=destination, destination=origin,
            depart=f"{date_end}T11:00:00Z", arrive=f"{date_end}T14:25:00Z",
            duration_minutes=445, fare_class="Y", refundable=False,
        )]
        rt_inbound_002 = [FlightSegment(
            carrier="UA", flight_number="UA965",
            origin=destination, destination=origin,
            depart=f"{date_end}T13:25:00Z", arrive=f"{date_end}T16:20:00Z",
            duration_minutes=415, fare_class="Y", refundable=False,
        )]
        rt_inbound_003 = [
            FlightSegment(
                carrier="LH", flight_number="LH1171",
                origin=destination, destination="FRA",
                depart=f"{date_end}T12:25:00Z", arrive=f"{date_end}T16:30:00Z",
                duration_minutes=185, fare_class="Y", refundable=True,
            ),
            FlightSegment(
                carrier="LH", flight_number="LH400",
                origin="FRA", destination=origin,
                depart=f"{date_end}T18:00:00Z", arrive=f"{date_end}T20:55:00Z",
                duration_minutes=475, fare_class="Y", refundable=True,
            ),
        ]
        offers = [
            FlightOffer(
                id="MOCK-FL-001",
                provider="mock",
                outbound=[
                    FlightSegment(
                        carrier="TAP", flight_number="TP202",
                        origin=origin, destination=destination,
                        depart=f"{date_start}T18:30:00Z", arrive=f"{date_start}T08:15:00Z",
                        duration_minutes=405, fare_class="Y", refundable=False,
                    )
                ],
                inbound=[] if one_way else rt_inbound_001,
                total_price_cents=37620 if one_way else 68400,
                currency="USD",
                baggage_included=True,
            ),
            FlightOffer(
                id="MOCK-FL-002",
                provider="mock",
                outbound=[
                    FlightSegment(
                        carrier="UA", flight_number="UA964",
                        origin=origin, destination=destination,
                        depart=f"{date_start}T20:50:00Z", arrive=f"{date_start}T10:40:00Z",
                        duration_minutes=410, fare_class="Y", refundable=False,
                    )
                ],
                inbound=[] if one_way else rt_inbound_002,
                total_price_cents=40095 if one_way else 72900,
                currency="USD",
                baggage_included=False,
            ),
            FlightOffer(
                id="MOCK-FL-003",
                provider="mock",
                outbound=[
                    FlightSegment(
                        carrier="LH", flight_number="LH401",
                        origin=origin, destination="FRA",
                        depart=f"{date_start}T17:50:00Z", arrive=f"{date_start}T07:30:00Z",
                        duration_minutes=400, fare_class="Y", refundable=True,
                    ),
                    FlightSegment(
                        carrier="LH", flight_number="LH1170",
                        origin="FRA", destination=destination,
                        depart=f"{date_start}T09:30:00Z", arrive=f"{date_start}T11:35:00Z",
                        duration_minutes=185, fare_class="Y", refundable=True,
                    ),
                ],
                inbound=[] if one_way else rt_inbound_003,
                total_price_cents=51975 if one_way else 94500,
                currency="USD",
                baggage_included=True,
            ),
        ]
        if max_price_cents is not None:
            offers = [o for o in offers if o.total_price_cents <= max_price_cents]
        return offers
