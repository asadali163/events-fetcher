"""Rules that settle an event without any LLM call: online, cancelled,
clearly outside the city."""

import re

ONLINE_RE = re.compile(r"\b(online event|online|webinar|livestream|live stream|zoom)\b", re.I)
CANCELLED_RE = re.compile(r"cancel|annul|postpon|report", re.I)
_STUBHUB_SLUG_RE = re.compile(r"stubhub\.[a-z.]+/([^/?#]+)-tickets-\d{1,2}-\d{1,2}-\d{4}", re.I)


def is_online(event):
    return bool(ONLINE_RE.search(str(event.get("venue_name") or "")))


def location_problem(event, city_settings):
    """Why the event is clearly NOT in the city, or None."""
    slug_word = city_settings.get("url_slug")
    slug = _STUBHUB_SLUG_RE.search(str(event.get("url") or ""))
    if slug_word and slug and slug_word not in slug.group(1).lower():
        return f'StubHub URL names another place ("{slug.group(1)}")'
    prefixes = tuple(str(p) for p in city_settings.get("postcode_prefixes") or ())
    postcode = re.search(r"\b(\d{5})\b", str(event.get("venue_address") or ""))
    if prefixes and postcode and not postcode.group(1).startswith(prefixes):
        return f"postcode {postcode.group(1)} is outside the city"
    return None


def _ruled(status, expected, reason):
    return {
        "status": status, "expectedAttendance": expected, "low": expected, "high": expected,
        "confidence": "high" if expected == 0 else None, "venueCapacityGuess": None,
        "likelyInCity": None, "eventKind": None, "capacityUsed": None, "capacitySource": None,
        "sessions": None, "outdoor": None, "priorCategory": None,
        "radiusMeters": 0 if expected == 0 else None,
        "reason": reason + " (rule, no LLM call)",
    }


def apply_rules(event, city_settings):
    """A finished estimate dict when a rule settles the event, else None."""
    if is_online(event):
        return _ruled("online", 0, "Online event - nobody physically present")
    if CANCELLED_RE.search(str(event.get("status") or "")):
        return _ruled("cancelled", 0, f'Status is "{event.get("status")}"')
    problem = location_problem(event, city_settings)
    if problem:
        return _ruled("outside_city", None, f"Skipped: {problem}")
    return None
