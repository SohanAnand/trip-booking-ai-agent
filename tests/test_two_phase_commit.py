"""Two-phase commit + compensation tests."""

from __future__ import annotations

import os

import pytest

from agent.orchestrator import run_agent
from approval.gate import ApprovalGate
from booking.providers.mock_always import MockAlwaysProvider
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


async def test_2pc_happy_path(log, planned):
    request_id, options = planned
    opt = options[0]
    token = await _approve(log, request_id, opt)
    snap = log.get_option_snapshot(opt.id)
    p = MockAlwaysProvider()
    legs = [
        BookingLeg(leg_id="flight", label="f",
                   amount_cents=opt.flight.value.total_price_cents,
                   currency=opt.currency.value, provider=p),
        BookingLeg(leg_id="hotel", label="h",
                   amount_cents=opt.hotel.value.total_price_cents,
                   currency=opt.currency.value, provider=p),
    ]
    res = await execute_booking(
        token=token, legs=legs, log=log, request_id=request_id,
        option_id=opt.id, option_hash=snap["snapshot_hash"],
    )
    assert res.state == "COMMITTED"
    # Audit trail: held → captured for each leg, plus committed
    types = [e.type for e in log.events_for_booking(res.booking_id)]
    assert "leg.held" in types
    assert "leg.captured" in types
    assert "booking.committed" in types


async def test_2pc_hold_failure_releases_prior_holds(log, planned, monkeypatch):
    request_id, options = planned
    opt = options[0]
    token = await _approve(log, request_id, opt)
    snap = log.get_option_snapshot(opt.id)

    monkeypatch.setenv("RUN_FAILURE_MODE", "hold_fail")
    flaky_flight = MockFlakyProvider(leg_label="flight")
    flaky_hotel = MockFlakyProvider(leg_label="hotel")
    legs = [
        BookingLeg(leg_id="flight", label="f",
                   amount_cents=opt.flight.value.total_price_cents,
                   currency=opt.currency.value, provider=flaky_flight),
        BookingLeg(leg_id="hotel", label="h",
                   amount_cents=opt.hotel.value.total_price_cents,
                   currency=opt.currency.value, provider=flaky_hotel),
    ]
    res = await execute_booking(
        token=token, legs=legs, log=log, request_id=request_id,
        option_id=opt.id, option_hash=snap["snapshot_hash"],
    )
    assert res.state == "FAILED"


async def test_2pc_capture_partial_compensates(log, planned, monkeypatch):
    request_id, options = planned
    opt = options[0]
    token = await _approve(log, request_id, opt)
    snap = log.get_option_snapshot(opt.id)

    monkeypatch.setenv("RUN_FAILURE_MODE", "capture_partial")
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
    # The capture order is reversed (hotel first), so hotel fails before any
    # captures succeed → no refund needed; both hold-releases happen.
    assert "leg.capture_failed" in types
    assert "leg.released" in types
