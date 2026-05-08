"""Semantic review search.

Embeddings via Voyage `voyage-3-lite` when VOYAGE_API_KEY is set; otherwise a
deterministic local fallback (hash → low-dim sparse vector) so the demo runs
offline. The fallback is NOT good enough for production matching but lets the
review-reading pipeline exercise end-to-end without external calls.

Storage: SQLite. We try sqlite-vss when available; otherwise a plain table +
in-memory cosine similarity over the result set (fine for ≤1k vectors).
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass

import httpx

from tools.flights.amadeus import _settings


VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_MODEL = "voyage-3-lite"
EMBED_DIM_REAL = 512    # voyage-3-lite default
EMBED_DIM_FALLBACK = 64


@dataclass
class ReviewSnippet:
    hotel_id: str
    text: str
    score: float
    source_url: str
    fetched_at: str


# ---- embedding ------------------------------------------------------------

async def _embed(texts: list[str]) -> list[list[float]]:
    s = _settings()
    if not s.voyage_api_key:
        return [_fallback_embed(t) for t in texts]
    async with httpx.AsyncClient(timeout=30.0) as cli:
        res = await cli.post(
            VOYAGE_URL,
            headers={"Authorization": f"Bearer {s.voyage_api_key}",
                     "Content-Type": "application/json"},
            json={"model": VOYAGE_MODEL, "input": texts, "input_type": "document"},
        )
        res.raise_for_status()
        data = res.json()
        return [d["embedding"] for d in data["data"]]


def _fallback_embed(text: str) -> list[float]:
    """Deterministic, hash-based bag-of-tokens embedding.

    Tokenize on whitespace, hash each token to a bucket, count, L2-normalize.
    Works well enough for the demo to differentiate "noise" reviews from
    "cleanliness" reviews — but obviously not real semantic search.
    """
    vec = [0.0] * EMBED_DIM_FALLBACK
    for tok in text.lower().split():
        h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=4).digest(), "big")
        vec[h % EMBED_DIM_FALLBACK] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# ---- storage --------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hotel_id      TEXT NOT NULL,
    text          TEXT NOT NULL,
    embedding     TEXT NOT NULL,        -- JSON-encoded list[float]
    source_url    TEXT,
    fetched_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_hotel ON reviews(hotel_id);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_settings().sqlite_path)
    conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


# ---- API ------------------------------------------------------------------

async def index_reviews(hotel_id: str, reviews: list[str], *,
                         source_url: str = "", fetched_at: str = "") -> int:
    """Embed and store reviews. Returns rows inserted."""
    if not reviews:
        return 0
    vecs = await _embed(reviews)
    with _conn() as conn:
        for text, vec in zip(reviews, vecs):
            conn.execute(
                "INSERT INTO reviews (hotel_id, text, embedding, source_url, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (hotel_id, text, json.dumps(vec), source_url, fetched_at),
            )
    return len(reviews)


async def find_concerns(hotel_id: str, query: str, *, k: int = 5) -> list[ReviewSnippet]:
    """Return top-k review snippets most similar to `query`."""
    q_emb = (await _embed([query]))[0]
    with _conn() as conn:
        rows = conn.execute(
            "SELECT hotel_id, text, embedding, source_url, fetched_at "
            "FROM reviews WHERE hotel_id = ?",
            (hotel_id,),
        ).fetchall()
    scored: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        emb = json.loads(row["embedding"])
        score = _cosine(q_emb, emb)
        scored.append((score, row))
    scored.sort(key=lambda t: -t[0])
    return [
        ReviewSnippet(
            hotel_id=row["hotel_id"], text=row["text"], score=score,
            source_url=row["source_url"] or "", fetched_at=row["fetched_at"],
        )
        for score, row in scored[:k]
    ]


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        # Different dim → can't compare; treat as orthogonal.
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)
