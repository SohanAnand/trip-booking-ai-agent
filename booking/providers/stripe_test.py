"""Stripe TEST mode adapter (M5).

Uses Stripe's PaymentIntents API with capture_method=manual to mirror our
hold/capture pattern. Idempotency keys flow through to Stripe via the
Idempotency-Key header.

CRITICAL: this adapter accepts ONLY test keys. The constructor refuses any
key not starting with `sk_test_`. Real-money launch requires legal sign-off
plus a deliberate code change to remove the assertion.
"""

from __future__ import annotations

import httpx

from booking.providers.base import (
    CaptureResult,
    HoldResult,
    PaymentProvider,
    RefundResult,
    ReleaseResult,
)
from booking.states import CapabilityTier
from tools.flights.amadeus import _settings


STRIPE_API_URL = "https://api.stripe.com/v1"


class StripeTestProvider:
    name = "stripe_test"
    tier = CapabilityTier.A

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        s = _settings()
        if not s.stripe_test_key:
            raise RuntimeError("STRIPE_TEST_KEY not set")
        if not s.stripe_test_key.startswith("sk_test_"):
            raise RuntimeError(
                "STRIPE_TEST_KEY does not start with 'sk_test_'. "
                "Refusing to operate against a live Stripe account."
            )
        self._key = s.stripe_test_key
        self._client = client

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    def _headers(self, *, idempotency_key: str | None = None) -> dict:
        h = {"Authorization": f"Bearer {self._key}",
             "Content-Type": "application/x-www-form-urlencoded"}
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    async def hold(self, *, amount_cents: int, currency: str, idempotency_key: str,
                   metadata: dict) -> HoldResult:
        cli = await self._http()
        # In test mode we use Stripe's test payment-method `pm_card_visa` which
        # auto-confirms without requiring a customer-card collection step.
        data = {
            "amount": str(amount_cents),
            "currency": currency.lower(),
            "capture_method": "manual",
            "payment_method": metadata.get("test_pm_id", "pm_card_visa"),
            "confirm": "true",
        }
        for k, v in metadata.items():
            data[f"metadata[{k}]"] = str(v)
        res = await cli.post(
            f"{STRIPE_API_URL}/payment_intents",
            headers=self._headers(idempotency_key=idempotency_key),
            data=data,
        )
        if res.status_code >= 300:
            return HoldResult(
                ok=False, hold_id=None, expires_at=None,
                error=res.json().get("error", {}).get("message", "stripe error"),
                error_code="STRIPE_ERROR",
            )
        body = res.json()
        return HoldResult(
            ok=True, hold_id=body["id"],
            expires_at=str(body.get("created", 0) + 7 * 86400),    # holds last ~7 days
        )

    async def capture(self, *, hold_id: str, idempotency_key: str) -> CaptureResult:
        cli = await self._http()
        res = await cli.post(
            f"{STRIPE_API_URL}/payment_intents/{hold_id}/capture",
            headers=self._headers(idempotency_key=idempotency_key),
            data={},
        )
        if res.status_code >= 300:
            return CaptureResult(
                ok=False, confirmation=None, charged_cents=None,
                error=res.json().get("error", {}).get("message", "stripe capture failed"),
                error_code="STRIPE_CAPTURE_FAILED",
            )
        body = res.json()
        return CaptureResult(
            ok=True, confirmation=body["id"],
            charged_cents=body.get("amount_received") or body.get("amount", 0),
        )

    async def release(self, *, hold_id: str) -> ReleaseResult:
        cli = await self._http()
        res = await cli.post(
            f"{STRIPE_API_URL}/payment_intents/{hold_id}/cancel",
            headers=self._headers(),
            data={},
        )
        if res.status_code >= 300:
            return ReleaseResult(ok=False, error="stripe cancel failed")
        return ReleaseResult(ok=True)

    async def refund(self, *, confirmation: str, amount_cents: int) -> RefundResult:
        cli = await self._http()
        res = await cli.post(
            f"{STRIPE_API_URL}/refunds",
            headers=self._headers(),
            data={"payment_intent": confirmation, "amount": str(amount_cents)},
        )
        if res.status_code >= 300:
            return RefundResult(
                ok=False, refunded_cents=None,
                error=res.json().get("error", {}).get("message", "stripe refund failed"),
            )
        body = res.json()
        return RefundResult(ok=True, refunded_cents=body.get("amount", amount_cents))
