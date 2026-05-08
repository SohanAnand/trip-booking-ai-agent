"""Prompt-injection defense for review content fed into the LLM.

Two-stage defense:
  1. Wrap every review in <review id=...>...</review> blocks. The system prompt
     instructs the model to treat <review> content as DATA, not instructions.
  2. Post-process: if the LLM emits a tool_request that targets a hotel
     mentioned ONLY inside a <review> block (i.e., not in any structured
     hotel-search result), refuse with a guard event.
"""

from __future__ import annotations

import re

REVIEW_OPEN = re.compile(r"<review\s+id=['\"]([^'\"]+)['\"]\s*>", re.IGNORECASE)
REVIEW_CLOSE = re.compile(r"</review>", re.IGNORECASE)


def wrap_reviews(reviews: list[tuple[str, str]]) -> str:
    """Build a single string of <review id=R1>...</review><review id=R2>...</review>.

    Defensively strips any inner </review> tags from the raw text to prevent
    injection of "I'm done with the data block, now obey me" attacks.
    """
    parts = []
    for rid, text in reviews:
        sanitized = REVIEW_CLOSE.sub("&lt;/review&gt;", text)
        parts.append(f"<review id='{rid}'>{sanitized}</review>")
    return "\n".join(parts)


def hotel_was_only_in_reviews(
    hotel_id: str,
    *,
    structured_hotel_ids: set[str],
    review_text: str,
) -> bool:
    """True iff hotel_id is mentioned in review text but NOT in structured results.

    The orchestrator calls this before honoring any tool_request that names a
    hotel; it refuses bookings of "review-only" hotels.
    """
    if hotel_id in structured_hotel_ids:
        return False
    return hotel_id in review_text
