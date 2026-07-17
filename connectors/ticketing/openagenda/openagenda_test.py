"""
OpenAgenda API v2 — city-based test script.

Strategy:
  Step 1 — Search agendas (event calendars/publishers) by city name
  Step 2 — Fetch events from each agenda within a geo bounding box
           (centered on the city, from CITY_COORDINATES) + date window
  Step 3 — Normalize and deduplicate all events

Coverage note: OpenAgenda is mostly France/Europe — no Americas coverage.

Docs: https://developers.openagenda.com/

Requirements:
    pip install requests python-dotenv

Run:
    python openagenda_test.py Paris
    python openagenda_test.py Paris 15
"""

import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

OPENAGENDA_BASE_URL = "https://api.openagenda.com/v2"
OPENAGENDA_PUBLIC_KEY = os.environ["OPENAGENDA_PUBLIC_KEY"]

# OpenAgenda's agenda search (step 1) is by city name text search, but
# narrowing events within each agenda to a geo bounding box (step 2) still
# needs a center point — these are standard city-center coordinates,
# precise enough for a radius search. Add more cities here as you test them.
CITY_COORDINATES = {
    "Paris": (48.8566, 2.3522),
    "Lyon": (45.7640, 4.8357),
}

DEFAULT_RADIUS_KM = 10
DEFAULT_DAYS_AHEAD = 15
MAX_AGENDAS = 20


def _lat_lon_to_bbox(lat, lon, radius_km):
    """Convert a lat/lon center point + radius into a bounding box."""
    delta_lat = radius_km / 111.0
    delta_lon = radius_km / (111.0 * math.cos(math.radians(lat)))
    return {
        "geo[northEast][lat]": round(lat + delta_lat, 6),
        "geo[northEast][lng]": round(lon + delta_lon, 6),
        "geo[southWest][lat]": round(lat - delta_lat, 6),
        "geo[southWest][lng]": round(lon - delta_lon, 6),
    }


def _haversine_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    radius_earth_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius_earth_km * math.asin(math.sqrt(a))


