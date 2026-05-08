"""Rule-based anomaly detection (M5).

Triggers a re-confirmation step (Tier-1 OOB SMS, or Tier-3 video selfie at
high values) when ANY of these match:

  - total > $1500
  - departure within 24h
  - new payment method (first use for this user)
  - geographic mismatch (payment method country != booking origin country)
  - velocity > 2 bookings in the last hour for this user
  - explicit risk score from external fraud provider above threshold

Each trigger contributes a tier; the highest tier wins. Tier-1 is SMS OTP;
Tier-3 escalates to selfie/CS callback.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

from tools.flights.amadeus import _settings


class AnomalyTier(IntEnum):
    NONE = 0
    SMS = 1            # Tier-1: SMS OTP
    REINPUT = 2        # Tier-2: SMS OTP + re-type total
    HIGH_TOUCH = 3     # Tier-3: video selfie or CS callback


@dataclass
class AnomalyAssessment:
    tier: AnomalyTier
    reasons: list[str]


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_settings().sqlite_path)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class BookingContext:
    user_id: str
    total_cents: int
    departure_iso: str       # earliest leg departure ISO
    payment_method_id: str
    pm_billing_country: str
    booking_origin_country: str
    risk_score: float = 0.0  # 0.0–1.0 from fraud provider; 0 by default


def assess(ctx: BookingContext) -> AnomalyAssessment:
    reasons: list[str] = []
    tier = AnomalyTier.NONE

    # 1. Total > $1500
    if ctx.total_cents > 150_000:
        tier = max(tier, AnomalyTier.SMS)
        reasons.append(f"total ${ctx.total_cents/100:.2f} > $1,500")

    # 2. Total > $5,000 → re-input total
    if ctx.total_cents > 500_000:
        tier = max(tier, AnomalyTier.REINPUT)
        reasons.append(f"total ${ctx.total_cents/100:.2f} > $5,000")

    # 3. Total > $10,000 → high touch
    if ctx.total_cents > 1_000_000:
        tier = max(tier, AnomalyTier.HIGH_TOUCH)
        reasons.append(f"total ${ctx.total_cents/100:.2f} > $10,000")

    # 4. Departure within 24h
    try:
        dep = datetime.fromisoformat(ctx.departure_iso.replace("Z", "+00:00"))
        delta = (dep.timestamp() - time.time())
        if 0 < delta < 86400:
            tier = max(tier, AnomalyTier.SMS)
            reasons.append("departure within 24 hours")
    except Exception:
        pass

    # 5. New payment method
    if _is_new_pm(ctx.user_id, ctx.payment_method_id):
        tier = max(tier, AnomalyTier.SMS)
        reasons.append("new payment method (first use)")

    # 6. Geographic mismatch
    if (ctx.pm_billing_country and ctx.booking_origin_country
            and ctx.pm_billing_country != ctx.booking_origin_country):
        tier = max(tier, AnomalyTier.SMS)
        reasons.append(
            f"payment country {ctx.pm_billing_country} != origin {ctx.booking_origin_country}"
        )

    # 7. Velocity > 2 bookings in last hour
    if _recent_bookings(ctx.user_id, seconds=3600) > 2:
        tier = max(tier, AnomalyTier.SMS)
        reasons.append("velocity: >2 bookings in last hour")

    # 8. Risk score
    if ctx.risk_score >= 0.8:
        tier = max(tier, AnomalyTier.HIGH_TOUCH)
        reasons.append(f"fraud risk score {ctx.risk_score:.2f} >= 0.80")
    elif ctx.risk_score >= 0.5:
        tier = max(tier, AnomalyTier.SMS)
        reasons.append(f"fraud risk score {ctx.risk_score:.2f} >= 0.50")

    return AnomalyAssessment(tier=tier, reasons=reasons)


def _is_new_pm(user_id: str, payment_method_id: str) -> bool:
    """True iff this PM has never been used by this user before."""
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_payment_methods (
                user_id TEXT NOT NULL,
                payment_method_id TEXT NOT NULL,
                first_used_at TEXT NOT NULL,
                PRIMARY KEY (user_id, payment_method_id)
            );
        """)
        row = conn.execute(
            "SELECT 1 FROM user_payment_methods WHERE user_id = ? AND payment_method_id = ?",
            (user_id, payment_method_id),
        ).fetchone()
    return row is None


def record_pm_use(user_id: str, payment_method_id: str) -> None:
    """Record a successful use, so subsequent bookings don't flag it as 'new'."""
    from datetime import UTC, datetime as dt
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_payment_methods (
                user_id TEXT NOT NULL,
                payment_method_id TEXT NOT NULL,
                first_used_at TEXT NOT NULL,
                PRIMARY KEY (user_id, payment_method_id)
            );
        """)
        conn.execute(
            "INSERT OR IGNORE INTO user_payment_methods (user_id, payment_method_id, first_used_at) "
            "VALUES (?, ?, ?)",
            (user_id, payment_method_id, dt.now(UTC).isoformat()),
        )


def _recent_bookings(user_id: str, *, seconds: int) -> int:
    """Count bookings created for this user within the last `seconds` seconds."""
    cutoff_ts = time.time() - seconds
    cutoff_iso = datetime.fromtimestamp(cutoff_ts).isoformat()
    with _conn() as conn:
        # Bookings table is created by audit/schema.sql; if absent, return 0.
        try:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM bookings b
                   JOIN events e ON e.booking_id = b.id
                   WHERE e.actor = 'user' AND e.created_at > ?
                     AND b.id IN (SELECT booking_id FROM events
                                  WHERE type = 'request.opened' AND payload LIKE ?)""",
                (cutoff_iso, f'%"user_id": "{user_id}"%'),
            ).fetchone()
            return int(row["n"] or 0) if row else 0
        except sqlite3.OperationalError:
            return 0
