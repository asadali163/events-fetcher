import hashlib
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from connector_sdk.core.base_connector import BaseConnector
from core.date_utils import split_iso_datetime
from core.geocoding import get_coordinates
from core.locations_store import save_location

BASE_URL = "https://www.songkick.com"

# Songkick needs a specific metro-area path per city — there's no reliable
# way to derive one from a city name, so new cities get added here by hand.
CITY_METRO_PATHS = {
    "Budapest": "/metro-areas/29047-hungary-budapest",
    "Paris": "metro-areas/28909-france-paris",
    "Lyon": "metro-areas/28889-france-lyon",
}

# Deliberately not ";" — a semicolon is the list separator in many Excel
# locales and would split a multi-value field if this is ever exported.
MULTI_SEP = " | "

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Connection": "keep-alive",
}

RETRY_STATUSES = {403, 406, 429, 503}
QUERY_DATE_FORMAT = "%m/%d/%Y"  # what the site's own date picker submits


def _make_session():
    try:
        import cloudscraper

        session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    except Exception:
        session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _fetch(session, url, referer, retries=4, backoff=3.0):
    headers = {"Referer": referer} if referer else {}
    resp = None
    for attempt in range(1, retries + 1):
        resp = session.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            return resp
        if resp.status_code in RETRY_STATUSES and attempt < retries:
            time.sleep(backoff * attempt)
            continue
        return resp
    return resp


def _to_query_date(iso_date):
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime(QUERY_DATE_FORMAT)


def _build_page_url(metro_path, page, min_date=None, max_date=None):
    params = []
    if min_date:
        params.append(("filters[minDate]", _to_query_date(min_date)))
    if max_date:
        params.append(("filters[maxDate]", _to_query_date(max_date)))
    if page > 1:
        params.append(("page", page))
    url = urljoin(BASE_URL, metro_path)
    if params:
        url += "?" + urlencode(params)
    return url


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _clean_geo(lat, lon):
    """Return (lat, lon) as validated floats, or (None, None). Also fixes a
    lat/lon that arrived swapped (latitude must be within +/-90)."""
    lat, lon = _num(lat), _num(lon)
    if lat is None or lon is None:
        return None, None
    lat_ok, lon_ok = -90 <= lat <= 90, -180 <= lon <= 180
    if lat_ok and lon_ok:
        return lat, lon
    if not lat_ok and -180 <= lat <= 180 and -90 <= lon <= 90:
        return lon, lat
    return None, None


def _fallback_event_id(name, date_str, venue_name):
    """Used only when Songkick's JSON-LD doesn't include an event URL — a
    stable, deterministic id so the same event doesn't get a new id every
    run just because we couldn't read one from the page."""
    basis = f"{name}|{date_str}|{venue_name}"
    return "songkick-" + hashlib.md5(basis.encode("utf-8")).hexdigest()[:16]


def _parse_event(li):
    data = {}
    data["listing_title"] = li.get("title")  # e.g. "Friday 24 July 2026"

    t = li.find("time")
    data["start_datetime_tz"] = t.get("datetime") if t else None

    # Headline + support acts, taken from the listing exactly as displayed.
    # The <strong> holds the headline (for a festival that's the festival
    # name); any remaining text is the support lineup.
    headline_html, support_html = None, None
    artists_p = li.select_one("p.artists")
    if artists_p:
        strong = artists_p.find("strong")
        if strong:
            headline_html = strong.get_text(" ", strip=True)
            span = strong.find_parent("span") or artists_p
            full = span.get_text(" ", strip=True)
            rest = full.replace(headline_html, "", 1).strip(" ,;·-–")
            support_html = re.sub(r"\s+", " ", rest).strip()
    data["headline_html"] = headline_html
    data["support_html"] = support_html

    ven = li.select_one("a.venue-link[href]")
    venue_name_html = ven.get_text(strip=True) if ven else None

    city = li.select_one("span.city-name")
    data["city_name"] = city.get_text(strip=True) if city else None

    jsonld = {}
    script = li.find("script", attrs={"type": "application/ld+json"})
    if script:
        raw = script.string or script.get_text()
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                jsonld = next(
                    (
                        o
                        for o in parsed
                        if isinstance(o, dict) and "Event" in str(o.get("@type", ""))
                    ),
                    parsed[0] if parsed else {},
                )
            elif isinstance(parsed, dict):
                jsonld = parsed
        except Exception:
            pass

    data["jsonld_name"] = jsonld.get("name")
    data["start_date"] = jsonld.get("startDate")
    # Standard Schema.org Event field — not yet confirmed populated on
    # Songkick's real pages, verify against a live sample.
    data["event_url"] = jsonld.get("url")

    loc = jsonld.get("location") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    addr = loc.get("address") or {}
    if isinstance(addr, list):
        addr = addr[0] if addr else {}
    geo = loc.get("geo") or {}
    if isinstance(geo, list):
        geo = geo[0] if geo else {}

    data["venue_name"] = loc.get("name") or venue_name_html
    data["street_address"] = addr.get("streetAddress")
    data["locality"] = addr.get("addressLocality")
    data["country"] = addr.get("addressCountry")
    data["latitude"], data["longitude"] = _clean_geo(
        geo.get("latitude"), geo.get("longitude")
    )

    performers = jsonld.get("performer") or []
    if isinstance(performers, dict):
        performers = [performers]
    names, genres = [], []
    for p in performers:
        if not isinstance(p, dict):
            continue
        if p.get("name"):
            names.append(p["name"])
        g = p.get("genre") or []
        if isinstance(g, str):
            g = [g]
        genres.extend(g)
    data["performers"] = names
    data["genres"] = list(dict.fromkeys(genres))

    return data


