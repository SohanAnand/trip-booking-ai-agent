"""Every fact in every option must point to a real ToolCall row."""

from __future__ import annotations

import pytest

from agent.orchestrator import run_agent


async def test_every_option_field_grounded(log):
    session, options = await run_agent(
        raw_text="4 days in Lisbon next month under $2000",
        user_id="test-user", log=log,
    )
    assert len(options) == 3
    for opt in options:
        for fname in ("flight", "hotel", "weather", "total_price_cents", "currency"):
            grounded = getattr(opt, fname)
            if grounded is None:
                continue
            tcid = grounded.prov.tool_call_id
            row = log.get_tool_call(tcid)
            assert row is not None, f"option {opt.id}.{fname} cites missing tool_call {tcid}"
            assert row["status"] == "ok"


async def test_option_snapshot_hash_stable(log):
    session, options = await run_agent(
        raw_text="4 days in Lisbon next month under $2000",
        user_id="test-user", log=log,
    )
    for opt in options:
        snap = log.get_option_snapshot(opt.id)
        assert snap is not None
        # Recompute hash from stored snapshot — must match
        from audit.log import jcs_canonical, sha256_hex
        recomputed = sha256_hex(jcs_canonical(snap["snapshot"]))
        assert recomputed == snap["snapshot_hash"]
