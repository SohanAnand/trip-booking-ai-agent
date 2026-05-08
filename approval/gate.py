"""Two-step approval gate.

Step 1 — select(): user picks one of the three options. The gate runs revalidation,
returns a FinalSummary with consent text and any drift diffs. Drift exceeding the
replan threshold transitions the agent back to PRESENTING.

Step 2 — authorize(): user re-confirms (M1: 6-digit out-of-band code; M4+: WebAuthn).
On success, the gate mints an Ed25519 ApprovalToken bound to the option_hash and
returns it. The booking handler then verifies the token and runs two_phase commit.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

from api.config import settings
from api.schemas import FinalSummary, ItineraryOption
from approval.revalidator import DriftReport, revalidate
from approval.tokens import ApprovalToken, mint_token
from audit.log import AuditLog, jcs_canonical, sha256_hex


CODE_TTL_SECONDS = 300       # 5 minutes
TOKEN_TTL_SECONDS = 90       # 90 seconds
MAX_OTP_FAILURES = 5         # consume the pending row after this many bad codes


@dataclass
class PendingAuthorization:
    code: str             # 6-digit OTP (M1 substitute for WebAuthn)
    issued_at: int
    user_id: str
    option_id: str
    option_hash: str
    consent_text: str
    consent_text_hash: str
    payment_method_id: str
    request_id: str
    amount_cents: int
    currency: str


class ApprovalGate:
    """Pending authorizations are persisted to SQLite so they survive CLI invocations.

    M4 will swap SQLite for Redis to support multi-worker pending state.
    """

    def __init__(self, log: AuditLog) -> None:
        self.log = log
        self._lock = threading.Lock()

    # ------------------------------------------------------------------------
    def select(
        self,
        *,
        request_id: str,
        user_id: str,
        option: ItineraryOption,
        current_option: ItineraryOption | None = None,
        payment_method_id: str = "pm_demo_visa",
    ) -> tuple[FinalSummary, str, DriftReport]:
        """Run revalidation and prepare an authorization challenge.

        Returns (FinalSummary, otp_code, drift_report). In production, the OTP
        would be sent via SMS, not returned. For M1, it's returned so the CLI
        can display it.
        """
        current = current_option or option   # in M1 there is no live mutation
        drift = revalidate(option, current)

        # Re-fetch the canonical snapshot from the audit log (signed once)
        snap = self.log.get_option_snapshot(option.id)
        if not snap:
            raise ValueError(f"option {option.id} not found in audit log")
        option_hash = snap["snapshot_hash"]

        consent_text = _build_consent_text(current)
        amount_cents = current.total_price_cents.value
        currency = current.currency.value

        otp = f"{secrets.randbelow(1_000_000):06d}"
        consent_hash = sha256_hex(consent_text)
        self.log.store_pending_authorization({
            "option_id": option.id,
            "code": otp,
            "issued_at": int(time.time()),
            "user_id": user_id,
            "option_hash": option_hash,
            "consent_text": consent_text,
            "consent_text_hash": consent_hash,
            "payment_method_id": payment_method_id,
            "request_id": request_id,
            "amount_cents": amount_cents,
            "currency": currency,
        })

        self.log.append("approval-service", "approval.selection", {
            "option_id": option.id, "user_id": user_id,
            "drift_detected": drift.has_drift,
            "requires_replan": drift.requires_replan,
            "diffs": drift.diffs,
        }, request_id=request_id)

        summary = FinalSummary(
            request_id=request_id, option=current,
            consent_text=consent_text,
            drift_detected=drift.has_drift,
            drift_diffs=drift.diffs,
            cancellation_policy=_describe_cancellation(current),
            total_price_display=f"${amount_cents/100:,.2f} {currency}",
            payment_method_id=payment_method_id,
        )
        return summary, otp, drift

    # ------------------------------------------------------------------------
    def authorize(
        self,
        *,
        option_id: str,
        otp: str,
    ) -> ApprovalToken:
        """Verify OTP, mint Ed25519 ApprovalToken bound to the option.

        Pending row is single-use: read-and-delete is atomic. If the OTP is
        wrong, we re-insert so the user can try again until the code expires.
        """
        # Peek first so we can audit-log per failure mode
        pending = self.log.peek_pending_authorization(option_id)
        if not pending:
            self.log.append("approval-service", "approval.no_pending", {
                "option_id": option_id,
            }, request_id=None)
            raise AuthorizationFailed("no pending authorization for that option")

        now = int(time.time())
        if now - pending["issued_at"] > CODE_TTL_SECONDS:
            self.log.take_pending_authorization(option_id)
            self.log.append("approval-service", "approval.code_expired", {
                "option_id": option_id,
            }, request_id=pending["request_id"])
            raise AuthorizationFailed("authorization code expired")

        # Atomic check-and-consume closes the peek/compare/take race window.
        # Either we get the row back (consumed), or the OTP didn't match and
        # the row stays put for failure counting + retry.
        consumed = self.log.consume_pending_if_match(option_id, otp)
        if consumed is None:
            new_failure_count = self.log.bump_pending_failure(option_id)
            self.log.append("approval-service", "approval.bad_code", {
                "option_id": option_id,
                "failure_count": new_failure_count,
            }, request_id=pending["request_id"])
            if new_failure_count >= MAX_OTP_FAILURES:
                self.log.take_pending_authorization(option_id)
                self.log.append("approval-service", "approval.locked_out", {
                    "option_id": option_id,
                    "failures": new_failure_count,
                }, request_id=pending["request_id"])
                raise AuthorizationFailed(
                    f"authorization locked out after {new_failure_count} "
                    "failed attempts; request a new option"
                )
            raise AuthorizationFailed("incorrect authorization code")

        token = mint_token(
            user_id=consumed["user_id"],
            request_id=consumed["request_id"],
            option_id=consumed["option_id"],
            option_hash=consumed["option_hash"],
            amount_value=f"{consumed['amount_cents'] / 100:.2f}",
            amount_currency=consumed["currency"],
            payment_method_id=consumed["payment_method_id"],
            user_consent_text=consumed["consent_text"],
            ttl_seconds=TOKEN_TTL_SECONDS,
        )

        self.log.append("approval-service", "approval.signed", {
            "option_id": consumed["option_id"],
            "jti": token.payload.jti,
            "exp": token.payload.exp,
            "amount_cents": consumed["amount_cents"],
            "user_consent_text_hash": consumed["consent_text_hash"],
        }, request_id=consumed["request_id"])
        return token


class AuthorizationFailed(Exception):
    pass


def _build_consent_text(option: ItineraryOption) -> str:
    total = f"${option.total_price_cents.value/100:,.2f} {option.currency.value}"

    if len(option.legs) > 1:
        parts: list[str] = []
        for L in option.legs:
            f = L.flight.value
            h = L.hotel.value
            parts.append(
                f"flight {f.outbound[0].origin} to {f.outbound[-1].destination} "
                f"on {f.outbound[0].depart[:10]} plus hotel {h.name} "
                f"({h.neighborhood}) for {h.nights} nights"
            )
        if option.return_flight is not None:
            rf = option.return_flight.value
            parts.append(
                f"return {rf.outbound[0].origin} to {rf.outbound[-1].destination} "
                f"on {rf.outbound[0].depart[:10]}"
            )
        return f"I authorize charging my payment method {total} for: " + "; ".join(parts) + "."

    flight = option.flight.value
    hotel = option.hotel.value
    # Return flight lives in option.return_flight after the one-way refactor;
    # flight.inbound is always empty for per-leg flights now. Absence is
    # fine for genuinely one-way trips.
    if option.return_flight is not None:
        rf = option.return_flight.value
        return_str = (
            f" (return {rf.outbound[0].origin} to "
            f"{rf.outbound[-1].destination} on {rf.outbound[0].depart[:10]})"
        )
    else:
        return_str = " (one-way, no return flight)"
    return (
        f"I authorize charging my payment method {total} for: "
        f"flights {flight.outbound[0].origin} to {flight.outbound[-1].destination} "
        f"on {flight.outbound[0].depart[:10]}{return_str}; "
        f"hotel {hotel.name} ({hotel.neighborhood}) "
        f"check-in {hotel.check_in} check-out {hotel.check_out} "
        f"({hotel.nights} nights, {hotel.star_rating}-star)."
    )


def _describe_cancellation(option: ItineraryOption) -> str:
    parts: list[str] = []

    if len(option.legs) > 1:
        for L in option.legs:
            f = L.flight.value
            f_refundable = all(s.refundable for s in f.outbound)
            h = L.hotel.value
            cancel = (
                f"Leg {L.leg_index + 1} {L.destination}: "
                f"flight {'refundable' if f_refundable else 'non-refundable'}, "
                f"hotel "
                + (f"free cancel until {h.refundable_until}" if h.refundable_until
                   else "non-refundable")
            )
            parts.append(cancel)
        return " | ".join(parts)

    f = option.flight.value
    # Per-leg flights are one-way (inbound always empty); the return flight is
    # in option.return_flight. Refundability must consider both.
    flight_segs = list(f.outbound)
    if option.return_flight is not None:
        flight_segs += list(option.return_flight.value.outbound)
    parts.append(
        "Flights: refundable" if flight_segs and all(s.refundable for s in flight_segs)
        else "Flights: non-refundable"
    )
    h = option.hotel.value
    parts.append(
        f"Hotel: free cancellation until {h.refundable_until}"
        if h.refundable_until else "Hotel: non-refundable"
    )
    return " | ".join(parts)
