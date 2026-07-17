import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from connector_sdk.core.base_connector import BaseConnector
from core.date_utils import split_iso_datetime
from core.geocoding import get_coordinates
from core.locations_store import save_location

load_dotenv()


class OpenAgendaConnector(BaseConnector):
    SOURCE = "openagenda"
    BASE_URL = "https://api.openagenda.com/v2"
    REQUIRED_FIELDS = ["source_event_id", "event_name", "date"]

    # OpenAgenda's agenda search (step 1) is a city-name text search, but
    # narrowing events within each agenda to a geo bounding box (step 2)
    # still needs a center point — same reasoning as Dice's CITY_SLUGS and
    # Songkick's CITY_METRO_PATHS. Add cities here as you test more.
    CITY_COORDINATES = {
        "Paris": (48.8566, 2.3522),
        # "Lyon": (45.7640, 4.8357),
    }

    DEFAULT_RADIUS_KM = 10
    MAX_AGENDAS = 20

    def __init__(self, api_key=None, days_ahead=15):
        self.api_key = api_key or os.environ["OPENAGENDA_PUBLIC_KEY"]
        self.days_ahead = days_ahead

    def _city_coordinates(self, city):
        coords = self.CITY_COORDINATES.get(city)
        if not coords:
            raise ValueError(
                f"No center coordinates known for '{city}'. "
                f"Add it to OpenAgendaConnector.CITY_COORDINATES."
            )
        return coords

    @staticmethod
    def _bbox(lat, lon, radius_km):
        delta_lat = radius_km / 111.0
        delta_lon = radius_km / (111.0 * math.cos(math.radians(lat)))
        return {
            "geo[northEast][lat]": round(lat + delta_lat, 6),
            "geo[northEast][lng]": round(lon + delta_lon, 6),
            "geo[southWest][lat]": round(lat - delta_lat, 6),
            "geo[southWest][lng]": round(lon - delta_lon, 6),
        }

    def _fetch_agenda_uids(self, city):
        params = {"key": self.api_key, "search": city, "size": self.MAX_AGENDAS}
        response = requests.get(f"{self.BASE_URL}/agendas", params=params, timeout=10)
        response.raise_for_status()
        return [a["uid"] for a in response.json().get("agendas", [])]

    def _fetch_events_for_agenda(
        self, agenda_uid, bbox, date_from, date_to, city, country
    ):
        params = {
            "key": self.api_key,
            "size": 300,
            "oaq[passed]": 1,
            "timings[gte]": date_from,
            "timings[lte]": date_to,
            "sort": "timings.asc",
            **bbox,
        }
        all_events = []
        while True:
            response = requests.get(
                f"{self.BASE_URL}/agendas/{agenda_uid}/events",
                params=params,
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            for event in data.get("events", []):
                # Stash what this search was for — used as a fallback in
                # normalize() since OpenAgenda's own location data doesn't
                # always include a clean city/country.
                event["_search_city"] = city
                event["_search_country"] = country
                all_events.append(event)

            after = data.get("after")
            if not after:
                break
            params = {k: v for k, v in params.items() if not k.startswith("after")}
            for i, val in enumerate(after):
                params[f"after[{i}]"] = val

        return all_events

    def discover(self, city, country):
        city_lat, city_lon = self._city_coordinates(city)

        agenda_uids = self._fetch_agenda_uids(city)
        if not agenda_uids:
            print(f"[openagenda] no agendas found for '{city}'")
            return []

        bbox = self._bbox(city_lat, city_lon, self.DEFAULT_RADIUS_KM)
        start = datetime.utcnow()
        end = start + timedelta(days=self.days_ahead)
        date_from = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        date_to = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        all_raw_events = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(
                    self._fetch_events_for_agenda,
                    uid,
                    bbox,
                    date_from,
                    date_to,
                    city,
                    country,
                ): uid
                for uid in agenda_uids
            }
            for future in as_completed(futures):
                try:
                    all_raw_events.extend(future.result())
                except Exception as error:
                    print(f"[openagenda] agenda fetch failed: {error}")

        seen, unique_events = set(), []
        for event in all_raw_events:
            uid = event.get("uid")
            if uid not in seen:
                seen.add(uid)
                unique_events.append(event)

        return unique_events

    def fetch(self, raw_event):
        # discover() already returns full event detail per agenda query.
        return raw_event

    @staticmethod
    def _localized_text(field, keys=("en", "fr", "es", "de")):
        if not isinstance(field, dict) or not field:
            return None
        for key in keys:
            if field.get(key):
                return field[key]
        return next(iter(field.values()), None)

    def _resolve_coordinates(
        self, latitude, longitude, address, venue_name, city, country
    ):
        if latitude is not None and longitude is not None:
            if venue_name:
                save_location(
                    "venue",
                    venue_name,
                    city,
                    country,
                    latitude,
                    longitude,
                    source=self.SOURCE,
                )
            return latitude, longitude
        if not venue_name:
            return None, None
        return get_coordinates(
            address=address,
            name=venue_name,
            city=city,
            country=country,
            source=self.SOURCE,
        )

    def normalize(self, raw_event):
        name = self._localized_text(raw_event.get("title")) or "Unknown Event"
        description = self._localized_text(raw_event.get("description")) or ""

        # nextTiming is the upcoming occurrence for this event. Important
        # for recurring events (e.g. a weekly event spanning months), where
        # firstTiming/lastTiming just bound the whole series rather than
        # telling us when the next real occurrence actually is.
        timing = raw_event.get("nextTiming") or raw_event.get("firstTiming") or {}
        date, time = split_iso_datetime(timing.get("begin"))
        end_date, end_time = split_iso_datetime(timing.get("end"))

        location = raw_event.get("location", {})
        venue_name = location.get("name")
        venue_address = location.get("address")
        venue_city = location.get("city") or raw_event.get("_search_city")
        venue_country = raw_event.get("_search_country")

        latitude, longitude = self._resolve_coordinates(
            location.get("latitude"),
            location.get("longitude"),
            venue_address,
            venue_name,
            venue_city,
            venue_country,
        )

        keywords = raw_event.get("keywords") or {}
        raw_kw = keywords.get("en") or keywords.get("fr") or []
        kw_list = [k.strip() for k in raw_kw if k and k.strip()]
        event_type = ", ".join(kw_list) if kw_list else None

        slug = raw_event.get("slug")
        origin = raw_event.get("originAgenda") or {}
        origin_slug = origin.get("slug")
        origin_uid = origin.get("uid")
        if origin_slug and slug:
            url = f"https://openagenda.com/{origin_slug}/events/{slug}"
        elif origin_uid and slug:
            url = f"https://openagenda.com/agendas/{origin_uid}/events/{slug}"
        else:
            url = None

        uid = raw_event.get("uid")

        return {
            "source": self.SOURCE,
            "source_event_id": str(uid) if uid is not None else None,
            "event_name": name,
            "description": description,
            "date": date,
            "time": time,
            "end_date": end_date,
            "end_time": end_time,
            "venue_name": venue_name,
            "venue_address": venue_address,
            "venue_city": venue_city,
            "venue_country": venue_country,
            "venue_latitude": latitude,
            "venue_longitude": longitude,
            "type": event_type,
            # Undocumented numeric type IDs (raw field: "types-devenement") —
            # preserved as-is since there's no ID-to-label reference to
            # decode them against yet.
            "category_ids": raw_event.get("types-devenement") or [],
            "url": url,
        }

    def validate(self, normalized_event):
        missing = [f for f in self.REQUIRED_FIELDS if not normalized_event.get(f)]
        return (len(missing) == 0, missing)
