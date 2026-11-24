"""Daily output: a city's events for the next N days in the final schema,
joined with their attendance + radius estimates.

Writes output/<city>/events_<YYYY-MM-DD>.json and output/<city>/latest.json
(full, with "_meta"), and output/<city>/public.json: a plain JSON array of
the events in the final schema without "_meta" - only enabled events (unless
upload.only_enabled is false), limited to the upload.max_events biggest by
expectedAttendance, at most upload.max_per_venue per location over the window
and upload.max_per_venue_per_day per location on one day - the file
core/upload.py pushes to S3.
Each event:
  id, name, type, venueName, latitude, longitude, startTime, durationHours,
  expectedAttendance, radiusMeters, demandUpliftPercent, footfallUpliftPercent,
  enabled
plus "_meta" (canonical key, estimate status, confidence, sources) for
checking; drop it downstream if not needed. footfallUpliftPercent (0-20) and
demandUpliftPercent (0-30) come from enrichment.db (core/attendance/uplift.py);
null when the event has no usable estimate.

`type` is one of: sport, festival, concert, other (see output_type()).

`durationHours` is never empty: the listed start->end span when the source
gives both times on the same day (up to 16 h), otherwise a typical length for
the event kind (DEFAULT_DURATION_BY_KIND / _BY_TYPE). _meta.durationSource
says which ("listed" or "default").

`enabled` is true only for events with an LLM estimate that isn't flagged as
outside the city.

Usage:
    python -m core.export Paris              # window = daily.window_days
    python -m core.export Paris --days 14
"""

import argparse
import json
import math
import re
import unicodedata
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import enrichment_store  # noqa: E402
from core.canonical_store import DB_PATH as CANONICAL_DB  # noqa: E402
from core.enrich import load_window_events  # noqa: E402
from core.attendance.uplift import parse_time as _parse_time  # noqa: E402
from core.pipeline_config import get_city_config  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "output"

OUTPUT_TYPES = ("sport", "festival", "concert", "other")
# LLM eventKind -> output type. Club nights count as music events.
KIND_TO_TYPE = {
    "pro_sport": "sport", "amateur_sport": "sport",
    "festival": "festival",
    "concert": "concert", "club_night": "concert",
}
# Eventbrite's "sports-and-fitness" (chess, runs, walks) is NOT treated as sport.
MUSIC_SOURCES = {"songkick", "dice", "bandsintown"}
_SPORT_RE = re.compile(r"football|rugby|basket|handball|volley|hockey|tennis|match\b", re.I)
_FESTIVAL_RE = re.compile(r"\bfestival|\bfest\b", re.I)
_NOT_MUSIC_RE = re.compile(r"comedy|com[ée]die|stand[- ]?up|th[ée][aâ]tre|theatre|spectacle", re.I)
_MUSIC_RE = re.compile(r"\bmusi[cq]|concert|\bjazz|\brock\b|\bpop\b|metal|electronic|hip[ _-]?hop|rap\b|"
                       r"\bfolk|blues|soul|funk|reggae|classical|classique|r&b|indie|techno|house\b|\bdj\b", re.I)


def output_type(event, est=None):
    """sport | festival | concert | other. Uses the LLM's eventKind when the
    event has an estimate, otherwise source + keywords."""
    kind = (est or {}).get("event_kind")
    if kind:
        return KIND_TO_TYPE.get(kind, "other")
    if event.get("source") == "sportsevents24" or any(event.get(f) for f in ("sport", "team_home", "tournament")):
        return "sport"
    labels = " ".join(str(event.get(f) or "") for f in ("type", "category", "genre"))
    name = str(event.get("event_name") or "")
    if _SPORT_RE.search(labels):
        return "sport"
    if _FESTIVAL_RE.search(name) or _FESTIVAL_RE.search(labels):
        return "festival"
    if _NOT_MUSIC_RE.search(labels) or _NOT_MUSIC_RE.search(name):
        return "other"
    if event.get("source") in MUSIC_SOURCES or _MUSIC_RE.search(labels):
        return "concert"
    return "other"


def _start_time(event, tz):
    """ISO 8601 with the city's UTC offset, or just the date if no time is known."""
    day = event.get("date")
    if not day:
        return None
    hm = _parse_time(event.get("time"))
    if not hm:
        return str(day)
    try:
        return datetime(*map(int, str(day).split("-")), hm[0], hm[1], tzinfo=tz).isoformat()
    except ValueError:
        return str(day)


