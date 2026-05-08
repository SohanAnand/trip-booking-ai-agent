"""Booking state machine. Distinct from the agent state machine in agent/state.py."""

from __future__ import annotations

from enum import Enum


class BookingState(str, Enum):
    PROPOSED = "PROPOSED"
    HELD = "HELD"
    AUTHORIZED = "AUTHORIZED"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"
    FAILED = "FAILED"


class CapabilityTier(str, Enum):
    A = "A"   # true hold + confirm
    B = "B"   # quote-only (price-locked, no inventory hold)
    C = "C"   # fire-and-forget atomic charge


LEGAL_BOOKING_TRANSITIONS: dict[BookingState, set[BookingState]] = {
    BookingState.PROPOSED: {BookingState.HELD, BookingState.FAILED},
    BookingState.HELD: {BookingState.AUTHORIZED, BookingState.FAILED, BookingState.COMPENSATING},
    BookingState.AUTHORIZED: {BookingState.COMMITTING, BookingState.FAILED},
    BookingState.COMMITTING: {BookingState.COMMITTED, BookingState.COMPENSATING, BookingState.FAILED},
    BookingState.COMMITTED: set(),
    BookingState.COMPENSATING: {BookingState.COMPENSATED, BookingState.FAILED},
    BookingState.COMPENSATED: set(),
    BookingState.FAILED: set(),
}


class IllegalBookingTransition(Exception):
    pass


def assert_legal(current: BookingState, target: BookingState) -> None:
    if target not in LEGAL_BOOKING_TRANSITIONS.get(current, set()):
        raise IllegalBookingTransition(f"{current} -> {target} not permitted")
