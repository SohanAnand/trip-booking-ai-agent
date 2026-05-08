"""Standalone helper for unwinding a partially-committed booking.

The two_phase.execute_booking() function compensates inline; this module
exposes the same logic for out-of-band remediation (e.g., M5 anomaly path
that freezes a booking and runs the compensator from a CS tool).
"""

from __future__ import annotations

from booking.providers.base import PaymentProvider
from booking.two_phase import _compensate as _internal_compensate
from audit.log import AuditLog


async def compensate_booking(
    *,
    captured_legs: list,
    held_legs: list,
    failed_leg,
    log: AuditLog,
    request_id: str,
    booking_id: str,
) -> None:
    await _internal_compensate(captured_legs, held_legs, failed_leg, log,
                               request_id, booking_id)
