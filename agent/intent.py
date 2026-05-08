"""LLM-based intent extraction for trip requests.

Uses Anthropic Haiku (cheap) with structured output. The model returns a JSON
object matching IntentSchema; we validate via Pydantic. If MOCK_LLM=1 OR no
ANTHROPIC_API_KEY is set, falls back to a regex parser below.

The LLM here is treated as a parser, not a fact-source. The only thing it
extracts is what the USER said. Hard facts (prices, availability) come later
from tool calls.

Multi-leg support: a single user message can describe multiple destinations
(e.g. "London and New York with separate budgets"). The schema returns a
`legs` array and mirrors the first leg into the legacy flat fields for code
that hasn't been migrated yet.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from pydantic import BaseModel, Field

from api.config import settings


class LegIntent(BaseModel):
    """One leg of a multi-city trip."""
    destination: str | None = Field(None, description="IATA city code or city name")
    date_start: str | None = Field(None, description="ISO date YYYY-MM-DD")
    date_end: str | None = Field(None, description="ISO date YYYY-MM-DD")
    budget_min_usd: float | None = None
    budget_max_usd: float | None = None
    traveler_count: int = 1


class IntentSchema(BaseModel):
    origin: str | None = Field(None, description="IATA airport code, e.g. JFK")
    legs: list[LegIntent] = Field(default_factory=list)
    # Legacy flat fields. Mirror legs[0] when only one leg is parsed.
    destination: str | None = None
    date_start: str | None = None
    date_end: str | None = None
    date_flexibility_days: int = 0
    traveler_count: int = 1
    budget_total_usd: float | None = None
    is_one_way: bool = False    # user said "one-way", "no return", "moving to"
    hard_constraints: dict = Field(default_factory=dict)
    soft_preferences: dict = Field(default_factory=dict)


INTENT_PROMPT = """\
You are an intent parser for a travel concierge. Extract a structured trip
request from the user's message. Output ONLY a JSON object matching this schema:

{
  "origin": "JFK" | null,
  "legs": [
    {
      "destination": "LIS",
      "date_start": "YYYY-MM-DD" | null,
      "date_end": "YYYY-MM-DD" | null,
      "budget_min_usd": 5000.00 | null,
      "budget_max_usd": 10000.00 | null,
      "traveler_count": 1
    }
  ],
  "destination": "LIS" | null,
  "date_start": "YYYY-MM-DD" | null,
  "date_end": "YYYY-MM-DD" | null,
  "date_flexibility_days": 0,
  "traveler_count": 1,
  "budget_total_usd": 2000.00 | null,
  "hard_constraints": {},
  "soft_preferences": {}
}

Rules for legs:
- One entry per distinct destination the user mentions. Order them in the
  order the user wants to visit (or the natural reading order).
- Use IATA city codes when you know them (LON, NYC, LAX, TYO, PAR, BCN, etc.).
- "minimum/at least/around/about $X" sets budget_min_usd = X.
- "under/below/less than $X" or "max $X" sets budget_max_usd = X.
- "$X total" or "exactly $X" sets both budget_min_usd and budget_max_usd to X.
- If the user gives one combined budget for the whole trip, also fill
  budget_total_usd at the top level. Per-leg budgets can still be in legs[].

Rules for dates:
- "next month" sets start to the first day of next month from today.
- "in two weeks" sets start to today + 14 days.
- "this weekend" sets start to the next Saturday.
- "starting July 5", "from July 5", "July 5" sets a specific start date in
  ISO YYYY-MM-DD using today as the year anchor.
- Explicit ISO dates pass through verbatim.
- "X days" makes end = start + X days.

CRITICAL rules for multi-leg date allocation. Read carefully:

A traveler can only be in ONE city at a time. Two legs must NEVER share the
same date_start or overlap. Always sequence legs back-to-back.

Pattern A: per-leg durations given.
  Input: "X days in CityA and Y days in CityB starting MMM D"
  Output:
    legs[0].date_start = MMM D
    legs[0].date_end   = MMM D + X days
    legs[1].date_start = legs[0].date_end          (the day they fly out)
    legs[1].date_end   = legs[0].date_end + Y days

  Concrete example with "10 days in London and 5 days in New York, starting July 5":
    legs[0]: London,   date_start=2026-07-05, date_end=2026-07-15  (10 days)
    legs[1]: New York, date_start=2026-07-15, date_end=2026-07-20  (5 days)

Pattern B: total duration only, no per-leg split given.
  Input: "Z days split between CityA and CityB starting MMM D"
  Divide Z evenly across legs (round leg 0 up if odd).
  Example with "15 days in London and New York starting July 5":
    legs[0]: London,   date_start=2026-07-05, date_end=2026-07-13  (8 days)
    legs[1]: New York, date_start=2026-07-13, date_end=2026-07-20  (7 days)

