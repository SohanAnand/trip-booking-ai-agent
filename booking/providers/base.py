"""PaymentProvider Protocol: hold / capture / release / refund.

All providers implement this. Mocks for M1/M3, Stripe test mode for M5.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from booking.states import CapabilityTier


@dataclass
class HoldResult:
    ok: bool
    hold_id: str | None
    expires_at: str | None
    error: str | None = None
    error_code: str | None = None


@dataclass
class CaptureResult:
    ok: bool
    confirmation: str | None
    charged_cents: int | None
    error: str | None = None
    error_code: str | None = None


@dataclass
class ReleaseResult:
    ok: bool
    error: str | None = None


@dataclass
class RefundResult:
    ok: bool
    refunded_cents: int | None
    error: str | None = None


class PaymentProvider(Protocol):
    name: str
    tier: CapabilityTier

    async def hold(self, *, amount_cents: int, currency: str, idempotency_key: str,
                   metadata: dict) -> HoldResult: ...

    async def capture(self, *, hold_id: str, idempotency_key: str) -> CaptureResult: ...

    async def release(self, *, hold_id: str) -> ReleaseResult: ...

    async def refund(self, *, confirmation: str, amount_cents: int) -> RefundResult: ...
