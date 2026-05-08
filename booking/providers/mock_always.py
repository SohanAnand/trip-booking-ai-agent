"""Always-succeeds mock provider. Used in M1 happy path."""

from __future__ import annotations

import threading
import time
import uuid

from booking.providers.base import (
    CaptureResult,
    HoldResult,
    PaymentProvider,
    RefundResult,
    ReleaseResult,
)
from booking.states import CapabilityTier


class MockAlwaysProvider:
    """Always succeeds. Idempotent: same (idempotency_key) returns same result."""
    name = "mock_always"
    tier = CapabilityTier.A

    def __init__(self) -> None:
        self._holds: dict[str, dict] = {}
        self._captures: dict[str, dict] = {}     # idempotency_key → result
        self._lock = threading.Lock()

    async def hold(self, *, amount_cents: int, currency: str, idempotency_key: str,
                   metadata: dict) -> HoldResult:
        with self._lock:
            existing = next(
                (h for h in self._holds.values() if h.get("idempotency_key") == idempotency_key),
                None,
            )
            if existing:
                return HoldResult(
                    ok=True, hold_id=existing["hold_id"], expires_at=existing["expires_at"],
                )
            hold_id = f"mock_hold_{uuid.uuid4().hex[:12]}"
            expires_at = str(int(time.time()) + 600)
            self._holds[hold_id] = {
                "hold_id": hold_id,
                "amount_cents": amount_cents,
                "currency": currency,
                "idempotency_key": idempotency_key,
                "metadata": metadata,
                "expires_at": expires_at,
            }
            return HoldResult(ok=True, hold_id=hold_id, expires_at=expires_at)

    async def capture(self, *, hold_id: str, idempotency_key: str) -> CaptureResult:
        with self._lock:
            cached = self._captures.get(idempotency_key)
            if cached:
                return CaptureResult(
                    ok=True,
                    confirmation=cached["confirmation"],
                    charged_cents=cached["charged_cents"],
                )
            hold = self._holds.get(hold_id)
            if not hold:
                return CaptureResult(
                    ok=False, confirmation=None, charged_cents=None,
                    error="hold not found", error_code="HOLD_NOT_FOUND",
                )
            confirmation = f"MOCK-CONF-{uuid.uuid4().hex[:10].upper()}"
            self._captures[idempotency_key] = {
                "confirmation": confirmation, "charged_cents": hold["amount_cents"],
            }
            return CaptureResult(
                ok=True, confirmation=confirmation, charged_cents=hold["amount_cents"],
            )

    async def release(self, *, hold_id: str) -> ReleaseResult:
        with self._lock:
            self._holds.pop(hold_id, None)
        return ReleaseResult(ok=True)

    async def refund(self, *, confirmation: str, amount_cents: int) -> RefundResult:
        return RefundResult(ok=True, refunded_cents=amount_cents)


# Module-level singleton (so the same in-memory hold table persists across calls)
_singleton: MockAlwaysProvider | None = None


def get_provider() -> MockAlwaysProvider:
    global _singleton
    if _singleton is None:
        _singleton = MockAlwaysProvider()
    return _singleton
