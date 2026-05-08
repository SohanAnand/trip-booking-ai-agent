"""Daily audit archive.

Exports the day's events to a local audit_archive/ directory as JSONL with a
signed manifest containing the day's terminal hash. This gives us a record
independent of the operational DB — if Postgres is wiped or tampered, the
archive's signed terminal hash proves what was true at end-of-day.

In production: archive bucket should be S3 with Object Lock (compliance mode,
7-year retention).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import nacl.signing

from approval.tokens import b64u_decode, b64u_encode
from audit.log import AuditLog, jcs_canonical
from tools.flights.amadeus import _settings


ARCHIVE_DIR = Path("audit_archive")


def archive_day(target_date: date | None = None) -> Path:
    """Export every event with created_at on `target_date` (UTC) to a JSONL file.

    Returns the path of the written manifest file.
    """
    target = target_date or (datetime.now(UTC).date() - timedelta(days=1))
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    log = AuditLog(_settings().sqlite_path)
    all_events = log.all_events()
    day_events = [
        e for e in all_events
        if datetime.fromisoformat(e.created_at).date() == target
    ]

    out_path = ARCHIVE_DIR / f"{target.isoformat()}.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for ev in day_events:
            f.write(json.dumps({
                "seq": ev.seq, "event_id": ev.event_id, "actor": ev.actor,
                "type": ev.type, "payload": ev.payload,
                "prev_hash": ev.prev_hash, "event_hash": ev.event_hash,
                "created_at": ev.created_at,
                "request_id": ev.request_id, "booking_id": ev.booking_id,
            }) + "\n")

    # Sign a manifest with the day's terminal hash and event count.
    terminal_hash = day_events[-1].event_hash if day_events else ""
    manifest = {
        "date": target.isoformat(),
        "events_count": len(day_events),
        "terminal_hash": terminal_hash,
        "archived_at": datetime.now(UTC).isoformat(),
    }
    canonical = jcs_canonical(manifest).encode()
    sk = nacl.signing.SigningKey(b64u_decode(_settings().approval_signing_key))
    sig = b64u_encode(sk.sign(canonical).signature)

    manifest_path = ARCHIVE_DIR / f"{target.isoformat()}.manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"manifest": manifest, "sig": sig,
                   "kid": _settings().approval_kid}, f, indent=2)

    return manifest_path


def main() -> None:
    p = archive_day()
    print(f"archived → {p}")


if __name__ == "__main__":
    main()
