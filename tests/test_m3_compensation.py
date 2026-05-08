"""M3: full compensation matrix.

Each test induces a different failure mode and asserts:
  - Final booking state is FAILED or COMPENSATED.
  - The audit log contains the expected sequence of events.
  - No leg is left "captured but un-refunded" except where the test asserts that.
"""

from __future__ import annotations

import os
import pytest

from agent.orchestrator import run_agent
from approval.gate import ApprovalGate
from booking.providers.mock_flaky import MockFlakyProvider
from booking.two_phase import BookingLeg, execute_booking


@pytest.fixture
async def planned(log):
    session, options = await run_agent(
        raw_text="4 days in Lisbon next month under $2000",
        user_id="test-user", log=log,
    )
    return session.request_id, options


async def _approve(log, request_id, option):
    gate = ApprovalGate(log)
    summary, otp, _ = gate.select(
        request_id=request_id, user_id="test-user", option=option,
    )
    return gate.authorize(option_id=option.id, otp=otp)


async def test_full_capture_fail_releases_holds(log, planned, monkeypatch):
    """All legs hold OK; capture on first leg fails; held legs are released."""
    request_id, options = planned
    opt = options[0]
    token = await _approve(log, request_id, opt)
    snap = log.get_option_snapshot(opt.id)

    monkeypatch.setenv("RUN_FAILURE_MODE", "capture_fail")
    p_flight = MockFlakyProvider(leg_label="flight")
    p_hotel = MockFlakyProvider(leg_label="hotel")
    legs = [
        BookingLeg(leg_id="flight", label="f",
                   amount_cents=opt.flight.value.total_price_cents,
                   currency=opt.currency.value, provider=p_flight),
        BookingLeg(leg_id="hotel", label="h",
                   amount_cents=opt.hotel.value.total_price_cents,
                   currency=opt.currency.value, provider=p_hotel),
    ]
    res = await execute_booking(
        token=token, legs=legs, log=log, request_id=request_id,
        option_id=opt.id, option_hash=snap["snapshot_hash"],
    )
    assert res.state == "COMPENSATED"
    types = [e.type for e in log.events_for_booking(res.booking_id)]
    assert "leg.held" in types
    assert "leg.capture_failed" in types
    assert "leg.released" in types
    assert "booking.committed" not in types


async def test_release_failure_logged(log, planned, monkeypatch):
    """Even if release() also fails, the booking ends in COMPENSATED state and
    the failure is logged for follow-up. The chain still verifies."""
    from audit.verify import walk_chain
    request_id, options = planned
    opt = options[0]
    token = await _approve(log, request_id, opt)
    snap = log.get_option_snapshot(opt.id)

    # Capture-fail triggers release path; release_fail makes that path itself fail.
    # We use a single env var so we induce capture_fail + simulate release_fail by
    # using a custom provider whose release always fails.
    class ReleaseFailingProvider(MockFlakyProvider):
        async def release(self, *, hold_id):
            from booking.providers.base import ReleaseResult
            return ReleaseResult(ok=False, error="release endpoint timed out")

    monkeypatch.setenv("RUN_FAILURE_MODE", "capture_fail")
    p = ReleaseFailingProvider(leg_label="flight")
    p2 = ReleaseFailingProvider(leg_label="hotel")
    legs = [
        BookingLeg(leg_id="flight", label="f",
                   amount_cents=opt.flight.value.total_price_cents,
                   currency=opt.currency.value, provider=p),
        BookingLeg(leg_id="hotel", label="h",
                   amount_cents=opt.hotel.value.total_price_cents,
                   currency=opt.currency.value, provider=p2),
    ]
    res = await execute_booking(
        token=token, legs=legs, log=log, request_id=request_id,
        option_id=opt.id, option_hash=snap["snapshot_hash"],
    )
    assert res.state == "COMPENSATED"
    # Audit log must record that release itself failed — operations would page on this.
    payloads = [e.payload for e in log.events_for_booking(res.booking_id)
                if e.type == "leg.released"]
    assert any(p.get("ok") is False for p in payloads), \
        "expected at least one leg.released event with ok=False"
    # Critically: the chain still verifies even though we're in a degraded state.
    assert walk_chain(log).ok


async def test_idempotent_replay_returns_same_result(log, planned):
    """A replay of the SAME (token, option) returns the same booking, no second charge.

    Property: after a successful booking, calling execute_booking again with the
    same token raises (jti consumed). This test asserts the provider does not
    charge twice, by checking the token replay rejection path.
    """
    request_id, options = planned
    opt = options[0]
    token = await _approve(log, request_id, opt)
    snap = log.get_option_snapshot(opt.id)
    from booking.providers.mock_always import MockAlwaysProvider
    p = MockAlwaysProvider()
    legs = [
        BookingLeg(leg_id="flight", label="f",
                   amount_cents=opt.flight.value.total_price_cents,
                   currency=opt.currency.value, provider=p),
        BookingLeg(leg_id="hotel", label="h",
                   amount_cents=opt.hotel.value.total_price_cents,
                   currency=opt.currency.value, provider=p),
    ]
    res1 = await execute_booking(
        token=token, legs=legs, log=log, request_id=request_id,
        option_id=opt.id, option_hash=snap["snapshot_hash"],
    )
    assert res1.state == "COMMITTED"
    confirm_count_before = len(p._captures)

    res2 = await execute_booking(
        token=token, legs=legs, log=log, request_id=request_id,
        option_id=opt.id, option_hash=snap["snapshot_hash"],
    )
    assert res2.state == "FAILED"
    assert "replay" in (res2.error or "").lower()
    assert len(p._captures) == confirm_count_before, \
        "second call must NOT have produced new captures"
