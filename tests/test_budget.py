"""Per-request hard budget — dollar and time ceilings."""

from __future__ import annotations

import time

import pytest

from agent.budget import BudgetExceeded, RequestBudget


def test_dollar_ceiling_blocks_over_budget_op():
    b = RequestBudget.new(max_dollars=0.20, max_seconds=300)   # 20 cents
    b.charge("llm_intent")    # 5
    b.charge("search_flights")  # 5
    b.charge("search_hotels")   # 5
    b.charge("get_weather")     # 1
    # 16 cents spent. llm_synthesis costs 50 — must blow budget.
    with pytest.raises(BudgetExceeded, match="exceed dollar ceiling"):
        b.charge("llm_synthesis")


def test_time_ceiling_blocks():
    b = RequestBudget.new(max_dollars=10.0, max_seconds=0)
    # max_seconds=0 means already exhausted
    time.sleep(0.01)
    with pytest.raises(BudgetExceeded, match="wall-clock"):
        b.charge("search_flights")


def test_near_limit_threshold():
    b = RequestBudget.new(max_dollars=1.0, max_seconds=300)   # $1
    assert not b.near_limit()
    b.charge("llm_synthesis")   # 50c → 50%
    assert not b.near_limit()
    b.charge("llm_synthesis")   # +50c → 100%
    assert b.near_limit()


def test_charge_returns_cost():
    b = RequestBudget.new(max_dollars=10.0, max_seconds=300)
    assert b.charge("search_flights") == 5
    assert b.charge("get_weather") == 1


@pytest.mark.asyncio
async def test_orchestrator_aborts_when_budget_too_low(log, monkeypatch):
    """If max_dollars is set absurdly low, the orchestrator transitions to FAILED."""
    monkeypatch.setenv("REQUEST_MAX_DOLLARS", "0.05")   # 5 cents — only intent fits
    import api.config; import importlib; importlib.reload(api.config)

    from agent.orchestrator import run_agent
    session, options = await run_agent(
        raw_text="4 days in Lisbon next month under $2000",
        user_id="test-user", log=log,
    )
    assert session.state.value == "failed"
    assert options == []
    types = [e.type for e in log.events_for_request(session.request_id)]
    assert "budget.exceeded" in types