Pattern C: explicit per-leg dates given for each.
  Pass them through verbatim.

Do NOT copy the trip's overall date range into every leg. That is wrong.
Each leg's date_start is the day they arrive in that city. Each leg's
date_end is the day they leave it (which is the next leg's date_start).

Rules for the legacy flat fields:
- destination, date_start, date_end mirror legs[0] when there is exactly one
  leg. Leave them null when there are multiple legs.

Rules for traveler_count:
- Default to 1. "we" or "couple" sets it to 2. "family of N" sets it to N.

Rules for constraints:
- "with pool", "walkable", "near the beach" go in soft_preferences.
- "must be refundable", "no red-eye", "non-stop only" go in hard_constraints.

Rules for is_one_way:
- Set is_one_way=true when the user says "one-way", "no return",
  "moving to", "relocating to", or otherwise signals they don't want a
  return flight back to home origin.
- Default is_one_way=false (round-trip with a return flight back home).

If a field is unstated, use null (not "" or 0). Output JSON only, no prose,
no code fences.
"""


async def extract_intent(
    raw_text: str,
    *,
    today: date | None = None,
    profile_summary: str = "",
) -> IntentSchema:
    """Parse raw_text into an IntentSchema. Falls back to regex if MOCK_LLM=1.

    profile_summary: a one-paragraph hint about the user's past behavior
    (refundable share, average star rating, recent neighborhoods). Injected
    into the LLM system prompt when present so ambiguous requests can be
    resolved against the user's history. The regex path ignores it.
    """
    today = today or date.today()
    if settings.mock_llm or not settings.anthropic_api_key:
        print(f"[intent] using regex fallback "
              f"(mock_llm={settings.mock_llm}, has_key={bool(settings.anthropic_api_key)})",
              flush=True)
        return _regex_fallback(raw_text, today)
    try:
        result = await _llm_extract(raw_text, today, profile_summary=profile_summary)
        print(f"[intent] LLM extracted {len(result.legs)} leg(s): "
              f"{[L.destination for L in result.legs]}", flush=True)
        return result
    except Exception as e:
        print(f"[intent] LLM call failed, falling back to regex: "
              f"{type(e).__name__}: {e}", flush=True)
        return _regex_fallback(raw_text, today)


# ---- LLM path -------------------------------------------------------------

async def _llm_extract(
    raw_text: str, today: date, *, profile_summary: str = "",
) -> IntentSchema:
    import anthropic
    cli = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    system = INTENT_PROMPT + f"\n\nToday's date is {today.isoformat()}."
    if profile_summary:
        system += (
            f"\n\nUser history (apply only when nothing in the message "
            f"conflicts):\n{profile_summary}"
        )
    msg = await cli.messages.create(
        model=settings.llm_model_fast,
        max_tokens=600,
        system=system,
        messages=[{"role": "user", "content": raw_text}],
    )
    body = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1].rsplit("```", 1)[0]
    parsed = IntentSchema(**json.loads(body))
    return _backfill_flat_from_legs(parsed)


# ---- Regex fallback (deterministic) ---------------------------------------

# IATA city codes. Order matters: multi-word entries first so "new york"
# matches before "york" would, if it were ever added.
CITY_MAP = {
    "lisbon": "LIS", "lisboa": "LIS",
    "porto": "OPO",
    "madrid": "MAD",
    "barcelona": "BCN",
    "rome": "ROM", "roma": "ROM",
    "milan": "MIL", "milano": "MIL",
    "paris": "PAR",
    "london": "LON",
    "amsterdam": "AMS",
    "berlin": "BER",
    "munich": "MUC",
    "vienna": "VIE",
    "prague": "PRG",
    "athens": "ATH",
    "istanbul": "IST",
    "dubai": "DXB",
    "tokyo": "TYO",
    "osaka": "OSA",
    "seoul": "SEL",
    "bangkok": "BKK",
    "singapore": "SIN",
    "hong kong": "HKG",
    "sydney": "SYD",
    "melbourne": "MEL",
    "new york": "NYC", "nyc": "NYC",
    "los angeles": "LAX",
    "san francisco": "SFO",
    "chicago": "CHI",
    "miami": "MIA",
    "boston": "BOS",
    "toronto": "YTO",
    "vancouver": "YVR",
    "mexico city": "MEX",
}

MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


_ONE_WAY_RE = re.compile(
    r"\bone[-\s]way\b|\bno return\b|\bmoving to\b|\brelocating to\b|\brelocate to\b",
    re.IGNORECASE,
)


def _detect_one_way(raw: str) -> bool:
    return bool(_ONE_WAY_RE.search(raw))


def _regex_fallback(raw_text: str, today: date) -> IntentSchema:
    raw = raw_text.lower()

    destination = None
    for needle, code in CITY_MAP.items():
        if needle in raw:
            destination = code
            break

    m = re.search(r"(\d+)\s*days?", raw)
    days = int(m.group(1)) if m else 4

    budget_min, budget_max = _parse_budgets(raw)
    start = _parse_start_date(raw, today)
    end = start + timedelta(days=days)

    leg = LegIntent(
        destination=destination,
        date_start=start.isoformat(),
        date_end=end.isoformat(),
        budget_min_usd=budget_min,
        budget_max_usd=budget_max,
        traveler_count=1,
    )
    return IntentSchema(
        origin="JFK",
        legs=[leg],
        destination=destination,
        date_start=start.isoformat(),
        date_end=end.isoformat(),
        traveler_count=1,
        budget_total_usd=budget_max if budget_max is not None else budget_min,
        is_one_way=_detect_one_way(raw_text),
    )


def _parse_budgets(raw: str) -> tuple[float | None, float | None]:
    """Returns (min, max). Handles 'at least $X', 'minimum $X', 'around $X',
    'under $X', '$X total', and explicit ranges '$X-$Y'."""
    budget_min: float | None = None
    budget_max: float | None = None

    # Explicit range: "$5,000-$10,000" or "$5000 to $10000"
    m = re.search(
        r"\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:-|to)\s*\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)",
        raw,
    )
    if m:
        a = float(m.group(1).replace(",", ""))
        b = float(m.group(2).replace(",", ""))
        # Sanity: reject if either side looks like a year or a date pair
        if a >= 100 and b >= 100:
            return min(a, b), max(a, b)

    # Floor: "at least $X", "minimum $X", "min $X", "spend $X"
    m = re.search(
        r"(?:at\s*least|minimum|min(?:imum)?|spend(?:ing)?)\s*\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)",
        raw,
    )
    if m:
        budget_min = float(m.group(1).replace(",", ""))

    # Ceiling: "under $X", "below $X", "less than $X", "max $X", "no more than $X"
    m = re.search(
        r"(?:under|below|less\s*than|max(?:imum)?|no\s*more\s*than)\s*\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)",
        raw,
    )
    if m:
        budget_max = float(m.group(1).replace(",", ""))

    # Around: "around $X", "about $X", "roughly $X" sets a soft range +/- 10%
    if budget_min is None and budget_max is None:
        m = re.search(
            r"(?:around|about|roughly|approximately|approx\.?)\s*\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)",
            raw,
        )
        if m:
            v = float(m.group(1).replace(",", ""))
            budget_min = v * 0.9
            budget_max = v * 1.1

    return budget_min, budget_max


def _parse_start_date(raw: str, today: date) -> date:
    """Handles ISO YYYY-MM-DD, 'Month Day' / 'starting Month Day', 'next month',
    'this weekend', and a default of today + 30 days."""
    # Explicit ISO date
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # "starting July 5" / "from July 5" / "July 5"
    pattern = (
        r"(?:starting|start(?:s|ing)?(?:\s+on)?|from|on|in)?\s*"
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})(?:st|nd|rd|th)?"
    )
    m = re.search(pattern, raw)
    if m:
        month_name = m.group(1)[:3]
        day = int(m.group(2))
        month = MONTH_NAMES.get(month_name)
        if month and 1 <= day <= 31:
            year = today.year
            try:
                candidate = date(year, month, day)
                if candidate < today:
                    candidate = date(year + 1, month, day)
                return candidate
            except ValueError:
                pass

    if "next month" in raw:
        return (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    if "this weekend" in raw:
        return today + timedelta(days=(5 - today.weekday()) % 7 or 7)
    if "in two weeks" in raw:
        return today + timedelta(days=14)
    if "next week" in raw:
        return today + timedelta(days=(7 - today.weekday()) % 7 or 7)

    return today + timedelta(days=30)


def _backfill_flat_from_legs(intent: IntentSchema) -> IntentSchema:
    """If the LLM returned legs but left the legacy flat fields null, mirror
    legs[0] into them so existing single-leg code keeps working."""
    if not intent.legs:
        return intent
    leg0 = intent.legs[0]
    data = intent.model_dump()
    if data.get("destination") is None and len(intent.legs) == 1:
        data["destination"] = leg0.destination
    if data.get("date_start") is None and len(intent.legs) == 1:
        data["date_start"] = leg0.date_start
    if data.get("date_end") is None and len(intent.legs) == 1:
        data["date_end"] = leg0.date_end
    if data.get("budget_total_usd") is None and len(intent.legs) == 1:
        data["budget_total_usd"] = leg0.budget_max_usd or leg0.budget_min_usd
    return IntentSchema(**data)