# Typical length in hours when the source gives no usable end time.
# By LLM eventKind first, then by output type.
DEFAULT_DURATION_BY_KIND = {
    "pro_sport": 2.5, "amateur_sport": 2.0,
    "concert": 3.0, "club_night": 6.0, "festival": 8.0, "theatre_show": 2.5,
    "film_screening": 2.0, "conference_talk": 2.0, "workshop_class": 2.0,
    "meetup_networking": 2.5, "tour_visit": 1.5, "religious": 1.5,
    "exhibition": 8.0, "fair_expo": 8.0, "other": 3.0,
}
DEFAULT_DURATION_BY_TYPE = {"sport": 2.5, "festival": 8.0, "concert": 3.0, "other": 3.0}
MAX_LISTED_HOURS = 16  # longer "same day" spans are placeholders (e.g. ends 23:59)


def _listed_duration(event):
    """Hours from start to end when both times are known on the same day."""
    start, end = _parse_time(event.get("time")), _parse_time(event.get("end_time"))
    end_date = event.get("end_date")
    if not start or not end or (end_date and end_date != event.get("date")):
        return None
    minutes = (end[0] * 60 + end[1]) - (start[0] * 60 + start[1])
    if minutes <= 0:  # e.g. 22:00 -> 04:00: runs past midnight
        minutes += 24 * 60
    hours = round(minutes / 60, 2)
    return hours if 0 < hours <= MAX_LISTED_HOURS else None


def _duration_hours(event, est=None, out_type="other"):
    """(hours, "listed" | "default") - never empty."""
    listed = _listed_duration(event)
    if listed is not None:
        return listed, "listed"
    kind = (est or {}).get("event_kind")
    if kind in DEFAULT_DURATION_BY_KIND:
        return DEFAULT_DURATION_BY_KIND[kind], "default"
    return DEFAULT_DURATION_BY_TYPE.get(out_type, 3.0), "default"


def _row_ids(city):
    conn = sqlite3.connect(f"file:{CANONICAL_DB}?mode=ro", uri=True, timeout=30)
    rows = conn.execute("SELECT canonical_key, id FROM canonical_events WHERE city = ?", (city,)).fetchall()
    conn.close()
    return dict(rows)


def build_output(city, days=None):
    config = get_city_config(city)
    days = days or (config.get("daily") or {}).get("window_days", 14)
    tz = ZoneInfo(config.get("timezone", "UTC"))
    events = load_window_events(city, days)
    estimates = enrichment_store.get_estimates(city)
    ids = _row_ids(city)

    out = []
    for key, event in events:
        est = estimates.get(key) or {}
        status = est.get("status")
        estimated = status == "estimated"
        out_type = output_type(event, est)
        duration, duration_source = _duration_hours(event, est, out_type)
        out.append({
            "id": ids.get(key),
            "name": event.get("event_name"),
            "type": out_type,
            "venueName": event.get("venue_name"),
            "latitude": event.get("venue_latitude"),
            "longitude": event.get("venue_longitude"),
            "startTime": _start_time(event, tz),
            "durationHours": duration,
            "expectedAttendance": est.get("expected_attendance") if status != "failed" else None,
            "radiusMeters": est.get("radius_meters") if status != "failed" else None,
            "demandUpliftPercent": est.get("demand_uplift_percent") if status != "failed" else None,
            "footfallUpliftPercent": est.get("footfall_uplift_percent") if status != "failed" else None,
            "enabled": bool(estimated and est.get("likely_in_city") != "no"
                            and est.get("expected_attendance") is not None),
            "_meta": {
                "canonicalKey": key,
                "estimateStatus": status or "not_estimated",
                "confidence": est.get("confidence"),
                "low": est.get("low"),
                "high": est.get("high"),
                "likelyInCity": est.get("likely_in_city"),
                "eventKind": est.get("event_kind"),
                "durationSource": duration_source,
                "sourceType": event.get("type"),
                "upliftFactors": json.loads(est["uplift_json"]) if est.get("uplift_json") else None,
                "sources": [s for s in (event.get("sources") or []) if isinstance(s, str)] or event.get("source"),
            },
        })
    return out, days


