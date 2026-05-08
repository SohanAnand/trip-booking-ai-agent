"""Configurable flaky provider for M3 compensation testing.

Drive failures with env var RUN_FAILURE_MODE:
  - hold_fail            : hold() returns error
  - capture_fail         : capture() returns error
  - capture_partial      : flight captures, hotel capture fails (use 2 instances)
  - release_fail         : release() returns error during compensation
  - random_30             : 30% random failure on every op
"""

from __future__ import annotations

import os
import random
import threading
import uuid

from booking.providers.base import (
    CaptureResult,
    HoldResult,
    RefundResult,
    ReleaseResult,
)
from booking.states import CapabilityTier


class MockFlakyProvider:
    name = "mock_flaky"
    tier = CapabilityTier.A

    def __init__(self, leg_label: str = "default") -> None:
        self.leg_label = leg_label
        self._holds: dict = {}
        self._captures: dict = {}
        self._lock = threading.Lock()
        self.mode = os.environ.get("RUN_FAILURE_MODE", "")

    def _should_fail_random(self) -> bool:
        return self.mode == "random_30" and random.random() < 0.30

    async def hold(self, *, amount_cents: int, currency: str, idempotency_key: str,
                   metadata: dict) -> HoldResult:
        if self.mode == "hold_fail" or self._should_fail_random():
            return HoldResult(ok=False, hold_id=None, expires_at=None,
                              error="provider declined hold", error_code="HOLD_DECLINED")
        with self._lock:
            existing = next(
                (h for h in self._holds.values() if h.get("idempotency_key") == idempotency_key),
                None,
            )
            if existing:
                return HoldResult(ok=True, hold_id=existing["hold_id"], expires_at=None)
            hold_id = f"flaky_hold_{uuid.uuid4().hex[:12]}"
            self._holds[hold_id] = {
                "hold_id": hold_id, "idempotency_key": idempotency_key,
                "amount_cents": amount_cents, "currency": currency, "metadata": metadata,
            }
            return HoldResult(ok=True, hold_id=hold_id, expires_at=None)

    async def capture(self, *, hold_id: str, idempotency_key: str) -> CaptureResult:
        # Capture-partial: only the leg labeled "hotel" fails
        if self.mode == "capture_partial" and self.leg_label == "hotel":
            return CaptureResult(ok=False, confirmation=None, charged_cents=None,
                                 error="inventory disappeared", error_code="INVENTORY_GONE")
        if self.mode == "capture_fail" or self._should_fail_random():
            return CaptureResult(ok=False, confirmation=None, charged_cents=None,
                                 error="provider rejected capture", error_code="CAPTURE_REJECTED")
        with self._lock:
            cached = self._captures.get(idempotency_key)
            if cached:
                return CaptureResult(ok=True, confirmation=cached["confirmation"],
                                     charged_cents=cached["charged_cents"])
            hold = self._holds.get(hold_id)
            if not hold:
                return CaptureResult(ok=False, confirmation=None, charged_cents=None,
                                     error="hold not found", error_code="HOLD_NOT_FOUND")
            confirmation = f"FLAKY-{uuid.uuid4().hex[:8].upper()}"
            self._captures[idempotency_key] = {
                "confirmation": confirmation, "charged_cents": hold["amount_cents"],
            }
            return CaptureResult(ok=True, confirmation=confirmation,
                                 charged_cents=hold["amount_cents"])

    async def release(self, *, hold_id: str) -> ReleaseResult:
        if self.mode == "release_fail":
            return ReleaseResult(ok=False, error="release endpoint timed out")
        with self._lock:
            self._holds.pop(hold_id, None)
        return ReleaseResult(ok=True)

    async def refund(self, *, confirmation: str, amount_cents: int) -> RefundResult:
        return RefundResult(ok=True, refunded_cents=amount_cents)
