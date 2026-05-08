-- Append-only event log with hash chain.
-- prev_hash + event_hash form a tamper-evident chain: changing any payload breaks the chain
-- and audit/verify.py flags the broken link.

CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,    -- UUIDv4 string
    seq           INTEGER NOT NULL,    -- monotonic sequence per chain
    request_id    TEXT,                -- optional FK to agent request
    booking_id    TEXT,                -- optional FK to booking
    actor         TEXT NOT NULL,       -- "user", "agent", "system", "approval-service", etc.
    type          TEXT NOT NULL,       -- "tool.called", "approval.signed", etc.
    payload       TEXT NOT NULL,       -- JCS-canonical JSON
    payload_hash  TEXT NOT NULL,       -- sha256(payload)
    prev_hash     TEXT NOT NULL,       -- hash of previous event in chain (or "GENESIS")
    event_hash    TEXT NOT NULL,       -- sha256(prev_hash || payload_hash || seq || type)
    created_at    TEXT NOT NULL        -- ISO8601
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_events_seq ON events(seq);
CREATE INDEX IF NOT EXISTS idx_events_request ON events(request_id);
CREATE INDEX IF NOT EXISTS idx_events_booking ON events(booking_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);

-- Track consumed token JTIs for replay defense.
CREATE TABLE IF NOT EXISTS consumed_jti (
    jti          TEXT PRIMARY KEY,
    consumed_at  TEXT NOT NULL,
    booking_id   TEXT
);

-- Tool call records. The LLM's view of facts is by ToolCall.id; the orchestrator
-- fills user-visible facts from these rows, never from the LLM's narrative.
CREATE TABLE IF NOT EXISTS tool_calls (
    id            TEXT PRIMARY KEY,
    request_id    TEXT NOT NULL,
    tool_name     TEXT NOT NULL,
    args          TEXT NOT NULL,
    result        TEXT,
    result_hash   TEXT,
    latency_ms    INTEGER,
    cost_cents    INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    completed_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_request ON tool_calls(request_id);

-- Booking holds and commits.
CREATE TABLE IF NOT EXISTS bookings (
    id                TEXT PRIMARY KEY,
    request_id        TEXT NOT NULL,
    option_id         TEXT NOT NULL,
    option_hash       TEXT NOT NULL,
    state             TEXT NOT NULL,    -- PROPOSED / HELD / AUTHORIZED / COMMITTING / COMMITTED / COMPENSATED / FAILED
    total_cents       INTEGER NOT NULL,
    currency          TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- Itinerary options presented to the user (snapshot at presentation time).
CREATE TABLE IF NOT EXISTS itinerary_options (
    id              TEXT PRIMARY KEY,
    request_id      TEXT NOT NULL,
    rank            INTEGER NOT NULL,
    snapshot        TEXT NOT NULL,    -- JCS-canonical full option blob
    snapshot_hash   TEXT NOT NULL,    -- sha256(snapshot) — bound into ApprovalToken
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_options_request ON itinerary_options(request_id);

-- Pending authorizations (M1 OTP flow). Single-use per option.
-- M4 replaces with Redis-backed pending state.
CREATE TABLE IF NOT EXISTS pending_authorizations (
    option_id            TEXT PRIMARY KEY,
    code                 TEXT NOT NULL,
    issued_at            INTEGER NOT NULL,
    user_id              TEXT NOT NULL,
    option_hash          TEXT NOT NULL,
    consent_text         TEXT NOT NULL,
    consent_text_hash    TEXT NOT NULL,
    payment_method_id    TEXT NOT NULL,
    request_id           TEXT NOT NULL,
    amount_cents         INTEGER NOT NULL,
    currency             TEXT NOT NULL,
    failure_count        INTEGER NOT NULL DEFAULT 0
);
