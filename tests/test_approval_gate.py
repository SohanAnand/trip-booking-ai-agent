"""Trust gate: every attack vector is rejected with audit entries."""

from __future__ import annotations

import pytest

from agent.orchestrator import run_agent
from approval.gate import ApprovalGate, AuthorizationFailed
from approval.tokens import (
    ApprovalToken,
    ApprovalTokenPayload,
    TokenInvalid,
    mint_token,
    verify_token,
)
from booking.providers.mock_always import MockAlwaysProvider
from booking.two_phase import BookingLeg, execute_booking


@pytest.fixture
async def planned(log):
    """Plan a fresh trip and return (request_id, options)."""
    session, options = await run_agent(
        raw_text="4 days in Lisbon next month under $2000",
        user_id="test-user", log=log,
    )
    assert len(options) == 3
    return session.request_id, options


async def test_happy_path_books(log, planned):
    request_id, options = planned
    selected = options[1]   # rank 2 (best_reviewed)
    gate = ApprovalGate(log)
    summary, otp, drift = gate.select(
        request_id=request_id, user_id="test-user", option=selected,
    )
    assert not drift.has_drift
    token = gate.authorize(option_id=selected.id, otp=otp)
    snap = log.get_option_snapshot(selected.id)
    legs = _legs(selected)
    res = await execute_booking(
        token=token, legs=legs, log=log, request_id=request_id,
        option_id=selected.id, option_hash=snap["snapshot_hash"],
    )
    assert res.state == "COMMITTED"
    assert "flight" in res.confirmations and "hotel" in res.confirmations


async def test_token_replay_rejected(log, planned):
    request_id, options = planned
    selected = options[0]
    gate = ApprovalGate(log)
    summary, otp, drift = gate.select(
        request_id=request_id, user_id="test-user", option=selected,
    )
    token = gate.authorize(option_id=selected.id, otp=otp)
    snap = log.get_option_snapshot(selected.id)
    legs = _legs(selected)

    res1 = await execute_booking(
        token=token, legs=legs, log=log, request_id=request_id,
        option_id=selected.id, option_hash=snap["snapshot_hash"],
    )
    assert res1.state == "COMMITTED"

    # Same token, second use → must be rejected
    res2 = await execute_booking(
        token=token, legs=legs, log=log, request_id=request_id,
        option_id=selected.id, option_hash=snap["snapshot_hash"],
    )
    assert res2.state == "FAILED"
    assert "replay" in (res2.error or "").lower()


async def test_expired_token_rejected(log, planned):
    request_id, options = planned
    selected = options[0]
    gate = ApprovalGate(log)
    summary, otp, drift = gate.select(
        request_id=request_id, user_id="test-user", option=selected,
    )
    snap = log.get_option_snapshot(selected.id)
    token = mint_token(
        user_id="test-user", request_id=request_id,
        option_id=selected.id, option_hash=snap["snapshot_hash"],
        amount_value=f"{selected.total_price_cents.value/100:.2f}",
        amount_currency=selected.currency.value,
        payment_method_id="pm_demo_visa",
        user_consent_text="auto-test",
        ttl_seconds=-10,   # already expired
    )
    with pytest.raises(TokenInvalid, match="expired"):
        verify_token(token)


async def test_token_for_wrong_option_rejected(log, planned):
    request_id, options = planned
    selected_a, selected_b = options[0], options[1]
    gate = ApprovalGate(log)
    summary, otp, _ = gate.select(
        request_id=request_id, user_id="test-user", option=selected_a,
    )
    token = gate.authorize(option_id=selected_a.id, otp=otp)
    snap_b = log.get_option_snapshot(selected_b.id)
    # Token signed for option A; attempt to book option B must raise TokenInvalid
    # at the very first step of execute_booking. No charge can occur.
    with pytest.raises(TokenInvalid, match="option_id mismatch"):
        await execute_booking(
            token=token, legs=_legs(selected_b), log=log,
            request_id=request_id, option_id=selected_b.id,
            option_hash=snap_b["snapshot_hash"],
        )


async def test_wrong_otp_rejected(log, planned):
    request_id, options = planned
    selected = options[0]
    gate = ApprovalGate(log)
    summary, otp, _ = gate.select(
        request_id=request_id, user_id="test-user", option=selected,
    )
    bad = "000000" if otp != "000000" else "111111"
    with pytest.raises(AuthorizationFailed, match="incorrect"):
        gate.authorize(option_id=selected.id, otp=bad)


async def test_authorize_without_select_rejected(log, planned):
    request_id, options = planned
    gate = ApprovalGate(log)
    with pytest.raises(AuthorizationFailed, match="no pending"):
        gate.authorize(option_id=options[0].id, otp="000000")


async def test_double_authorize_rejected(log, planned):
    request_id, options = planned
    selected = options[0]
    gate = ApprovalGate(log)
    summary, otp, _ = gate.select(
        request_id=request_id, user_id="test-user", option=selected,
    )
    gate.authorize(option_id=selected.id, otp=otp)
    # The pending was consumed; second authorize must fail
    with pytest.raises(AuthorizationFailed):
        gate.authorize(option_id=selected.id, otp=otp)


async def test_tampered_signature_rejected(log, planned):
    request_id, options = planned
    selected = options[0]
    gate = ApprovalGate(log)
    summary, otp, _ = gate.select(
        request_id=request_id, user_id="test-user", option=selected,
    )
    token = gate.authorize(option_id=selected.id, otp=otp)
    # Flip a byte in the signature
    bad_sig = "A" * len(token.sig) if token.sig[0] != "A" else "B" * len(token.sig)
    bad_token = ApprovalToken(payload=token.payload, sig=bad_sig, kid=token.kid)
    with pytest.raises(TokenInvalid, match="signature"):
        verify_token(bad_token)


async def test_amount_mutation_rejected(log, planned):
    request_id, options = planned
    selected = options[0]
    gate = ApprovalGate(log)
    summary, otp, _ = gate.select(
        request_id=request_id, user_id="test-user", option=selected,
    )
    token = gate.authorize(option_id=selected.id, otp=otp)
    # Mutate amount in payload (without re-signing)
    bad_payload = ApprovalTokenPayload(**{
        **token.payload.to_dict(), "amount_value": "0.01",
    })
    bad_token = ApprovalToken(payload=bad_payload, sig=token.sig, kid=token.kid)
    with pytest.raises(TokenInvalid, match="signature"):
        verify_token(bad_token)


def _legs(opt):
    p = MockAlwaysProvider()
    return [
        BookingLeg(leg_id="flight", label="flight",
                   amount_cents=opt.flight.value.total_price_cents,
                   currency=opt.currency.value, provider=p),
        BookingLeg(leg_id="hotel", label="hotel",
                   amount_cents=opt.hotel.value.total_price_cents,
                   currency=opt.currency.value, provider=p),
    ]