PUBLIC_FIELDS = [
    "id", "name", "type", "venueName", "latitude", "longitude", "startTime", "durationHours",
    "expectedAttendance", "radiusMeters", "demandUpliftPercent", "footfallUpliftPercent", "enabled",
]


def _listing_key(event):
    """Same date + first word of the name + venue: the same show listed by two
    sources (e.g. DICE and Songkick with slightly different titles)."""
    first_word = (re.split(r"\W+", str(event.get("name") or "").lower().strip()) or [""])[0]
    return (str(event.get("startTime") or "")[:10], first_word, _venue_name_key(event)[:8])


SAME_VENUE_METERS = 150


def _venue_name_key(event):
    name = unicodedata.normalize("NFKD", str(event.get("venueName") or "").lower())
    name = name.encode("ascii", "ignore").decode().strip()
    name = re.sub(r"^(les|le|la|l)\s*['\u2019 ]\s*", "", name)  # leading article: "Le Trabendo", "L'Olympia"
    return re.sub(r"[^a-z0-9]+", "", name)


def _meters(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def _same_venue(a, b):
    """Same location: same normalised venue name, or within SAME_VENUE_METERS."""
    if _venue_name_key(a) and _venue_name_key(a) == _venue_name_key(b):
        return True
    pa, pb = (a.get("latitude"), a.get("longitude")), (b.get("latitude"), b.get("longitude"))
    if None in pa or None in pb:
        return False
    return _meters(pa, pb) <= SAME_VENUE_METERS


def _day(event):
    return str(event.get("startTime") or "")[:10]


def public_events(events, only_enabled=True, max_events=None, max_per_venue=None, max_per_venue_per_day=None):
    """Final-schema events without _meta. With max_events: the N biggest by
    expectedAttendance, biggest first, with duplicate listings of the same show
    dropped, at most max_per_venue events per location over the whole window
    and at most max_per_venue_per_day per location on the same day (either
    limit can be left empty)."""
    chosen = [e for e in events if e.get("enabled") or not only_enabled]
    if max_events:
        chosen = sorted(chosen, key=lambda e: (-(e.get("expectedAttendance") or 0), e.get("startTime") or ""))
        seen, top = set(), []
        for event in chosen:
            key = _listing_key(event)
            if key in seen:
                continue
            if max_per_venue and sum(1 for t in top if _same_venue(t, event)) >= int(max_per_venue):
                continue
            if max_per_venue_per_day and sum(
                1 for t in top if _day(t) == _day(event) and _same_venue(t, event)
            ) >= int(max_per_venue_per_day):
                continue
            seen.add(key)
            top.append(event)
            if len(top) >= int(max_events):
                break
        chosen = top
    return [{f: e.get(f) for f in PUBLIC_FIELDS} for e in chosen]


def run_export(city, days=None):
    events, days = build_output(city, days)
    upload_cfg = get_city_config(city).get("upload") or {}
    only_enabled = upload_cfg.get("only_enabled", True)
    max_events = upload_cfg.get("max_events")
    max_per_venue = upload_cfg.get("max_per_venue")
    max_per_venue_per_day = upload_cfg.get("max_per_venue_per_day")
    folder = OUTPUT_DIR / re.sub(r"[^a-z0-9]+", "_", city.lower()).strip("_")
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "city": city,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "windowStart": date.today().isoformat(),
        "windowDays": days,
        "count": len(events),
        "enabledCount": sum(1 for e in events if e["enabled"]),
        "events": events,
    }
    dated = folder / f"events_{date.today().isoformat()}.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    dated.write_text(text, encoding="utf-8")
    (folder / "latest.json").write_text(text, encoding="utf-8")
    public = public_events(events, only_enabled, max_events, max_per_venue, max_per_venue_per_day)
    public_path = folder / "public.json"
    public_path.write_text(json.dumps(public, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    summary = {"city": city, "days": days, "events": len(events), "enabled": payload["enabledCount"],
               "file": str(dated), "public_file": str(public_path), "public_events": len(public)}
    print(f"[export] {json.dumps(summary)}")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Write a city's final event list for the next N days.")
    parser.add_argument("city")
    parser.add_argument("--days", type=int, default=None)
    args = parser.parse_args()
    run_export(args.city, args.days)


if __name__ == "__main__":
    main()
