"""Cross-session user-profile memory.

Stores rolling preferences derived from past bookings: refundable share,
average star rating, last-N neighborhoods. Profile is loaded at the start
of every agent run and injected into both the intent prompt and the
agentic-loop system prompt, so the LLM applies a returning user's prior
choices when nothing in the current request conflicts.

Profile updates happen post-commit in api.main, not speculatively from
search activity. Memory tracks what the user actually paid for, not what
the agent considered.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from api.config import settings


_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id            TEXT PRIMARY KEY,
    booking_count      INTEGER NOT NULL DEFAULT 0,
    last_booked_at     INTEGER,
    pref_refundable    REAL,
    pref_avg_star      REAL,
    pref_neighborhoods TEXT NOT NULL DEFAULT '[]',
    pref_destinations  TEXT NOT NULL DEFAULT '[]',
    notes              TEXT NOT NULL DEFAULT ''
);
"""

_LOCK = threading.Lock()


@dataclass
class UserProfile:
    user_id: str
    booking_count: int = 0
    last_booked_at: int | None = None
    pref_refundable: float | None = None    # 0..1 share of past bookings refundable
    pref_avg_star: float | None = None
    pref_neighborhoods: list[str] = field(default_factory=list)
    pref_destinations: list[str] = field(default_factory=list)
    notes: str = ""

    def to_prompt_summary(self) -> str:
        """One paragraph the LLM can read. Empty string for new users so the
        prompt simply omits the section rather than saying 'no history.'"""
        if self.booking_count == 0:
            return ""
        bits: list[str] = [f"Returning user with {self.booking_count} prior booking(s)."]
        if self.pref_refundable is not None:
            if self.pref_refundable >= 0.7:
                bits.append("Strongly prefers refundable fares (paid for refundability "
                            f"on {self.pref_refundable:.0%} of past trips).")
            elif self.pref_refundable <= 0.3:
                bits.append("Tends to skip refundable upgrades to save money.")
        if self.pref_avg_star is not None:
            bits.append(f"Average past hotel rating: {self.pref_avg_star:.1f} stars.")
        if self.pref_neighborhoods:
            bits.append(f"Recent stays: {', '.join(self.pref_neighborhoods[-3:])}.")
        if self.pref_destinations:
            bits.append(f"Recent destinations: {', '.join(self.pref_destinations[-3:])}.")
        if self.notes:
            bits.append(self.notes)
        return " ".join(bits)


_PROFILE_SCHEMA_VERSION = 1


def _migrate_profile_schema(conn: sqlite3.Connection) -> None:
    """Versioned migrations for the user_profiles table. SQLite's
    `CREATE TABLE IF NOT EXISTS` won't add columns to an existing table, so
    when the schema gains a column we need an explicit ALTER TABLE here.
    """
    cur_version = conn.execute("PRAGMA user_version").fetchone()[0]
    # Below this comment, future schema bumps land as: if cur_version < N: ...
    # For now there's only one version; the block exists so adding a column
    # later is a one-line change.
    if cur_version < _PROFILE_SCHEMA_VERSION:
        # No-op for now (table is created fresh by _TABLE_SQL).
        conn.execute(f"PRAGMA user_version = {_PROFILE_SCHEMA_VERSION}")


def _connect() -> sqlite3.Connection:
    Path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.sqlite_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_TABLE_SQL)
    _migrate_profile_schema(conn)
    return conn


def load_profile(user_id: str) -> UserProfile:
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,),
            ).fetchone()
    if not row:
        return UserProfile(user_id=user_id)
    return UserProfile(
        user_id=user_id,
        booking_count=row["booking_count"] or 0,
        last_booked_at=row["last_booked_at"],
        pref_refundable=row["pref_refundable"],
        pref_avg_star=row["pref_avg_star"],
        pref_neighborhoods=json.loads(row["pref_neighborhoods"] or "[]"),
        pref_destinations=json.loads(row["pref_destinations"] or "[]"),
        notes=row["notes"] or "",
    )


def _dedupe_window(existing: list[str], new_items: list[str], cap: int = 5) -> list[str]:
    """Append new_items to existing, dedupe while preserving order, keep last cap.

    Without dedupe a user who books Alfama 5 times in a row ends up with a
    list of 5 identical entries — the prompt summary loses signal diversity.
    """
    combined = existing + [x for x in new_items if x]
    seen: set[str] = set()
    deduped: list[str] = []
    for item in combined:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped[-cap:]


def update_profile_after_booking(
    user_id: str, *,
    refundable: bool,
    avg_star: float,
    neighborhoods: list[str],
    destinations: list[str],
) -> None:
    """Roll a successful booking into the user's profile.

    Called from api.main.approve_and_book after the booking commits. Uses
    an atomic upsert under BEGIN IMMEDIATE so two parallel bookings can't
    each read the same `n_old` and overwrite each other (the in-process
    threading.Lock didn't help across multiple uvicorn workers; the single
    SQLite transaction does).
    """
    with _LOCK:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,),
                ).fetchone()
                now = int(time.time())
                if existing is None:
                    conn.execute(
                        "INSERT INTO user_profiles "
                        "(user_id, booking_count, last_booked_at, pref_refundable, "
                        " pref_avg_star, pref_neighborhoods, pref_destinations, notes) "
                        "VALUES (?, 1, ?, ?, ?, ?, ?, '')",
                        (user_id, now,
                         1.0 if refundable else 0.0, avg_star,
                         json.dumps(_dedupe_window([], neighborhoods)),
                         json.dumps(_dedupe_window([], destinations))),
                    )
                    conn.execute("COMMIT")
                    return
                n_old = existing["booking_count"] or 0
                n_new = n_old + 1
                old_ref = existing["pref_refundable"] if existing["pref_refundable"] is not None else 0.0
                old_star = existing["pref_avg_star"] if existing["pref_avg_star"] is not None else 0.0
                new_ref = ((old_ref * n_old) + (1.0 if refundable else 0.0)) / n_new
                new_star = ((old_star * n_old) + avg_star) / n_new
                existing_n = json.loads(existing["pref_neighborhoods"] or "[]")
                existing_d = json.loads(existing["pref_destinations"] or "[]")
                merged_n = _dedupe_window(existing_n, neighborhoods)
                merged_d = _dedupe_window(existing_d, destinations)
                conn.execute(
                    "UPDATE user_profiles SET "
                    "booking_count=?, last_booked_at=?, pref_refundable=?, "
                    "pref_avg_star=?, pref_neighborhoods=?, pref_destinations=? "
                    "WHERE user_id=?",
                    (n_new, now, new_ref, new_star,
                     json.dumps(merged_n), json.dumps(merged_d), user_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
