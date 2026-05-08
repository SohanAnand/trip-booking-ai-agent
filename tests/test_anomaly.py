"""Anomaly-detection rule tests."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from approval.anomaly import (
    AnomalyTier,
    BookingContext,
    assess,
    record_pm_use,
)


def _ctx(**overrides) -> BookingContext:
    base = dict(
        user_id="u-1",
        total_cents=100_000,             # $1,000
        departure_iso="2099-01-01T00:00:00+00:00",   # far future
        payment_method_id="pm_visa_42",
        pm_billing_country="US",
        booking_origin_country="US",
        risk_score=0.0,
    )
    base.update(overrides)
    return BookingContext(**base)


def test_clean_booking_no_tier():
    record_pm_use("u-1", "pm_visa_42")
    a = assess(_ctx())
    assert a.tier == AnomalyTier.NONE


def test_high_total_triggers_sms():
    record_pm_use("u-1", "pm_visa_42")
    a = assess(_ctx(total_cents=200_000))
    assert a.tier == AnomalyTier.SMS
    assert any(">" in r and "1,500" in r for r in a.reasons)


def test_very_high_total_escalates_to_reinput():
    record_pm_use("u-1", "pm_visa_42")
    a = assess(_ctx(total_cents=600_000))
    assert a.tier == AnomalyTier.REINPUT


def test_extreme_total_escalates_to_high_touch():
    record_pm_use("u-1", "pm_visa_42")
    a = assess(_ctx(total_cents=1_500_000))
    assert a.tier == AnomalyTier.HIGH_TOUCH


def test_new_pm_triggers_sms():
    # Don't record the PM first; should be flagged as new.
    a = assess(_ctx(payment_method_id="pm_unseen_99"))
    assert a.tier == AnomalyTier.SMS
    assert any("new payment" in r for r in a.reasons)


def test_geographic_mismatch_triggers_sms():
    record_pm_use("u-2", "pm_visa_42")
    a = assess(_ctx(user_id="u-2", pm_billing_country="GB", booking_origin_country="US"))
    assert a.tier == AnomalyTier.SMS
    assert any("payment country" in r for r in a.reasons)


def test_imminent_departure_triggers_sms():
    record_pm_use("u-3", "pm_visa_42")
    soon = datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc).isoformat()
    a = assess(_ctx(user_id="u-3", departure_iso=soon))
    assert a.tier == AnomalyTier.SMS
    assert any("24 hours" in r for r in a.reasons)


def test_high_risk_score_triggers_high_touch():
    record_pm_use("u-4", "pm_visa_42")
    a = assess(_ctx(user_id="u-4", risk_score=0.95))
    assert a.tier == AnomalyTier.HIGH_TOUCH


def test_multiple_signals_pick_highest_tier():
    a = assess(_ctx(user_id="u-5", payment_method_id="pm_unseen",
                    total_cents=600_000))
    # new PM (SMS) + total > $5k (REINPUT) → REINPUT
    assert a.tier == AnomalyTier.REINPUT
    assert len(a.reasons) >= 2
