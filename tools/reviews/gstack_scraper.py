"""gstack-driven review reader.

Public review pages ONLY. We scope the scraper hard:
  - robots.txt is checked first via urllib.robotparser.
  - 1 req/host/sec with jitter (the rate limiter is global per process).
  - top-20 candidates per request only.
  - results cached in SQLite (one row per URL+day-bucket) so re-runs almost
    never hit the network within 24h.

gstack is invoked as a CLI subprocess: $B goto / $B text / $B snapshot -i.
The persistent daemon is auto-started on first call.
"""

from __future__ import annotations

import asyncio
import os
import random
import shutil
import sqlite3
import subprocess
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from tools.flights.amadeus import _settings


_GSTACK_USER_AGENT = "trip-booking-concierge/0.1 (+https://example.invalid; contact: dev)"
_MIN_INTERVAL_SEC = 1.0
_MAX_JITTER_SEC = 0.5
_LAST_FETCH: dict[str, float] = {}
_LOCK = asyncio.Lock()


@dataclass
class ReviewBatch:
    hotel_id: str
    source_url: str
    fetched_at: str
    reviews: list[str]


def _gstack_binary() -> str | None:
    candidates = [
        Path.home() / ".claude/skills/gstack/browse/dist/browse",
        Path(".claude/skills/gstack/browse/dist/browse"),
    ]
    for c in candidates:
        if c.exists() and os.access(c, os.X_OK):
            return str(c)
    return shutil.which("browse")


# ---- robots.txt ------------------------------------------------------------

_ROBOTS_CACHE: dict[str, urllib.robotparser.RobotFileParser] = {}


def robots_allows(url: str, user_agent: str = _GSTACK_USER_AGENT) -> bool:
    """Return True iff robots.txt allows fetching `url` for our user-agent."""
    p = urlparse(url)
    host_key = f"{p.scheme}://{p.netloc}"
    rp = _ROBOTS_CACHE.get(host_key)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{host_key}/robots.txt")
        try:
            rp.read()
        except Exception:
            return False
        _ROBOTS_CACHE[host_key] = rp
    return rp.can_fetch(user_agent, url)


# ---- rate limit ------------------------------------------------------------

async def _rate_limit(host: str) -> None:
    async with _LOCK:
        last = _LAST_FETCH.get(host, 0.0)
        delta = time.time() - last
        wait = max(0.0, _MIN_INTERVAL_SEC - delta) + random.uniform(0.0, _MAX_JITTER_SEC)
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_FETCH[host] = time.time()


# ---- cache (SQLite) --------------------------------------------------------

_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS scraped_reviews_cache (
    url          TEXT NOT NULL,
    day_bucket   TEXT NOT NULL,
    hotel_id     TEXT NOT NULL,
    reviews_json TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (url, day_bucket)
);
"""


def _cache_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_settings().sqlite_path)
    conn.executescript(_CACHE_SCHEMA)
    return conn


def _cache_lookup(url: str) -> ReviewBatch | None:
    bucket = datetime.now(UTC).strftime("%Y-%m-%d")
    with _cache_conn() as conn:
        row = conn.execute(
            "SELECT hotel_id, reviews_json, fetched_at FROM scraped_reviews_cache "
            "WHERE url = ? AND day_bucket = ?",
            (url, bucket),
        ).fetchone()
        if not row:
            return None
        import json
        return ReviewBatch(
            hotel_id=row[0], source_url=url,
            fetched_at=row[2], reviews=json.loads(row[1]),
        )


def _cache_store(batch: ReviewBatch) -> None:
    bucket = datetime.now(UTC).strftime("%Y-%m-%d")
    import json
    with _cache_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO scraped_reviews_cache "
            "(url, day_bucket, hotel_id, reviews_json, fetched_at) VALUES (?, ?, ?, ?, ?)",
            (batch.source_url, bucket, batch.hotel_id,
             json.dumps(batch.reviews), batch.fetched_at),
        )


# ---- main entrypoint -------------------------------------------------------

async def fetch_reviews(*, hotel_id: str, public_url: str,
                         max_reviews: int = 30) -> ReviewBatch:
    """Fetch reviews for a public hotel page via gstack.

    Order of operations:
      1. Cache hit? Return immediately.
      2. robots.txt allows? Else raise.
      3. Rate-limit per host.
      4. Subprocess: $B goto, $B text → page text.
      5. Naive review extraction by paragraph splitting.
      6. Store in cache.

    Raises if gstack binary is missing or robots.txt disallows.
    """
    cached = _cache_lookup(public_url)
    if cached:
        return cached

    if not robots_allows(public_url):
        raise RuntimeError(f"robots.txt disallows {public_url} for our agent")

    bin_path = _gstack_binary()
    if not bin_path:
        raise RuntimeError(
            "gstack binary not found. Run setup at "
            "~/.claude/skills/gstack/setup or set its path on PATH."
        )

    host = urlparse(public_url).netloc
    await _rate_limit(host)

    # Use a separate event loop run to keep gstack subprocess sync.
    def _run() -> str:
        subprocess.run([bin_path, "goto", public_url], check=False, timeout=30)
        out = subprocess.run([bin_path, "text"], check=False, capture_output=True,
                             text=True, timeout=30)
        return out.stdout or ""

    page_text = await asyncio.to_thread(_run)
    reviews = _split_into_reviews(page_text)[:max_reviews]
    batch = ReviewBatch(
        hotel_id=hotel_id, source_url=public_url,
        fetched_at=datetime.now(UTC).isoformat(), reviews=reviews,
    )
    _cache_store(batch)
    return batch


def _split_into_reviews(page_text: str) -> list[str]:
    """Heuristic split: paragraphs of 60-2000 chars look like reviews.

    Real production code would use site-specific selectors (TripAdvisor's
    `[data-test-target="review-text"]`, Google Maps' review cards). For the
    prototype we use a length-based heuristic and codify per-site routines
    via gstack browser-skills (`$B skill save`) once we manually identify
    the right selectors.
    """
    paras = [p.strip() for p in page_text.split("\n\n") if p.strip()]
    return [p for p in paras if 60 <= len(p) <= 2000]
