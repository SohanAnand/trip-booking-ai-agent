"""Semantic review indexing + retrieval, using the offline fallback embedding."""

from __future__ import annotations

import pytest

from reviews.semantic import find_concerns, index_reviews


@pytest.mark.asyncio
async def test_indexed_reviews_retrievable():
    n = await index_reviews(
        "AMA-ABCDE",
        [
            "The walls are paper-thin and street noise kept me up all night.",
            "Loved the rooftop bar and the staff were exceptional.",
            "Very clean rooms, fresh towels every day, spotless bathroom.",
            "The breakfast buffet was disappointing and overpriced.",
        ],
    )
    assert n == 4

    # Query targeting noise should rank the first review highest.
    snippets = await find_concerns("AMA-ABCDE", "noise sleep walls", k=4)
    assert len(snippets) == 4
    # Top result mentions noise / walls / sleep.
    top = snippets[0].text.lower()
    assert "noise" in top or "walls" in top or "kept me up" in top


@pytest.mark.asyncio
async def test_query_isolated_per_hotel():
    await index_reviews("AMA-A", ["loud music until 3am"])
    await index_reviews("AMA-B", ["pin-drop quiet, slept beautifully"])
    only_a = await find_concerns("AMA-A", "noise")
    assert all(s.hotel_id == "AMA-A" for s in only_a)
