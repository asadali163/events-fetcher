import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# Ticket master doesnt give more then 1000 events on a single call. for example if for one month, it says there are 1200 events, but you can only get first 1000. So it is better to use small window like 1 week 2 week.

# core/ lives at the project root, three folders up from here
# (connectors/ticketing/ticketmaster/ -> project root) — add it to sys.path
# before importing it.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from core.locations_store import save_location

load_dotenv()

API_KEY = os.environ["TICKETMASTER_API_KEY"]
BASE_URL = "https://app.ticketmaster.com/discovery/v2/events.json"

DAYS_AHEAD = 30  # change to 7 or 14 later — this is the only line that changes
PAGE_SIZE = 50

# Ticketmaster refuses any request past the 1,000th result of a search —
# it returns a 400 instead of an empty page, so we must stop before asking.
MAX_ACCESSIBLE_RESULTS = 1000

REQUIRED_FIELDS = ["source_event_id", "event_name", "start_datetime"]


def get_date_window(days_ahead=DAYS_AHEAD):
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=days_ahead)
    # Ticketmaster wants this exact format: 2026-07-08T00:00:00Z
    start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    return start_str, end_str


def discover(city, days_ahead=DAYS_AHEAD):
    start_str, end_str = get_date_window(days_ahead)
    all_events = []
    page = 0

    while page * PAGE_SIZE < MAX_ACCESSIBLE_RESULTS:
        params = {
            "apikey": API_KEY,
            "city": city,
            "startDateTime": start_str,
            "endDateTime": end_str,
            "size": PAGE_SIZE,
            "page": page,
        }
        response = requests.get(BASE_URL, params=params)
        response.raise_for_status()
        data = response.json()
        # print(data.get("page").get("totalElements"))

        page_events = data.get("_embedded", {}).get("events", [])
        all_events.extend(page_events)

        total_pages = data.get("page", {}).get("totalPages", 1)
        page += 1
        if page >= total_pages:
            break
    else:
        print(
            f"Reached Ticketmaster's {MAX_ACCESSIBLE_RESULTS}-result paging limit "
            f"for {city} — there may be more events than we can see in this window."
        )

    return all_events


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize(raw_event):
    venue = raw_event.get("_embedded", {}).get("venues", [{}])[0]
    classification = raw_event.get("classifications", [{}])[0]
    location = venue.get("location", {})

    return {
        "source": "ticketmaster",
        "source_event_id": raw_event.get("id"),
        "event_name": raw_event.get("name"),
        "start_datetime": raw_event.get("dates", {}).get("start", {}).get("dateTime"),
        "timezone": raw_event.get("dates", {}).get("timezone"),
        "status": raw_event.get("dates", {}).get("status", {}).get("code"),
        "venue_name": venue.get("name"),
        "venue_city": venue.get("city", {}).get("name"),
        "venue_country": venue.get("country", {}).get("name"),
        "venue_latitude": to_float(location.get("latitude")),
        "venue_longitude": to_float(location.get("longitude")),
        "category": classification.get("segment", {}).get("name"),
        "url": raw_event.get("url"),
    }


def validate(normalized_event):
    missing = [f for f in REQUIRED_FIELDS if not normalized_event.get(f)]
    return (len(missing) == 0, missing)


def save_venue_location(normalized_event):
    venue_name = normalized_event.get("venue_name")
    latitude = normalized_event.get("venue_latitude")
    longitude = normalized_event.get("venue_longitude")

    if not venue_name or latitude is None or longitude is None:
        return False

    save_location(
        "venue",
        venue_name,
        normalized_event.get("venue_city"),
        normalized_event.get("venue_country"),
        latitude,
        longitude,
        source="ticketmaster",
    )
    return True


if __name__ == "__main__":
    city = "New York"

    raw_events = discover(city)
    print(f"Fetched {len(raw_events)} raw events for {city}")

    good_events = []
    venues_saved = 0
    for raw in raw_events:
        normalized = normalize(raw)

        if save_venue_location(normalized):
            venues_saved += 1

        is_valid, missing = validate(normalized)
        if is_valid:
            good_events.append(normalized)
        else:
            print(f"Skipping event, missing: {missing}")

    print(f"Saved coordinates for {venues_saved} venues to locations.db")
    print(f"{len(good_events)} events passed validation")

    with open("ticketmaster_normalized.json", "w") as f:
        json.dump(good_events, f, indent=2)
