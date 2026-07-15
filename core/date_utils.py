import re
from datetime import datetime

# Matches a trailing timezone offset with no colon (e.g. "+0200"), so it can
# be reformatted into the "+02:00" shape datetime.fromisoformat() always
# accepts, regardless of Python version.
_BARE_OFFSET_PATTERN = re.compile(r"([+-]\d{2})(\d{2})$")


def split_iso_datetime(raw_value):
    """Split a standard ISO 8601 datetime string into (date, time).

    Normalizes the small format variations seen so far — a trailing 'Z'
    (Ticketmaster) and a bare +HHMM offset with no colon (Songkick) — before
    parsing. Returns (None, "") if raw_value is empty or isn't ISO 8601.

    Reusable by any future source whose dates are already ISO 8601. If a new
    source's format isn't ISO 8601, write it its own parser next to its
    connector instead of extending this one.
    """
    if not raw_value:
        return None, ""

    normalized = raw_value.strip().replace("Z", "+00:00")
    normalized = _BARE_OFFSET_PATTERN.sub(r"\1:\2", normalized)

    # A date-only value (e.g. Songkick's start_date, "2026-07-20") has no
    # "T" separator — fromisoformat() will still parse it as midnight, but
    # that's not a real announced time, so it must not be reported as one.
    has_time_component = "T" in normalized

    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None, ""

    if not has_time_component:
        return dt.date().isoformat(), ""

    return dt.date().isoformat(), dt.strftime("%H:%M:%S%z")
