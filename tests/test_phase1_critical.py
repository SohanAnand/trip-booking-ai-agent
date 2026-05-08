"""Phase 1 critical-fix coverage.

Three new tests that lock in the bugfixes from the audit-fix sprint:

  1. Single-leg consent text MUST mention the return flight (was silently
     dropping the return-flight scope after the one-way refactor).
  2. OTP brute-force lockout after MAX_OTP_FAILURES bad attempts.
  3. Hotel search receiving an airport code (e.g. LHR) resolves to a metro
     (LON) so LiteAPI/Amadeus city maps actually find inventory.
"""

from __future__ import annotations

import pytest

from agent.orchestrator import _resolve_metro, run_agent
from approval.gate import (
    MAX_OTP_FAILURES,
    ApprovalGate,
    AuthorizationFailed,
    _build_consent_text,
    _describe_cancellation,
)


# ---- 1. Consent text covers the return flight ----------------------------

@pytest.mark.asyncio
async def test_consent_text_single_leg_mentions_return_flight(log):
    """For a single-leg trip the orchestrator triggers a return-flight search;
    the consent text MUST name that return so the user authorizes the actual
    scope they're being charged for."""
    session, options = await run_agent(
        raw_text="4 days in Lisbon next month under $2000",
        user_id="u-consent", log=log,
    )
    assert options, "expected at least one option"
    o = options[0]
    # Sanity: this scenario should have a return flight (LIS != JFK).
    assert o.return_flight is not None

    consent = _build_consent_text(o)
    assert "return" in consent.lower(), f"consent text missing return: {consent}"
    # The return route must appear: LIS to JFK
    assert "LIS to JFK" in consent or "LIS→JFK" in consent

    cancellation = _describe_cancellation(o)
    # Cancellation policy must consider both outbound + return refundability;
    # since both flights are refundable in mock data, this should report so.
    assert "refundable" in cancellation.lower()


# ---- 2. OTP brute-force lockout ------------------------------------------

@pytest.mark.asyncio
async def test_otp_lockout_after_max_failures(log):
    """After MAX_OTP_FAILURES wrong codes the gate consumes the pending row
    and refuses further attempts, even with the correct code."""
    session, options = await run_agent(
        raw_text="4 days in Lisbon next month under $2000",
        user_id="u-lockout", log=log,
    )
    o = options[0]
    gate = ApprovalGate(log)
    summary, real_otp, drift = gate.select(
        request_id=o.request_id, user_id="u-lockout", option=o,
    )

    # Submit MAX_OTP_FAILURES wrong codes.
    for i in range(MAX_OTP_FAILURES):
        with pytest.raises(AuthorizationFailed) as exc:
            gate.authorize(option_id=o.id, otp="000000")
        if i < MAX_OTP_FAILURES - 1:
            assert "incorrect" in str(exc.value).lower()
        else:
            # On the Nth failure the pending row is consumed and the message
            # should say "locked out".
            assert "locked out" in str(exc.value).lower()

    # Even with the correct OTP, no pending row is left to authorize against.
    with pytest.raises(AuthorizationFailed) as exc:
        gate.authorize(option_id=o.id, otp=real_otp)
    assert "no pending" in str(exc.value).lower()


# ---- 3. Hotel search receives a metro code, not an airport ---------------

def test_resolve_metro_maps_airport_to_metro():
    """The hotel search dispatch passes destinations through _resolve_metro so
    LHR / JFK / CDG resolve to LON / NYC / PAR (which the LiteAPI/Amadeus
    city maps actually know about)."""
    assert _resolve_metro("LHR") == "LON"
    assert _resolve_metro("JFK") == "NYC"
    assert _resolve_metro("CDG") == "PAR"
    assert _resolve_metro("HND") == "TYO"
    # Already-metro codes pass through unchanged.
    assert _resolve_metro("LON") == "LON"
    assert _resolve_metro("NYC") == "NYC"
    # Unknown codes pass through unchanged (no surprise rewrites).
    assert _resolve_metro("LIS") == "LIS"
    assert _resolve_metro("ZZZ") == "ZZZ"
    # Case-insensitive on input, output stays in the map's case.
    assert _resolve_metro("lhr") == "LON"