def fetch_agenda_uids(city_name, max_agendas=MAX_AGENDAS):
    """Search for agenda UIDs by city name."""
    params = {
        "key": OPENAGENDA_PUBLIC_KEY,
        "search": city_name,
        "size": max_agendas,
    }
    try:
        response = requests.get(f"{OPENAGENDA_BASE_URL}/agendas", params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        return [a["uid"] for a in data.get("agendas", [])]
    except requests.exceptions.RequestException as error:
        print(f"[openagenda] agenda search failed: {error}")
        return []


def fetch_events_for_agenda(agenda_uid, bbox, date_from, date_to, size=100):
    """Fetch events from a single agenda within a geo bounding box + date window."""
    params = {
        "key": OPENAGENDA_PUBLIC_KEY,
        "size": min(size, 300),
        "oaq[passed]": 1,
        "timings[gte]": date_from,
        "timings[lte]": date_to,
        "sort": "timings.asc",
        **bbox,
    }
    all_events = []
    try:
        while True:
            response = requests.get(
                f"{OPENAGENDA_BASE_URL}/agendas/{agenda_uid}/events",
                params=params,
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            all_events.extend(data.get("events", []))

            after = data.get("after")
            if not after:
                break
            params = {k: v for k, v in params.items() if not k.startswith("after")}
            for i, val in enumerate(after):
                params[f"after[{i}]"] = val
    except requests.exceptions.RequestException as error:
        print(f"[openagenda] event fetch failed for agenda {agenda_uid}: {error}")

    return all_events


def normalize_event(event, city_lat, city_lon):
    """Parse a raw OpenAgenda event into a clean, test-friendly dict."""
    title = event.get("title", {})
    if isinstance(title, dict):
        name = (
            title.get("en")
            or title.get("fr")
            or title.get("es")
            or title.get("de")
            or next(iter(title.values()), "Unknown Event")
        )
    else:
        name = str(title) if title else "Unknown Event"

    desc = event.get("description", {})
    if isinstance(desc, dict):
        description = (
            desc.get("en")
            or desc.get("fr")
            or desc.get("es")
            or desc.get("de")
            or next(iter(desc.values()), "")
            or ""
        )
    else:
        description = str(desc) if desc else ""

    first_timing = event.get("firstTiming", {})
    last_timing = event.get("lastTiming", {})
    raw_start = first_timing.get("begin", "")
    raw_end = last_timing.get("end", "")
    date_str = raw_start[:10] if raw_start else None
    time_str = raw_start[11:16] if len(raw_start) > 10 else ""
    end_time_str = raw_end[11:16] if len(raw_end) > 10 else ""

    location = event.get("location", {})
    venue_name = location.get("name", "Unknown Venue")
    city = location.get("city", "")
    address = location.get("address", "")
    venue_lat = location.get("latitude")
    venue_lon = location.get("longitude")

    keywords = event.get("keywords", {})
    if isinstance(keywords, dict):
        raw_kw = keywords.get("en") or keywords.get("fr") or []
        kw_list = [k.strip() for k in raw_kw if k and k.strip()]
    else:
        kw_list = []
    genre = ", ".join(kw_list[:3]) if kw_list else ""

    image_data = event.get("image", {})
    image_url = ""
    if image_data and isinstance(image_data, dict):
        base = image_data.get("base", "")
        filename = image_data.get("filename", "")
        if base and filename:
            image_url = f"{base}{filename}"

    slug = event.get("slug", "")
    origin = event.get("originAgenda", {})
    origin_slug = origin.get("slug", "") if isinstance(origin, dict) else ""
    origin_uid = origin.get("uid", "") if isinstance(origin, dict) else ""
    if origin_slug and slug:
        event_url = f"https://openagenda.com/{origin_slug}/events/{slug}"
    elif origin_uid and slug:
        event_url = f"https://openagenda.com/agendas/{origin_uid}/events/{slug}"
    else:
        event_url = ""

    distance_km = _haversine_km(city_lat, city_lon, venue_lat, venue_lon)

    return {
        "source": "openagenda",
        "id": str(event.get("uid", "")),
        "name": name,
        "description": description,
        "date": date_str,
        "time": time_str,
        "end_time": end_time_str,
        "venue": venue_name,
        "city": city,
        "address": address,
        "venue_lat": venue_lat,
        "venue_lon": venue_lon,
        "genre": genre,
        "distance_km": distance_km,
        "url": event_url,
        "image": image_url,
    }


def fetch_events(city_name, radius_km=DEFAULT_RADIUS_KM, days_ahead=DEFAULT_DAYS_AHEAD):
    coords = CITY_COORDINATES.get(city_name)
    if not coords:
        raise ValueError(
            f"No center coordinates known for '{city_name}'. "
            f"Add it to CITY_COORDINATES in openagenda_test.py."
        )
    city_lat, city_lon = coords

    print(f"Searching OpenAgenda for agendas matching '{city_name}'...")
    agenda_uids = fetch_agenda_uids(city_name)
    if not agenda_uids:
        print(f"No agendas found for '{city_name}'. OpenAgenda is mostly France/Europe.")
        return []
    print(f"Found {len(agenda_uids)} agenda(s): {agenda_uids}")

    bbox = _lat_lon_to_bbox(city_lat, city_lon, radius_km)
    start = datetime.utcnow()
    end = start + timedelta(days=days_ahead)
    date_from = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    date_to = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    all_raw_events = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(fetch_events_for_agenda, uid, bbox, date_from, date_to): uid
            for uid in agenda_uids
        }
        for future in as_completed(futures):
            all_raw_events.extend(future.result())

    if not all_raw_events:
        print(f"No events found near '{city_name}' within {radius_km}km.")
        return []

    normalized = [normalize_event(e, city_lat, city_lon) for e in all_raw_events]
    seen, unique = set(), []
    for e in normalized:
        if e["id"] not in seen:
            seen.add(e["id"])
            unique.append(e)

    return unique


if __name__ == "__main__":
    city = sys.argv[1] if len(sys.argv) > 1 else "Paris"
    days_ahead = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DAYS_AHEAD

    print("=" * 60)
    print("  OpenAgenda API -- Test Run")
    print("=" * 60)
    print(f"  City : {city}")
    print(f"  Days : next {days_ahead} days")
    print("=" * 60)

    events = fetch_events(city, days_ahead=days_ahead)

    print(f"\nFound {len(events)} events\n")
    for i, event in enumerate(events[:10], 1):
        print(f"{i}. {event['name']}")
        print(f"   Date     : {event['date']} {event['time']} -> {event['end_time']}")
        print(f"   Venue    : {event['venue']}, {event['city']}")
        print(f"   Genre    : {event['genre']}")
        print(f"   Distance : {event['distance_km']} km")
        print(f"   URL      : {event['url']}")
        print()
