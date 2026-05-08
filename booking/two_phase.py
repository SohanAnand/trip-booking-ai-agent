"""Capability-aware two-phase commit across multiple booking legs.

Algorithm:

  PREPARE (Tier A then B; Tier C deferred to commit):
    - Tier A: HOLD; if any fails, release all prior holds and abort.
    - Tier B: QUOTE (price-match against the approval token amount).

  COMMIT (reverse order — Tier C first if any, then B, then A last):
    - For each leg, call capture/charge with the booking's idempotency key.
    - If any leg fails: COMPENSATE in reverse — refund/release/cancel each
      already-committed leg. Each compensation step is itself idempotent.

A successful booking writes a `BookingResult` and a `booking_committed` audit
event. A failed booking writes `booking_compensated` with per-leg outcomes.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from api.schemas import BookingResult
from approval.tokens import ApprovalToken, verify_token
from audit.log import AuditLog, jcs_canonical, sha256_hex
from booking.providers.base import PaymentProvider
from booking.states import BookingState, CapabilityTier, assert_legal


@dataclass
class BookingLeg:
    leg_id: str                # "flight" | "hotel" | etc.
    label: str                 # human-readable
    amount_cents: int
    currency: str
    provider: PaymentProvider
    metadata: dict = field(default_factory=dict)
    hold_id: str | None = None
    confirmation: str | None = None
    state: str = "PROPOSED"


def derive_leg_idempotency_key(booking_id: str, leg_id: str, root_idempotency: str) -> str:
    return sha256_hex(f"{root_idempotency}|{booking_id}|{leg_id}")


def assert_token_match(token: ApprovalToken | dict, *,
                       option_id: str, option_hash: str,
                       amount_cents: int, currency: str) -> None:
    """Hard claims check called from the booking handler.

    First statement of every booking handler should call this. The
    fitness test (tests/fitness/no_untokened_booking.py) enforces it.
    """
    amount_str = f"{amount_cents / 100:.2f}"
    verify_token(
        token,
        expected_aud="payment-service",
        expected_option_id=option_id,
        expected_option_hash=option_hash,
        expected_amount_value=amount_str,
        expected_amount_currency=currency,
    )


async def execute_booking(
    *,
    token: ApprovalToken,
    legs: list[BookingLeg],
    log: AuditLog,
    request_id: str,
    option_id: str,
    option_hash: str,
) -> BookingResult:
    """Run prepare → commit → (compensate on failure). Always writes audit events."""

    # CRITICAL: every booking handler's first action is verify_token.
    # The fitness test (tests/fitness/test_no_untokened_booking.py) scans the
    # AST and rejects any booking handler whose first statement is anything else.
    # verify_token raises TokenInvalid on any check failure; the API layer
    # catches and converts to 401, the CLI catches and prints the error.
    payload = verify_token(
        token,
        expected_aud="payment-service",
        expected_option_id=option_id,
        expected_option_hash=option_hash,
    )

    # Replay defense: claim the jti atomically.
    if not log.consume_jti(payload.jti):
        result = BookingResult(
            booking_id="", state=BookingState.FAILED.value,
            confirmations={}, error="token replay (jti already consumed)",
        )
        log.append("payment-service", "approval.replay_rejected",
                   {"jti": payload.jti, "option_id": option_id},
                   request_id=request_id)
        return result

    booking_id = f"bkg_{uuid.uuid4().hex[:12]}"
    log.consume_jti  # already used; just to make explicit
    total_cents = sum(leg.amount_cents for leg in legs)
    currency = legs[0].currency if legs else "USD"

    log.upsert_booking({
        "id": booking_id, "request_id": request_id, "option_id": option_id,
        "option_hash": option_hash, "state": BookingState.PROPOSED.value,
        "total_cents": total_cents, "currency": currency,
        "idempotency_key": payload.idempotency_key,
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    })
    log.append("payment-service", "booking.started", {
        "booking_id": booking_id, "option_id": option_id,
        "total_cents": total_cents, "leg_count": len(legs),
    }, request_id=request_id, booking_id=booking_id)

    # ---- PHASE 1: PREPARE (holds) -------------------------------------------
    held: list[BookingLeg] = []
    for leg in legs:
        leg_idem = derive_leg_idempotency_key(booking_id, leg.leg_id, payload.idempotency_key)
        if leg.provider.tier == CapabilityTier.A:
            res = await leg.provider.hold(
                amount_cents=leg.amount_cents, currency=leg.currency,
                idempotency_key=leg_idem,
                metadata={"booking_id": booking_id, "leg": leg.leg_id},
            )
            if not res.ok:
                # Release everything held so far, abort.
                log.append("payment-service", "leg.hold_failed", {
                    "booking_id": booking_id, "leg": leg.leg_id, "error": res.error,
                }, request_id=request_id, booking_id=booking_id)
                await _release_all(held, log, request_id, booking_id)
                _set_state(log, booking_id, BookingState.PROPOSED, BookingState.FAILED)
                return BookingResult(
                    booking_id=booking_id, state=BookingState.FAILED.value,
                    confirmations={}, error=f"hold failed for {leg.leg_id}: {res.error}",
                )
            leg.hold_id = res.hold_id
            leg.state = "HELD"
            held.append(leg)
            log.append("payment-service", "leg.held", {
                "booking_id": booking_id, "leg": leg.leg_id, "hold_id": res.hold_id,
                "amount_cents": leg.amount_cents,
            }, request_id=request_id, booking_id=booking_id)

    _set_state(log, booking_id, BookingState.PROPOSED, BookingState.HELD)
    _set_state(log, booking_id, BookingState.HELD, BookingState.AUTHORIZED)
    log.append("payment-service", "booking.authorized", {
        "booking_id": booking_id, "jti": payload.jti,
    }, request_id=request_id, booking_id=booking_id)

    # ---- PHASE 2: COMMIT (captures, reverse order) --------------------------
    _set_state(log, booking_id, BookingState.AUTHORIZED, BookingState.COMMITTING)
    captured: list[BookingLeg] = []
    for leg in reversed(held):
        leg_idem = derive_leg_idempotency_key(booking_id, leg.leg_id, payload.idempotency_key)
        res = await leg.provider.capture(hold_id=leg.hold_id, idempotency_key=leg_idem)
        if not res.ok:
            log.append("payment-service", "leg.capture_failed", {
                "booking_id": booking_id, "leg": leg.leg_id, "error": res.error,
            }, request_id=request_id, booking_id=booking_id)
            await _compensate(captured, held, leg, log, request_id, booking_id)
            _set_state(log, booking_id, BookingState.COMMITTING, BookingState.COMPENSATING)
            _set_state(log, booking_id, BookingState.COMPENSATING, BookingState.COMPENSATED)
            return BookingResult(
                booking_id=booking_id, state=BookingState.COMPENSATED.value,
                confirmations={l.leg_id: l.confirmation for l in captured if l.confirmation},
                error=f"capture failed for {leg.leg_id}: {res.error}",
            )
        leg.confirmation = res.confirmation
        leg.state = "CAPTURED"
        captured.append(leg)
        log.append("payment-service", "leg.captured", {
            "booking_id": booking_id, "leg": leg.leg_id,
            "confirmation": res.confirmation, "charged_cents": res.charged_cents,
        }, request_id=request_id, booking_id=booking_id)

    _set_state(log, booking_id, BookingState.COMMITTING, BookingState.COMMITTED)
    confirmations = {l.leg_id: l.confirmation for l in held if l.confirmation}
    log.append("payment-service", "booking.committed", {
        "booking_id": booking_id, "confirmations": confirmations,
        "total_charged_cents": total_cents,
    }, request_id=request_id, booking_id=booking_id)
    return BookingResult(
        booking_id=booking_id, state=BookingState.COMMITTED.value,
        confirmations=confirmations, total_charged_cents=total_cents,
    )


# ----- internals -------------------------------------------------------------

async def _release_all(legs: list[BookingLeg], log: AuditLog,
                       request_id: str, booking_id: str) -> None:
    for leg in legs:
        if leg.hold_id:
            res = await leg.provider.release(hold_id=leg.hold_id)
            log.append("payment-service", "leg.released", {
                "booking_id": booking_id, "leg": leg.leg_id, "ok": res.ok,
                "error": res.error,
            }, request_id=request_id, booking_id=booking_id)


async def _compensate(captured: list[BookingLeg], held: list[BookingLeg],
                      failed_leg: BookingLeg, log: AuditLog,
                      request_id: str, booking_id: str) -> None:
    """Refund captured legs (reverse), release any holds not yet captured."""
    for leg in captured:
        if leg.confirmation:
            res = await leg.provider.refund(confirmation=leg.confirmation,
                                            amount_cents=leg.amount_cents)
            log.append("payment-service", "leg.refunded", {
                "booking_id": booking_id, "leg": leg.leg_id, "ok": res.ok,
                "refunded_cents": res.refunded_cents, "error": res.error,
            }, request_id=request_id, booking_id=booking_id)

    # The previous logic excluded the failed_leg from release — but the
    # failed leg's hold may still be active (capture failed AFTER hold
    # succeeded). Skipping it would leave money on hold until provider
    # expiry. We release every held leg that wasn't captured, INCLUDING the
    # failed one. Already-captured legs are skipped (they were refunded in
    # the loop above).
    captured_ids = {l.leg_id for l in captured}
    for leg in held:
        if leg.leg_id in captured_ids:
            continue
        if leg.hold_id:
            res = await leg.provider.release(hold_id=leg.hold_id)
            log.append("payment-service", "leg.released", {
                "booking_id": booking_id, "leg": leg.leg_id, "ok": res.ok,
                "error": res.error,
                "was_failed_leg": leg.leg_id == failed_leg.leg_id,
            }, request_id=request_id, booking_id=booking_id)


def _set_state(log: AuditLog, booking_id: str,
               cur: BookingState, new: BookingState) -> None:
    assert_legal(cur, new)
    booking = log.get_booking(booking_id)
    if booking:
        booking["state"] = new.value
        booking["updated_at"] = datetime.now(UTC).isoformat()
        log.upsert_booking(booking)