def _parse_page(html):
    soup = BeautifulSoup(html, "html.parser")
    events = [_parse_event(li) for li in soup.select("li.event-listings-element")]
    has_next = soup.select_one("div.pagination a.next_page") is not None
    return events, has_next


class SongkickConnector(BaseConnector):
    SOURCE = "songkick"
    REQUIRED_FIELDS = ["source_event_id", "event_name", "date"]
    MAX_PAGES = 100
    DELAY_SECONDS = 2.0

    def __init__(self, days_ahead=15):
        self.days_ahead = days_ahead

    def _date_window(self):
        start = date.today()
        end = start + timedelta(days=self.days_ahead - 1)
        return start.isoformat(), end.isoformat()

    def discover(self, city, country):
        # country isn't used — CITY_METRO_PATHS is already city-specific,
        # same reasoning as Dice. Accepted only for BaseConnector contract
        # consistency.
        metro_path = CITY_METRO_PATHS.get(city)
        if not metro_path:
            raise ValueError(
                f"No Songkick metro-area path known for '{city}'. "
                f"Add it to CITY_METRO_PATHS in connector.py."
            )
        min_date, max_date = self._date_window()

        session = _make_session()
        all_events, page, referer = [], 1, BASE_URL + "/"
        while page <= self.MAX_PAGES:
            url = _build_page_url(metro_path, page, min_date, max_date)
            resp = _fetch(session, url, referer)
            if resp.status_code != 200:
                print(
                    f"[songkick] HTTP {resp.status_code} after retries — "
                    f"stopping at page {page}"
                )
                break
            events, has_next = _parse_page(resp.text)
            if not events:
                break
            all_events.extend(events)
            if not has_next:
                break
            referer = url
            page += 1
            time.sleep(self.DELAY_SECONDS)

        return all_events

    def fetch(self, raw_event):
        # The listing page's embedded JSON-LD already gives full detail per
        # event, so there's nothing extra to fetch — same reasoning as
        # Ticketmaster.
        return raw_event

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
        name = raw_event.get("headline_html")
        if not name and raw_event.get("jsonld_name"):
            name = raw_event["jsonld_name"].split(" @ ")[0].strip()
        performers = raw_event.get("performers") or []
        if not name and performers:
            name = performers[0]

        venue_name = raw_event.get("venue_name")
        venue_city = raw_event.get("locality") or raw_event.get("city_name")
        venue_country = raw_event.get("country")
        street_address = raw_event.get("street_address")

        latitude, longitude = self._resolve_coordinates(
            raw_event.get("latitude"),
            raw_event.get("longitude"),
            street_address,
            venue_name,
            venue_city,
            venue_country,
        )

        event_url = raw_event.get("event_url")
        source_event_id = event_url or _fallback_event_id(
            name,
            raw_event.get("start_date") or raw_event.get("listing_title"),
            venue_name,
        )

        # start_datetime_tz has a real time when Songkick announced one;
        # start_date is a date-only fallback. listing_title (a display
        # string like "Friday 24 July 2026") isn't ISO 8601 and won't parse
        # via split_iso_datetime — if we land there, date stays None and
        # validate() correctly flags the event as missing a date.
        event_date, event_time = split_iso_datetime(raw_event.get("start_datetime_tz"))
        if event_date is None:
            event_date, event_time = split_iso_datetime(raw_event.get("start_date"))

        return {
            "source": self.SOURCE,
            "source_event_id": source_event_id,
            "event_name": name or "",
            "date": event_date,
            "time": event_time,
            "venue_name": venue_name,
            "venue_address": street_address,
            "venue_city": venue_city,
            "venue_country": venue_country,
            "venue_latitude": latitude,
            "venue_longitude": longitude,
            "genres": raw_event.get("genres") or [],
            "url": event_url,
        }

    def validate(self, normalized_event):
        missing = [f for f in self.REQUIRED_FIELDS if not normalized_event.get(f)]
        return (len(missing) == 0, missing)
