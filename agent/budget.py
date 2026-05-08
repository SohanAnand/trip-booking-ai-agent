"""Per-request hard budget for the agent loop.

Two ceilings:
  - dollars_remaining: hard cap on tool/LLM cost per request
  - ms_remaining: wall-clock cap

When 70% of either is consumed, the orchestrator transitions to graceful
degradation: stop exploring breadth, drop nice-to-haves, present partial results.
At 100%, the request transitions to FAILED with a partial banner.

Costs are static estimates per tool — easy to override via env or config.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


# Per-call cost estimates in cents. Treated as upper bounds, not actuals.
COST_TABLE_CENTS: dict[str, int] = {
    "search_flights": 5,    # GDS / Amadeus
    "search_hotels": 5,
    "get_weather": 1,
    "fetch_reviews": 3,     # gstack page load
    "embed_reviews": 10,    # voyage call
    "llm_synthesis": 50,    # sonnet
    "llm_intent": 5,        # haiku
}


@dataclass
class RequestBudget:
    started_at: float
    max_dollars: float
    max_seconds: int
    spent_cents: int = 0

    @classmethod
    def new(cls, max_dollars: float, max_seconds: int) -> "RequestBudget":
        return cls(started_at=time.time(), max_dollars=max_dollars,
                   max_seconds=max_seconds, spent_cents=0)

    def elapsed_ms(self) -> int:
        return int((time.time() - self.started_at) * 1000)

    def dollars_remaining(self) -> float:
        return self.max_dollars - self.spent_cents / 100

    def seconds_remaining(self) -> int:
        return max(0, self.max_seconds - int(time.time() - self.started_at))

    def can_afford(self, op_cents: int) -> bool:
        return (self.spent_cents + op_cents) / 100 <= self.max_dollars

    def time_ok(self) -> bool:
        return self.seconds_remaining() > 0

    def near_limit(self, threshold: float = 0.70) -> bool:
        """True iff either dimension is past `threshold` of its cap."""
        dollars_used_pct = (self.spent_cents / 100) / max(self.max_dollars, 0.01)
        seconds_used_pct = (
            int(time.time() - self.started_at) / max(self.max_seconds, 1)
        )
        return dollars_used_pct >= threshold or seconds_used_pct >= threshold

    def charge(self, op: str) -> int:
        """Charge a known operation; returns its cost. Raises BudgetExceeded if over."""
        c = COST_TABLE_CENTS.get(op, 5)
        if not self.can_afford(c):
            raise BudgetExceeded(
                f"would exceed dollar ceiling charging '{op}' ({c}c) "
                f"on top of {self.spent_cents}c"
            )
        if not self.time_ok():
            raise BudgetExceeded(
                f"wall-clock budget exhausted before '{op}'"
            )
        self.spent_cents += c
        return c


class BudgetExceeded(Exception):
    pass
