"""Detect drift between the option as presented and the option at approval time.

Drift thresholds (configurable):
  - total_price : ±2% OR ±$10, whichever larger → re-confirm
  - cancellation_policy : any change → re-confirm
  - dates / room_type / inventory : any change → replan (drop back to PRESENTING)
"""

from __future__ import annotations

from dataclasses import dataclass

from api.schemas import ItineraryOption


@dataclass
class DriftReport:
    has_drift: bool
    requires_replan: bool
    diffs: list[str]


def revalidate(
    presented: ItineraryOption,
    current: ItineraryOption,
    *,
    price_pct_threshold: float = 0.02,
    price_abs_threshold_cents: int = 1000,
) -> DriftReport:
    diffs: list[str] = []
    requires_replan = False

    # Same option_id assumed; if not, that's a programming error.
    assert presented.id == current.id, "revalidator called with mismatched option ids"

    # Date / room-type / inventory changes -> replan
    if presented.flight.value.id != current.flight.value.id:
        diffs.append(
            f"flight changed: {presented.flight.value.id} → {current.flight.value.id}"
        )
        requires_replan = True
    if presented.hotel.value.id != current.hotel.value.id:
        diffs.append(f"hotel changed: {presented.hotel.value.id} → {current.hotel.value.id}")
        requires_replan = True
    if presented.hotel.value.check_in != current.hotel.value.check_in:
        diffs.append(
            f"check-in changed: {presented.hotel.value.check_in} → {current.hotel.value.check_in}"
        )
        requires_replan = True

    # Price drift
    delta = abs(current.total_price_cents.value - presented.total_price_cents.value)
    threshold = max(
        int(presented.total_price_cents.value * price_pct_threshold),
        price_abs_threshold_cents,
    )
    if delta > threshold:
        diffs.append(
            f"price changed by ${delta/100:.2f} "
            f"({presented.total_price_cents.value/100:.2f} → "
            f"{current.total_price_cents.value/100:.2f})"
        )
        # Price drift requires re-confirm but not replan.

    # Cancellation policy change
    p_refund = presented.hotel.value.refundable_until
    c_refund = current.hotel.value.refundable_until
    if p_refund != c_refund:
        diffs.append(f"cancellation policy changed: {p_refund} → {c_refund}")

    return DriftReport(
        has_drift=bool(diffs),
        requires_replan=requires_replan,
        diffs=diffs,
    )
