import base64
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from connector_sdk.core.base_connector import BaseConnector
from core.dedup import haversine_meters
from core.geocoding import geocode_place

BASE_URL = "https://www.stubhub.com"

# "Mon, Aug 10 • TBA"  /  "Wed, Aug 05 • 7:00 PM" — StubHub omits the year,
# so it's inferred from the requested window in normalize() (same approach
# as Bandsintown's _parse_bandsintown_date).
_MONTHS_ALT = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
DATE_RE = re.compile(
    rf"^[A-Za-z]{{3}},?\s+({_MONTHS_ALT})\s+(\d{{1,2}})(?:\s*[•·]\s*(.+))?$",
    re.IGNORECASE,
)
MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ],
        start=1,
    )
}

_DATE_HINT = re.compile(
    r"(mon|tue|wed|thu|fri|sat|sun|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|•|\b\d{1,2}:\d{2}\b|am|pm",
    re.IGNORECASE,
)
_PRICE_HINT = re.compile(r"[€$£]\s?\d")


def _b64(value):
    return base64.b64encode(value.encode()).decode()


def _epoch_ms(dt):
    return int(dt.timestamp() * 1000)


def _looks_like_date(text):
    return bool(_DATE_HINT.search(text))


def _parse_stubhub_date(raw, start, end):
    """'Wed, Aug 05 • 7:00 PM' -> ('2026-08-05', '19:00'). Year is inferred
    from the requested window (handles Dec->Jan rollover). 'TBA' or an
    unparseable time yields an empty time string. Returns (None, "") if the
    date itself can't be parsed."""
    match = DATE_RE.match((raw or "").strip())
    if not match:
        return None, ""

    month = MONTHS[match.group(1)[:3].lower()]
    day = int(match.group(2))
    time_part = (match.group(3) or "").strip()

    candidates = []
    for year in (start.year, start.year + 1, start.year - 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue  # e.g. Feb 29 on a non-leap year
    if not candidates:
        return None, ""

    inside = [d for d in candidates if start <= d <= end]
    chosen = (
        inside[0] if inside else min(candidates, key=lambda d: abs((d - start).days))
    )

    time_str = ""
    if time_part and time_part.upper() != "TBA":
        try:
            t = datetime.strptime(re.sub(r"\s+", " ", time_part.upper()), "%I:%M %p")
            time_str = t.strftime("%H:%M")
        except ValueError:
            pass

    return chosen.isoformat(), time_str


def _parse_events(html):
    """Ported as-is from the reference scraper. Anchors each card on the
    href pattern (/event/) rather than any CSS class name, then climbs to
    the enclosing list item / div for the card's other text."""
    soup = BeautifulSoup(html, "html.parser")
    events = {}

    for a in soup.select('a[href*="/event/"]'):
        href = a.get("href", "")
        match = re.search(r"/event/(\d+)", href)
        if not match:
            continue
        event_id = match.group(1)

        card = a.find_parent("li") or a.find_parent("div") or a.parent

        h3 = card.find("h3") if card else None
        name = (h3.get_text(strip=True) if h3 else None) or a.get("aria-label")

        raw = [p.get_text(strip=True) for p in (card.find_all("p") if card else [])]
        candidates = [
            t
            for t in raw
            if t and t.lower() not in ("follow", "followed") and not t.isdigit()
        ]

        date_text = next((t for t in candidates if _looks_like_date(t)), None)
        venue = next((t for t in candidates if t != date_text), None)
        if date_text is None and len(candidates) >= 1:
            date_text = candidates[0]
            venue = venue or (candidates[1] if len(candidates) > 1 else None)

        price = next((t for t in candidates if _PRICE_HINT.search(t)), None)

        img = card.find("img") if card else None
        image_url = img.get("src") if img else None

        follow_count = None
        if card:
            for p in card.find_all("p"):
                if p.get_text(strip=True).isdigit():
                    follow_count = p.get_text(strip=True)
                    break

        events[event_id] = {
            "event_id": event_id,
            "name": name,
            "date_text": date_text,
            "venue": venue,
            "price_from": price,
            "follow_count": follow_count,
            "image_url": image_url,
            "url": urljoin(BASE_URL, href),
        }

    return list(events.values())


class StubHubConnector(BaseConnector):
    SOURCE = "stubhub"
    REQUIRED_FIELDS = ["source_event_id", "event_name", "date"]

    # StubHub's explore search takes plain lat/lon (base64-encoded in the
    # URL) rather than a city name, and the from/to epoch-millisecond range
    # needs a real IANA timezone to compute correct day boundaries
    # regardless of what timezone the machine running this is in — same
    # reasoning as every other connector's own city-mapping dict, just
    # bundled as one 3-tuple per city instead of two separate dicts.
    CITY_SEARCH_PARAMS = {
        "Paris": (48.856614, 2.352222, "Europe/Paris"),
        "Budapest": (47.497912, 19.040235, "Europe/Budapest"),
        "Amsterdam": (52.3675734, 4.9041389, "Europe/Amsterdam"),
        "Brussels": (50.847703, 4.357200, "Europe/Brussels"),
        "Luxembourg City": (49.612333, 6.125843, "Europe/Luxembourg"),
        "Toronto": (43.6548253, -79.388447, "America/Toronto"),
    }

    HEADLESS = False  # untested in headless mode yet — same caution as Bandsintown
    MAX_SHOW_MORE = 300  # safety cap on "Show More" clicks
    CLICK_PAUSE_SEC = 1.5
    PAGE_TIMEOUT_MS = 60_000

    # Bare venue names (no street address) sometimes match the wrong place
    # entirely in OpenStreetMap — verified in practice: "Olympic Aquatics
    # Centre, Paris, France" once resolved to Vaux-sur-Mer, a coastal town
    # 400+ km away. If the geocoded point lands further than this from the
    # searched city's own center, the match is discarded as unreliable
    # rather than trusted.
    MAX_VENUE_DISTANCE_METERS = 50_000
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )

    def __init__(self, days_ahead=14):
        self.days_ahead = days_ahead

    def _city_search_params(self, city):
        params = self.CITY_SEARCH_PARAMS.get(city)
        if not params:
            raise ValueError(
                f"No search coordinates/timezone known for '{city}'. "
                f"Add it to StubHubConnector.CITY_SEARCH_PARAMS."
            )
        return params

    def _build_url(self, city):
        lat, lon, tz_name = self._city_search_params(city)
        tz = ZoneInfo(tz_name)
        start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=self.days_ahead)).replace(
            hour=23, minute=59, second=59, microsecond=999000
        )
        params = {
            "from": _epoch_ms(start),
            "to": _epoch_ms(end),
            "lat": _b64(f"{lat}"),
            "lon": _b64(f"{lon}"),
        }
        url = f"{BASE_URL}/explore?{urlencode(params)}"
        return url, start.date(), end.date()

    @staticmethod
    def _dismiss_cookies(page):
        for sel in (
            "#onetrust-accept-btn-handler",
            "button:has-text('Accept All')",
            "button:has-text('Accept')",
            "button:has-text('Got it')",
        ):
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1500):
                    loc.click(timeout=2000)
                    return
            except Exception:
                continue

    @staticmethod
    def _count_events(page):
        try:
            return page.locator('a[href*="/event/"]').count()
        except Exception:
            return 0

    def _load_all_events(self, page):
        """Scrolls and clicks 'Show More' (located by text, not class name —
        StubHub's classes are unstable) until the list stops growing."""
        self._dismiss_cookies(page)
        clicks = 0
        stalls = 0
        last_count = -1

        while clicks < self.MAX_SHOW_MORE:
            page.mouse.wheel(0, 25_000)
            page.wait_for_timeout(int(self.CLICK_PAUSE_SEC * 1000))

            count = self._count_events(page)

            btn = page.get_by_role(
                "button", name=re.compile("show more", re.IGNORECASE)
            )
            clicked = False
            try:
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.scroll_into_view_if_needed()
                    btn.first.click()
                    clicks += 1
                    clicked = True
                    page.wait_for_timeout(int(self.CLICK_PAUSE_SEC * 1000))
            except PWTimeout:
                clicked = False
            except Exception:
                clicked = False

            if not clicked:
                if count == last_count:
                    stalls += 1
                    if stalls >= 2:
                        break
                else:
                    stalls = 0
            last_count = count

        return clicks

    def discover(self, city, country):
        url, range_start, range_end = self._build_url(city)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.HEADLESS)
            ctx = browser.new_context(locale="en-US", user_agent=self.USER_AGENT)
            page = ctx.new_page()
            page.set_default_timeout(self.PAGE_TIMEOUT_MS)

            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)

            self._load_all_events(page)

            html = page.content()
            browser.close()

        raw_events = _parse_events(html)
        for row in raw_events:
            row["_search_city"] = city
            row["_search_country"] = country
            row["_range_start"] = range_start.isoformat()
            row["_range_end"] = range_end.isoformat()

        return raw_events

    def fetch(self, raw_event):
        # The listing card's text already gives everything needed — no
        # separate event-page visit required.
        return raw_event

    def normalize(self, raw_event):
        range_start = date.fromisoformat(raw_event["_range_start"])
        range_end = date.fromisoformat(raw_event["_range_end"])
        event_date, event_time = _parse_stubhub_date(
            raw_event.get("date_text", ""), range_start, range_end
        )

        venue_name = (raw_event.get("venue") or "").strip() or None
        venue_city = raw_event.get("_search_city")
        venue_country = raw_event.get("_search_country")
        latitude = longitude = None

        if venue_name:
            # Only a bare venue name is available here, no separate street
            # address — the searched city/country is appended to the
            # geocoding query itself (not just the cache key) to help
            # disambiguate generic venue names, while still trusting
            # geocode_place()'s own resolved city/country over the search
            # input, since a lat/lon-radius "explore" search isn't
            # guaranteed to stay within the literal searched city (same
            # lesson learned from Universe and Bandsintown).
            query = f"{venue_name}, {venue_city}, {venue_country}"
            latitude, longitude, geocoded_city, geocoded_country = geocode_place(
                query, name=venue_name, source=self.SOURCE
            )

            # Sanity check: a bare venue name with no street address can
            # match the wrong place entirely (verified in practice — see
            # MAX_VENUE_DISTANCE_METERS above). Discard the match if it
            # lands implausibly far from the searched city's own center.
            search_city = raw_event.get("_search_city")
            if latitude is not None and search_city in self.CITY_SEARCH_PARAMS:
                city_lat, city_lon, _ = self._city_search_params(search_city)
                distance = haversine_meters(latitude, longitude, city_lat, city_lon)
                if distance is not None and distance > self.MAX_VENUE_DISTANCE_METERS:
                    latitude = longitude = None
                    geocoded_city = geocoded_country = None

            venue_city = geocoded_city or venue_city
            venue_country = geocoded_country or venue_country

        return {
            "source": self.SOURCE,
            "source_event_id": raw_event.get("event_id"),
            "event_name": raw_event.get("name") or None,
            "date": event_date,
            "time": event_time,
            "venue_name": venue_name,
            "venue_address": None,
            "venue_city": venue_city,
            "venue_country": venue_country,
            "venue_latitude": latitude,
            "venue_longitude": longitude,
            "url": raw_event.get("url"),
            "image_url": raw_event.get("image_url") or None,
            # Extra fields, not part of the common schema used elsewhere
            # yet (same precedent as Meetup's "attendees" and Universe's
            # price fields) — preserved since they're free and available.
            "follow_count": raw_event.get("follow_count"),
            "price_from": raw_event.get("price_from"),
        }

    def validate(self, normalized_event):
        missing = [f for f in self.REQUIRED_FIELDS if not normalized_event.get(f)]
        return (len(missing) == 0, missing)
