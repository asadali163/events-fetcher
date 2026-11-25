"""Daily output: a city's events for the next N days in the final schema,
joined with their attendance + radius estimates.

Writes output/<city>/events_<YYYY-MM-DD>.json and output/<city>/latest.json
(full, with "_meta"), and output/<city>/public.json: a plain JSON array of
the events in the final schema without "_meta" - the file core/upload.py
pushes to S3. public.json holds:
  - only enabled events (unless upload.only_enabled is false),
  - only events whose coordinates are inside upload.boundary_file (a KML
    polygon, e.g. boundaries/Paris.kml) when it is set,
  - at most upload.max_events of them: biggest expectedAttendance first, but
    spread over the city (upload.spread: each extra event in an already-used
    cell_km grid square scores `decay` times less), at most
    upload.max_per_venue per location over the window and
    upload.max_per_venue_per_day per location on one day.
With a boundary, every event in latest.json gets _meta.insideBoundary.
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
from core.boundary import bounding_box, in_boundary, load_boundary_file  # noqa: E402
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


def _size(event):
    return event.get("expectedAttendance") or 0


def _area(event, origin, cell_km):
    """Grid square (row, col) of `cell_km` km containing the event, counted
    from `origin` (lat, lon); None without coordinates."""
    lat, lon = event.get("latitude"), event.get("longitude")
    if lat is None or lon is None:
        return None
    north_km = (lat - origin[0]) * 111.32
    east_km = (lon - origin[1]) * 111.32 * math.cos(math.radians(origin[0]))
    return int(north_km // cell_km), int(east_km // cell_km)


def _low_confidence(event):
    return ((event.get("_meta") or {}).get("confidence") or "").lower() == "low"


def _pick(candidates, max_events, max_per_venue=None, max_per_venue_per_day=None, boundary=None, spread=None,
          max_per_venue_low_confidence=None):
    """Greedy pick of up to max_events. Each step takes the allowed event with
    the highest score = expectedAttendance x decay^(events already picked in
    its area), so big events still come first, but every extra event in an
    already-covered area counts a bit less - the list spreads over the city.
    Areas are spread.cell_km squares (from the boundary's corner). Without
    spread (or decay 1) this is plain "biggest first".
    Allowed = not a duplicate listing of a picked show, at most max_per_venue
    picked at the same location, at most max_per_venue_per_day there that day,
    and - for an estimate the model itself rated "low" confidence - at most
    max_per_venue_low_confidence low-confidence picks at that location (a weak
    candidate never fills several days at the same place)."""
    spread = spread or {}
    cell_km = float(spread.get("cell_km") or 0)
    decay = float(spread.get("decay", 1) if spread.get("decay") is not None else 1)
    use_spread = cell_km > 0 and decay < 1
    origin = (0.0, 0.0)
    if use_spread:
        if boundary:
            origin = bounding_box(boundary)[:2]
        else:
            coords = [(e["latitude"], e["longitude"]) for e in candidates
                      if e.get("latitude") is not None and e.get("longitude") is not None]
            origin = (min(c[0] for c in coords), min(c[1] for c in coords)) if coords else origin
    area = {id(e): (_area(e, origin, cell_km) if use_spread else None) for e in candidates}

    def allowed(event, top, seen):
        if _listing_key(event) in seen:
            return False
        if max_per_venue and sum(1 for t in top if _same_venue(t, event)) >= int(max_per_venue):
            return False
        if max_per_venue_per_day and sum(
            1 for t in top if _day(t) == _day(event) and _same_venue(t, event)
        ) >= int(max_per_venue_per_day):
            return False
        if max_per_venue_low_confidence is not None and _low_confidence(event) and sum(
            1 for t in top if _low_confidence(t) and _same_venue(t, event)
        ) >= int(max_per_venue_low_confidence):
            return False
        return True

    pool = list(candidates)
    top, seen, per_area = [], set(), {}
    while pool and len(top) < max_events:
        ranked = sorted(pool, key=lambda e: (
            -_size(e) * (decay ** per_area.get(area[id(e)], 0) if use_spread else 1),
            e.get("startTime") or "",
        ))
        dropped, pick = set(), None
        for event in ranked:
            if allowed(event, top, seen):
                pick = event
                break
            dropped.add(id(event))  # limits only get tighter: never allowed again
        if pick is None:
            break
        dropped.add(id(pick))
        pool = [e for e in pool if id(e) not in dropped]
        top.append(pick)
        seen.add(_listing_key(pick))
        per_area[area[id(pick)]] = per_area.get(area[id(pick)], 0) + 1
    return sorted(top, key=lambda e: (-_size(e), e.get("startTime") or ""))


def public_events(events, only_enabled=True, max_events=None, max_per_venue=None, max_per_venue_per_day=None,
                  boundary=None, spread=None, max_per_venue_low_confidence=None):
    """Final-schema events without _meta: enabled events (unless
    only_enabled is False), only those inside `boundary` (polygons from
    core.boundary) when given, then with max_events the picked ones (see
    _pick: big events first, spread over the city, venue limits), biggest
    first."""
    chosen = [e for e in events if e.get("enabled") or not only_enabled]
    if boundary:
        chosen = [e for e in chosen if in_boundary(e.get("latitude"), e.get("longitude"), boundary)]
    if max_events:
        chosen = _pick(chosen, int(max_events), max_per_venue, max_per_venue_per_day, boundary, spread,
                       max_per_venue_low_confidence)
    return [{f: e.get(f) for f in PUBLIC_FIELDS} for e in chosen]


def run_export(city, days=None):
    events, days = build_output(city, days)
    upload_cfg = get_city_config(city).get("upload") or {}
    only_enabled = upload_cfg.get("only_enabled", True)
    max_events = upload_cfg.get("max_events")
    max_per_venue = upload_cfg.get("max_per_venue")
    max_per_venue_per_day = upload_cfg.get("max_per_venue_per_day")
    spread = upload_cfg.get("spread") or {}
    # A configured but missing/broken boundary file raises: better a failed
    # (retried) daily job than an upload that ignores the boundary.
    boundary_file = upload_cfg.get("boundary_file")
    boundary = load_boundary_file(boundary_file) if boundary_file else None
    if boundary:
        for event in events:
            event["_meta"]["insideBoundary"] = in_boundary(event.get("latitude"), event.get("longitude"), boundary)
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
    public = public_events(events, only_enabled, max_events, max_per_venue, max_per_venue_per_day,
                           boundary, spread, upload_cfg.get("max_per_venue_low_confidence"))
    public_path = folder / "public.json"
    public_path.write_text(json.dumps(public, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    summary = {"city": city, "days": days, "events": len(events), "enabled": payload["enabledCount"],
               "file": str(dated), "public_file": str(public_path), "public_events": len(public)}
    if boundary:
        summary["boundary_file"] = boundary_file
        summary["enabled_inside_boundary"] = sum(
            1 for e in events if e["enabled"] and e["_meta"].get("insideBoundary"))
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
