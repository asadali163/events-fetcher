"""Footfall and demand uplift: deterministic, no LLM call.

footfallUpliftPercent = how much busier the area inside radiusMeters is than
normal, during the event (+/- ~1 h for arrival and departure). Max 20 %.

    footfall = 20 x size x time x crowd_pattern x outdoor   (cap 20, step 0.5)

  size           log scale on attendance per session: <=50 -> 0, 500 -> 0.33,
                 1,500 -> 0.49, 6,000 -> 0.69, 20,000 -> 0.87, >=50,000 -> 1.0
  time           evening 17-23h -> 1.0, weekend daytime -> 0.9, weekday
                 daytime -> 0.7 (area already busy), late night -> 0.6,
                 start time unknown -> 0.85
  crowd_pattern  everyone arrives/leaves together (match, concert, show) -> 1.0,
                 club/conference/meetup-like -> 0.8, festival -> 0.7,
                 spread over the day (exhibition, fair, tours) -> 0.5
  outdoor        crowd is on the street itself -> 1.1

demandUpliftPercent = extra spending demand nearby (food & drink, transport,
shops). Attendees spend more per head than passers-by, so:

    demand = footfall x spend_factor   (cap 30, step 0.5)

Online / cancelled -> 0 and 0. Outside the city, failed or not estimated ->
None (no numbers to base it on).

Bump UPLIFT_VERSION whenever a weight changes: stored rows with an older
version are recomputed on the next enrichment run, with no LLM call.
"""

import math
import re
from datetime import date

UPLIFT_VERSION = "u1"

FOOTFALL_CAP = 20.0
DEMAND_CAP = 30.0
SIZE_FLOOR, SIZE_FULL = 50, 50_000

TIME_EVENING, TIME_WEEKEND_DAY, TIME_WEEKDAY_DAY, TIME_LATE_NIGHT, TIME_UNKNOWN = 1.0, 0.9, 0.7, 0.6, 0.85
OUTDOOR_FACTOR = 1.1

# eventKind values come from the attendance prompt.
CROWD_PATTERN = {
    "pro_sport": 1.0, "amateur_sport": 1.0, "concert": 1.0, "theatre_show": 1.0, "film_screening": 1.0,
    "club_night": 0.8, "conference_talk": 0.8, "workshop_class": 0.8, "meetup_networking": 0.8,
    "religious": 0.8, "other": 0.8,
    "festival": 0.7,
    "exhibition": 0.5, "fair_expo": 0.5, "tour_visit": 0.5,
}
SPEND_FACTOR = {
    "pro_sport": 1.5, "concert": 1.5, "festival": 1.5,
    "club_night": 1.4, "theatre_show": 1.3,
    "conference_talk": 1.2, "fair_expo": 1.2,
    "meetup_networking": 1.1, "film_screening": 1.1,
    "amateur_sport": 1.0, "workshop_class": 1.0, "other": 1.0,
    "exhibition": 0.8, "tour_visit": 0.8,
    "religious": 0.6,
}
DEFAULT_KIND = "other"

_TIME_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*([AaPp]\.?\s*[Mm]\.?)?")


def parse_time(value):
    """(hour, minute) from "20:00", "20:00:00+0200", "7 PM", "7:30 pm"; else None."""
    match = _TIME_RE.match(str(value or ""))
    if not match:
        return None
    hour, minute, ampm = int(match.group(1)), int(match.group(2) or 0), match.group(3)
    if match.group(2) is None and not ampm:
        return None  # a bare number isn't a time
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm.lower().startswith("p") else 0)
    return (hour, minute) if hour < 24 and minute < 60 else None


def _round_half(value):
    return round(value * 2) / 2


def size_factor(attendance, sessions=None):
    per_session = (attendance or 0) / max(int(sessions or 1), 1)
    if per_session <= SIZE_FLOOR:
        return 0.0
    return min(1.0, math.log10(per_session / SIZE_FLOOR) / math.log10(SIZE_FULL / SIZE_FLOOR))


def time_factor(event):
    hm = parse_time(event.get("time"))
    if not hm:
        return TIME_UNKNOWN, "unknown"
    hour = hm[0]
    if 17 <= hour < 23:
        return TIME_EVENING, "evening"
    if hour >= 23 or hour < 6:
        return TIME_LATE_NIGHT, "late_night"
    try:
        weekend = date.fromisoformat(str(event.get("date"))[:10]).weekday() >= 5
    except ValueError:
        weekend = False
    return (TIME_WEEKEND_DAY, "weekend_day") if weekend else (TIME_WEEKDAY_DAY, "weekday_day")


def compute_uplift(event, status, attendance, sessions=None, outdoor=None, event_kind=None):
    """{"footfall", "demand", "version", "factors"}; footfall/demand are None
    when there is nothing to base them on."""
    result = {"footfall": None, "demand": None, "version": UPLIFT_VERSION, "factors": None}
    if status in ("online", "cancelled"):
        result.update(footfall=0.0, demand=0.0)
        return result
    if status != "estimated" or attendance is None:
        return result

    kind = event_kind if event_kind in CROWD_PATTERN else DEFAULT_KIND
    size = size_factor(attendance, sessions)
    time_f, time_label = time_factor(event)
    crowd = CROWD_PATTERN[kind]
    out_f = OUTDOOR_FACTOR if outdoor else 1.0
    spend = SPEND_FACTOR[kind]

    footfall = min(FOOTFALL_CAP, _round_half(FOOTFALL_CAP * size * time_f * crowd * out_f))
    demand = min(DEMAND_CAP, _round_half(footfall * spend))
    result.update(
        footfall=footfall,
        demand=demand,
        factors={
            "size": round(size, 3), "time": time_f, "timeSlot": time_label,
            "crowdPattern": crowd, "outdoor": out_f, "spend": spend, "eventKind": kind,
        },
    )
    return result


def uplift_from_estimate(event, estimate):
    """From an estimate dict as produced by the estimator / rules."""
    return compute_uplift(
        event, estimate.get("status"), estimate.get("expectedAttendance"),
        estimate.get("sessions"), estimate.get("outdoor"), estimate.get("eventKind"),
    )


def uplift_from_row(event, row):
    """From an attendance_estimates row (enrichment.db)."""
    return compute_uplift(
        event, row.get("status"), row.get("expected_attendance"),
        row.get("sessions"), row.get("outdoor"), row.get("event_kind"),
    )
