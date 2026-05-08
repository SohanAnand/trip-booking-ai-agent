"""Walk the audit log and verify the hash chain.

Used by tests, CLI verification, and the M4 audit page.
"""

from __future__ import annotations

from dataclasses import dataclass

from audit.log import GENESIS, AuditLog, Event, jcs_canonical, sha256_hex


@dataclass
class VerifyResult:
    ok: bool
    events_checked: int
    broken_at_seq: int | None = None
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def verify_event(event: Event, expected_prev_hash: str) -> tuple[bool, str | None]:
    """Verify a single event. Returns (ok, reason)."""
    if event.prev_hash != expected_prev_hash:
        return False, f"prev_hash mismatch (expected {expected_prev_hash[:8]}..., got {event.prev_hash[:8]}...)"

    expected_payload_hash = sha256_hex(jcs_canonical(event.payload))
    if event.payload_hash != expected_payload_hash:
        return False, "payload_hash does not match payload"

    expected_event_hash = sha256_hex(
        f"{event.prev_hash}|{event.payload_hash}|{event.seq}|{event.type}"
    )
    if event.event_hash != expected_event_hash:
        return False, "event_hash does not match recomputed value"

    return True, None


def walk_chain(log: AuditLog) -> VerifyResult:
    events = log.all_events()
    prev_hash = GENESIS
    expected_seq = 1
    for ev in events:
        if ev.seq != expected_seq:
            return VerifyResult(
                ok=False,
                events_checked=expected_seq - 1,
                broken_at_seq=ev.seq,
                reason=f"sequence gap: expected {expected_seq}, got {ev.seq}",
            )
        ok, reason = verify_event(ev, prev_hash)
        if not ok:
            return VerifyResult(ok=False, events_checked=expected_seq - 1, broken_at_seq=ev.seq, reason=reason)
        prev_hash = ev.event_hash
        expected_seq += 1

    return VerifyResult(ok=True, events_checked=len(events))
