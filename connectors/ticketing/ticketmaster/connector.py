import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# connector_sdk/ and core/ both live at the project root, three folders up
# from here (connectors/ticketing/ticketmaster/ -> project root).
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from connector_sdk.core.base_connector import BaseConnector
from core.date_utils import split_iso_datetime
from core.locations_store import save_location

load_dotenv()


class TicketmasterConnector(BaseConnector):
    BASE_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
    PAGE_SIZE = 50

    # Ticketmaster refuses any request past the 1,000th result of a search —
    # it returns a 400 instead of an empty page, so we must stop before asking.
    MAX_ACCESSIBLE_RESULTS = 1000

    REQUIRED_FIELDS = ["source_event_id", "event_name", "date"]

    def __init__(self, api_key=None, days_ahead=30):
        self.api_key = api_key or os.environ["TICKETMASTER_API_KEY"]
        self.days_ahead = days_ahead

    def _date_window(self):
        start = datetime.now(timezone.utc)
        end = start + timedelta(days=self.days_ahead)
        start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        return start_str, end_str

    def discover(self, city, country):
        start_str, end_str = self._date_window()
        all_events = []
        page = 0

        while page * self.PAGE_SIZE < self.MAX_ACCESSIBLE_RESULTS:
            params = {
                "apikey": self.api_key,
                "city": city,
                "countryCode": country,
                "startDateTime": start_str,
                "endDateTime": end_str,
                "size": self.PAGE_SIZE,
                "page": page,
            }
            response = requests.get(self.BASE_URL, params=params)
            response.raise_for_status()
            data = response.json()

            page_events = data.get("_embedded", {}).get("events", [])
            all_events.extend(page_events)

            total_pages = data.get("page", {}).get("totalPages", 1)
            page += 1
            if page >= total_pages:
                break
        else:
            print(
                f"Reached Ticketmaster's {self.MAX_ACCESSIBLE_RESULTS}-result paging limit "
                f"for {city} — there may be more events than we can see in this window."
            )

        return all_events

    def fetch(self, raw_event):
        # Ticketmaster's search results already include full event detail,
        # so there's nothing extra to fetch per event.
        return raw_event

    @staticmethod
    def _to_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def normalize(self, raw_event):
        venue = raw_event.get("_embedded", {}).get("venues", [{}])[0]
        classification = raw_event.get("classifications", [{}])[0]
        location = venue.get("location", {})

        raw_datetime = raw_event.get("dates", {}).get("start", {}).get("dateTime")
        date, time = split_iso_datetime(raw_datetime)

        normalized = {
            "source": "ticketmaster",
            "source_event_id": raw_event.get("id"),
            "event_name": raw_event.get("name"),
            "date": date,
            "time": time,
            "timezone": raw_event.get("dates", {}).get("timezone"),
            "status": raw_event.get("dates", {}).get("status", {}).get("code"),
            "venue_name": venue.get("name"),
            "venue_city": venue.get("city", {}).get("name"),
            "venue_country": venue.get("country", {}).get("name"),
            "venue_latitude": self._to_float(location.get("latitude")),
            "venue_longitude": self._to_float(location.get("longitude")),
            "category": classification.get("segment", {}).get("name"),
            "url": raw_event.get("url"),
        }

        self._save_venue_location(normalized)
        return normalized

    def _save_venue_location(self, normalized_event):
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

    def validate(self, normalized_event):
        missing = [f for f in self.REQUIRED_FIELDS if not normalized_event.get(f)]
        return (len(missing) == 0, missing)
