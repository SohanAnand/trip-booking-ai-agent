"""Append-only, hash-chained audit log backed by SQLite.

Every event's hash includes the prior event's hash, so any tampering breaks the chain.
verify.py walks the chain and surfaces the first broken link.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS = "GENESIS"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def jcs_canonical(obj: Any) -> str:
    """JSON canonical serialization (subset of RFC 8785).

    Sorted keys, no extra whitespace, UTF-8. Sufficient for our hashing needs.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@dataclass
class Event:
    event_id: str
    seq: int
    request_id: str | None
    booking_id: str | None
    actor: str
    type: str
    payload: dict
    payload_hash: str
    prev_hash: str
    event_hash: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Event:
        return cls(
            event_id=row["event_id"],
            seq=row["seq"],
            request_id=row["request_id"],
            booking_id=row["booking_id"],
            actor=row["actor"],
            type=row["type"],
            payload=json.loads(row["payload"]),
            payload_hash=row["payload_hash"],
            prev_hash=row["prev_hash"],
            event_hash=row["event_hash"],
            created_at=row["created_at"],
        )


class AuditLog:
    """Thread-safe append-only event log."""

    _lock = threading.Lock()

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None)  # autocommit
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with self._connect() as conn:
            conn.executescript(sql)
            # Lightweight column migrations for existing DBs created before
            # newer columns were added. SQLite has no IF NOT EXISTS for
            # ADD COLUMN, so we probe and conditionally alter.
            cols = {row["name"] for row in conn.execute(
                "PRAGMA table_info(pending_authorizations)").fetchall()}
            if "failure_count" not in cols:
                conn.execute(
                    "ALTER TABLE pending_authorizations "
                    "ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0"
                )

    def append(
        self,
        actor: str,
        type: str,
        payload: dict,
        request_id: str | None = None,
        booking_id: str | None = None,
    ) -> Event:
        """Append a new event. Computes hash chain inside a critical section.

        Raises if the same event_id is inserted twice (guaranteed by UUID).
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT event_hash, seq FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = row["event_hash"] if row else GENESIS
            seq = (row["seq"] + 1) if row else 1

            payload_str = jcs_canonical(payload)
            payload_hash = sha256_hex(payload_str)
            event_hash = sha256_hex(f"{prev_hash}|{payload_hash}|{seq}|{type}")
            event_id = str(uuid.uuid4())
            created_at = datetime.now(UTC).isoformat()

            conn.execute(
                """INSERT INTO events
                   (event_id, seq, request_id, booking_id, actor, type, payload,
                    payload_hash, prev_hash, event_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id, seq, request_id, booking_id, actor, type,
                    payload_str, payload_hash, prev_hash, event_hash, created_at,
                ),
            )
            return Event(
                event_id=event_id, seq=seq, request_id=request_id, booking_id=booking_id,
                actor=actor, type=type, payload=payload, payload_hash=payload_hash,
                prev_hash=prev_hash, event_hash=event_hash, created_at=created_at,
            )

    def all_events(self) -> list[Event]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY seq ASC").fetchall()
            return [Event.from_row(r) for r in rows]

    def events_for_request(self, request_id: str) -> list[Event]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE request_id = ? ORDER BY seq ASC",
                (request_id,),
            ).fetchall()
            return [Event.from_row(r) for r in rows]

    def events_for_booking(self, booking_id: str) -> list[Event]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE booking_id = ? ORDER BY seq ASC",
                (booking_id,),
            ).fetchall()
            return [Event.from_row(r) for r in rows]

    def consume_jti(self, jti: str, booking_id: str | None = None) -> bool:
        """Atomically claim a jti. Returns True if first consumer; False if already used."""
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO consumed_jti (jti, consumed_at, booking_id) VALUES (?, ?, ?)",
                    (jti, datetime.now(UTC).isoformat(), booking_id),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def record_tool_call(
        self,
        tool_name: str,
        args: dict,
        result: dict | None,
        request_id: str,
        latency_ms: int,
        status: str,
        cost_cents: int = 0,
    ) -> str:
        tool_call_id = str(uuid.uuid4())
        result_str = jcs_canonical(result) if result is not None else None
        result_hash = sha256_hex(result_str) if result_str else None
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO tool_calls
                   (id, request_id, tool_name, args, result, result_hash,
                    latency_ms, cost_cents, status, started_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tool_call_id, request_id, tool_name, jcs_canonical(args), result_str,
                    result_hash, latency_ms, cost_cents, status, now, now,
                ),
            )
        return tool_call_id

    def get_tool_call(self, tool_call_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tool_calls WHERE id = ?", (tool_call_id,)
            ).fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "request_id": row["request_id"],
                "tool_name": row["tool_name"],
                "args": json.loads(row["args"]),
                "result": json.loads(row["result"]) if row["result"] else None,
                "result_hash": row["result_hash"],
                "status": row["status"],
            }

    def store_option_snapshot(self, option_id: str, request_id: str, rank: int, snapshot: dict) -> str:
        snapshot_str = jcs_canonical(snapshot)
        snapshot_hash = sha256_hex(snapshot_str)
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO itinerary_options
                   (id, request_id, rank, snapshot, snapshot_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (option_id, request_id, rank, snapshot_str, snapshot_hash,
                 datetime.now(UTC).isoformat()),
            )
        return snapshot_hash

    def get_option_snapshot(self, option_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT snapshot, snapshot_hash FROM itinerary_options WHERE id = ?",
                (option_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "snapshot": json.loads(row["snapshot"]),
                "snapshot_hash": row["snapshot_hash"],
            }

    def get_options_for_request(self, request_id: str) -> list[dict]:
        """Fetch every option-snapshot for a request, in rank order. Used by
        the API to re-hydrate `_request_options` after a server restart so
        the user's /select and /approve calls don't 404."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, snapshot, snapshot_hash, rank "
                "FROM itinerary_options WHERE request_id = ? ORDER BY rank",
                (request_id,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "rank": r["rank"],
                "snapshot": json.loads(r["snapshot"]),
                "snapshot_hash": r["snapshot_hash"],
            }
            for r in rows
        ]

    def upsert_booking(self, booking: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO bookings
                   (id, request_id, option_id, option_hash, state, total_cents,
                    currency, idempotency_key, created_at, updated_at)
                   VALUES (:id, :request_id, :option_id, :option_hash, :state,
                           :total_cents, :currency, :idempotency_key,
                           :created_at, :updated_at)
                   ON CONFLICT(id) DO UPDATE SET
                     state=excluded.state, updated_at=excluded.updated_at""",
                booking,
            )

    def get_booking(self, booking_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM bookings WHERE id = ?", (booking_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_booking_by_idempotency_key(self, key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM bookings WHERE idempotency_key = ?", (key,)
            ).fetchone()
            return dict(row) if row else None

    def store_pending_authorization(self, pending: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO pending_authorizations
                   (option_id, code, issued_at, user_id, option_hash,
                    consent_text, consent_text_hash, payment_method_id,
                    request_id, amount_cents, currency)
                   VALUES (:option_id, :code, :issued_at, :user_id, :option_hash,
                           :consent_text, :consent_text_hash, :payment_method_id,
                           :request_id, :amount_cents, :currency)""",
                pending,
            )

    def take_pending_authorization(self, option_id: str) -> dict | None:
        """Atomically read+delete a pending authorization (single-use)."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_authorizations WHERE option_id = ?",
                (option_id,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "DELETE FROM pending_authorizations WHERE option_id = ?",
                (option_id,),
            )
            return dict(row)

    def consume_pending_if_match(self, option_id: str, code: str) -> dict | None:
        """Atomic check-and-consume: DELETE the pending row only if its code
        matches. Returns the row dict if consumed, None otherwise.

        Closes the peek-then-take race in the approval gate where two parallel
        `/approve` calls with the same correct code could both pass the
        compare_digest check; the loser's `take` would then return None and
        we'd incorrectly raise "already consumed" instead of letting the
        winner mint cleanly.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_authorizations "
                "WHERE option_id = ? AND code = ?",
                (option_id, code),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "DELETE FROM pending_authorizations "
                "WHERE option_id = ? AND code = ?",
                (option_id, code),
            )
            return dict(row)

    def peek_pending_authorization(self, option_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_authorizations WHERE option_id = ?",
                (option_id,),
            ).fetchone()
            return dict(row) if row else None

    def bump_pending_failure(self, option_id: str) -> int:
        """Increment failure_count on a pending authorization and return the
        new count. Used by the approval gate to lock out brute-force OTP
        guessing after N failed attempts."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE pending_authorizations "
                "SET failure_count = failure_count + 1 WHERE option_id = ?",
                (option_id,),
            )
            row = conn.execute(
                "SELECT failure_count FROM pending_authorizations WHERE option_id = ?",
                (option_id,),
            ).fetchone()
            return row["failure_count"] if row else 0


# Module-level convenience: a default log singleton bound to env-configured path.
_default_log: AuditLog | None = None
_default_lock = threading.Lock()


def get_default_log() -> AuditLog:
    global _default_log
    if _default_log is None:
        with _default_lock:
            if _default_log is None:
                from api.config import settings
                _default_log = AuditLog(settings.sqlite_path)
    return _default_log


def append_event(actor: str, type: str, payload: dict, **kwargs) -> Event:
    return get_default_log().append(actor, type, payload, **kwargs)
