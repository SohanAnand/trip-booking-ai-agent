"""Hash-chained audit log: tampering breaks verify.py."""

from __future__ import annotations

import json
import sqlite3

import pytest

from audit.verify import walk_chain


def test_empty_chain_verifies(log):
    res = walk_chain(log)
    assert res.ok and res.events_checked == 0


def test_appended_chain_verifies(log):
    log.append("user", "request.opened", {"raw_text": "Lisbon"}, request_id="r1")
    log.append("agent", "tool.called", {"tool": "search_flights"}, request_id="r1")
    log.append("agent", "options.presented", {"count": 3}, request_id="r1")
    res = walk_chain(log)
    assert res.ok
    assert res.events_checked == 3


def test_tampering_payload_breaks_chain(log):
    log.append("user", "request.opened", {"raw_text": "Lisbon"}, request_id="r1")
    log.append("agent", "tool.called", {"tool": "search_flights"}, request_id="r1")
    log.append("agent", "options.presented", {"count": 3}, request_id="r1")

    # Tamper: change the payload of seq=2 directly in SQLite.
    with sqlite3.connect(log.db_path) as conn:
        conn.execute(
            "UPDATE events SET payload = ? WHERE seq = 2",
            (json.dumps({"tool": "search_hotels"}, sort_keys=True, separators=(",", ":")),),
        )

    res = walk_chain(log)
    assert not res.ok
    assert res.broken_at_seq == 2


def test_tampering_event_hash_breaks_chain(log):
    log.append("user", "request.opened", {"raw_text": "Lisbon"}, request_id="r1")
    log.append("agent", "tool.called", {"tool": "search_flights"}, request_id="r1")

    with sqlite3.connect(log.db_path) as conn:
        conn.execute("UPDATE events SET event_hash = 'tampered' WHERE seq = 2")

    res = walk_chain(log)
    assert not res.ok
    # The first event (seq=1) is fine; seq=2 has a wrong event_hash.
    # When we walk to seq=3, prev_hash mismatch is detected. The break either
    # surfaces at seq=2 (event_hash mismatch) or seq=3 (prev_hash mismatch).
    assert res.broken_at_seq in (2, 3)


def test_jti_consumed_once(log):
    assert log.consume_jti("jti-abc") is True
    assert log.consume_jti("jti-abc") is False
    assert log.consume_jti("jti-xyz") is True
