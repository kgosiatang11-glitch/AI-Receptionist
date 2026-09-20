"""Deterministic natural-language parsing for the booking conversation.

Deliberately not built on a fuzzy NLP library: a scheduling system needs
predictable, testable behaviour, and the failure mode of "guessed wrong" is
worse here than "asked one more clarifying question." Every function returns
``None`` when it isn't confident, which the booking engine treats as "ask the
customer" rather than as a value.

All date resolution is relative to the TENANT's timezone and current time,
never the server's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "a": 1, "an": 1, "couple": 2,
}

#: Vague periods recognised but NOT resolved to a clock time -- the booking
#: engine asks a sharper follow-up instead of guessing within the period.
VAGUE_PERIODS = ("morning", "afternoon", "evening", "night", "tonight")

_AFFIRMATIVE = (
    "yes", "yep", "yeah", "yup", "correct", "confirm", "confirmed",
    "sounds good", "go ahead", "that's right", "thats right", "please do",
    "book it", "sure", "ok", "okay",
)
_NEGATIVE = (
    "no", "nope", "not that", "actually", "wrong", "change it", "different",
)


def now_in_tenant_tz(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def is_affirmative(text: str) -> bool:
    t = _normalize(text)
    return any(_phrase_matches(phrase, t) for phrase in _AFFIRMATIVE)


def is_negative_or_change(text: str) -> bool:
    t = _normalize(text)
    return any(_phrase_matches(phrase, t) for phrase in _NEGATIVE)


def _phrase_matches(phrase: str, text: str) -> bool:
    """Word-boundary match so short tokens like 'ok' don't fire inside
    unrelated words (naive substring matching would match 'ok' inside
    'book', which is exactly the bug this guards against)."""
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().strip().split())


@dataclass(frozen=True)
class ParsedDate:
    value: date
    #: True when parsed from an explicit calendar date/weekday rather than a
    #: vague reference -- not currently used for branching, kept for tests
    #: and future confidence-based behaviour.
    explicit: bool = True


def extract_date(text: str, tz_name: str, reference: datetime | None = None) -> date | None:
    """Return the calendar date the customer means, or None if unclear.

    ``reference`` overrides "now" -- used by tests so results are stable
    regardless of when they run.
    """
    t = _normalize(text)
    now = reference or now_in_tenant_tz(tz_name)
    today = now.date()

    if re.search(r"\btoday\b", t):
        return today
    if re.search(r"\btomorrow\b", t):
        return today + timedelta(days=1)

    # Explicit "DD Month" or "Month DD", optionally with a year.
    explicit = _extract_explicit_date(t, today)
    if explicit is not None:
        return explicit

    for name, weekday in _WEEKDAYS.items():
        if not re.search(rf"\b{name}\b", t):
            continue
        delta = (weekday - today.weekday()) % 7
        nearest = today + timedelta(days=delta)
        if re.search(rf"\bnext\s+{name}\b", t):
            # "next X" means the occurrence in the following week, distinct
            # from "this X" / a bare weekday name, which means the nearest
            # upcoming one (today counts if today IS that weekday).
            return nearest + timedelta(days=7)
        return nearest

    return None


def _extract_explicit_date(t: str, today: date) -> date | None:
    month_names = "|".join(_MONTHS)
    # "19 september" / "19 september 2026" / "19th of september"
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({month_names})\b(?:\s+(\d{{4}}))?", t)
    if not m:
        # "september 19" / "sep 19th"
        m = re.search(rf"\b({month_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:\s+(\d{{4}}))?", t)
        if m:
            month = _MONTHS[m.group(1)]
            day = int(m.group(2))
            year_group = m.group(3)
        else:
            return None
    else:
        day = int(m.group(1))
        month = _MONTHS[m.group(2)]
        year_group = m.group(3)

    year = int(year_group) if year_group else today.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    if not year_group and candidate < today:
        # "19 September" said in October means next year's 19 September.
        candidate = date(year + 1, month, day)
    return candidate


def extract_vague_period(text: str) -> str | None:
    t = _normalize(text)
    for period in VAGUE_PERIODS:
        if re.search(rf"\b{period}\b", t):
            return "evening" if period == "tonight" else period
    return None


def extract_time(text: str) -> time | None:
    """Return a specific clock time, or None if only a vague period or nothing.

    Ambiguous bare hours (no am/pm, no colon) are resolved with a documented,
    conservative assumption for a booking context: 1-7 -> PM (evenings are
    the common case for restaurant/court/salon bookings), 8-11 -> AM, 12 ->
    PM (noon) unless "midnight" is present. This assumption is never used to
    silently confirm a booking -- the confirmation step always states the
    resolved time back to the customer in full.
    """
    t = _normalize(text)

    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", t)
    if m:
        hour, minute, meridiem = int(m.group(1)), int(m.group(2)), m.group(3)
        return _build_time(hour, minute, meridiem, t)

    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
    if m:
        return _build_time(int(m.group(1)), 0, m.group(2), t)

    if "midnight" in t:
        return time(0, 0)
    if "noon" in t or "midday" in t:
        return time(12, 0)

    m = re.search(r"\baround\s+(\d{1,2})\b", t)
    if m:
        return _build_time(int(m.group(1)), 0, None, t)

    # A bare number on its own turn, e.g. the customer's whole message is "6"
    # or "6:30", in reply to "what time would you like?".
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", t)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        return _build_time(hour, minute, None, t)

    return None


def _build_time(hour: int, minute: int, meridiem: str | None, context: str) -> time | None:
    if hour > 23 or minute > 59:
        return None
    if meridiem is None and hour <= 12:
        if "midnight" in context:
            meridiem = "am" if hour == 12 else meridiem
        elif hour == 12:
            meridiem = "pm"
        elif 1 <= hour <= 7:
            meridiem = "pm"
        elif 8 <= hour <= 11:
            meridiem = "am"
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23:
        return None
    return time(hour, minute)


def extract_duration_minutes(text: str) -> int | None:
    t = _normalize(text)
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*hours?\b", t)
    if m:
        return int(float(m.group(1)) * 60)
    if re.search(r"\bhalf\s+an?\s+hour\b", t):
        return 30
    if re.search(r"\ban?\s+hour\b", t):
        return 60
    m = re.search(r"\b(\d+)\s*(?:min|mins|minutes)\b", t)
    if m:
        return int(m.group(1))
    return None


def extract_party_size(text: str) -> int | None:
    t = _normalize(text)
    m = re.search(r"\bfor\s+(\d+)\s*(?:people|persons|of us|guests)?\b", t)
    if m:
        return int(m.group(1))
    m = re.search(r"\bparty of\s+(\d+)\b", t)
    if m:
        return int(m.group(1))
    for word, value in _WORD_NUMBERS.items():
        if re.search(rf"\bfor\s+{word}\s*(?:people|persons|of us)?\b", t):
            return value
    return None


def extract_customer_name(text: str) -> str | None:
    t = text.strip()
    m = re.search(
        r"(?:my name is|this is|i'?m|it'?s)\s+([A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*){0,2})\b",
        t, re.IGNORECASE,
    )
    if not m:
        return None
    candidate = m.group(1).strip()
    # Reject common false positives where "I'm/it's" is followed by a
    # non-name word rather than an actual name.
    first_word = candidate.split()[0].lower()
    if first_word in {"looking", "trying", "hoping", "wondering", "not", "sure", "here", "good"}:
        return None
    return candidate.title()


def extract_service(text: str, known_services: list[str]) -> str | None:
    """Match against the tenant's OWN configured services only.

    Never invents a service name that isn't in the tenant's knowledge base.
    """
    t = _normalize(text)
    for service in known_services or []:
        if service and service.lower() in t:
            return service
    return None
