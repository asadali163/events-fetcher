import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from connector_sdk.core.base_connector import BaseConnector
from core.geocoding import get_coordinates

BASE_URL = "https://dice.fm"

# Dice's own display format, e.g. "Mon 20 Jul, 7:15 pm CEST" — not a
# standard format, so it gets its own parser here rather than living in the
# shared core/date_utils.py. The time portion is optional; some listings
# are date-only.
_DICE_DATE_PATTERN = re.compile(
    r"^(?P<weekday>\w+)\s+(?P<day>\d{1,2})\s+(?P<month>\w+)"
    r"(?:,\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[ap]m)\s+(?P<tz>\w+))?$",
    re.IGNORECASE,
)


def _split_dice_datetime(raw_value, reference_date=None):
    """Split Dice's combined string into (date, time). No year is given, so
    the current year is attached, rolling forward a year if that would land
    in the past. Returns (None, "") if unparseable, and (date, "") if the
    date parses but no time was given."""
    if not raw_value:
        return None, ""

    match = _DICE_DATE_PATTERN.match(raw_value.strip())
    if not match:
        return None, ""

    reference_date = reference_date or date.today()
    month, day = match.group("month"), int(match.group("day"))
    year = reference_date.year
    try:
        parsed_date = datetime.strptime(f"{month} {day} {year}", "%b %d %Y").date()
    except ValueError:
        return None, ""
    if parsed_date < reference_date:
        parsed_date = datetime.strptime(f"{month} {day} {year + 1}", "%b %d %Y").date()

    if not match.group("hour"):
        return parsed_date.isoformat(), ""

    time_str = (
        f"{match.group('hour')}:{match.group('minute')} "
        f"{match.group('ampm').upper()} {match.group('tz')}"
    )
    return parsed_date.isoformat(), time_str

# Dice.fm needs a specific browse-page slug per city — there's no reliable
# way to derive one from a city name, so new cities get added here by hand.
CITY_SLUGS = {
    "Paris": "paris-5b23e8a0e63cc224a4c36a2d",
    "Lyon": "lyon-5e3c1c5327ee96d00f784966",
    "Toronto": "toronto-5dfb2d3d7d9fd902f7ecdd5d",
}

# NOTE on selectors: dice.fm uses styled-components, so class names look like
# "EventCard__Event-sc-583c2147-1 flSCYU". The hash suffix changes between
# site builds, but the semantic prefix is stable — match on that only.
SEL_EVENT_CARD_LINK = '[class*="EventCard__Event"] a[href^="/event/"]'
SEL_LOAD_MORE_BTN = 'button:has-text("Load more")'
SEL_TITLE = 'h1[class*="EventDetailsTitle__Title"]'
SEL_TYPE = '[class*="EventDetailsTitle__Venues"]'
SEL_DATE = '[class*="EventDetailsTitle__Date"]'
SEL_VENUE = '[class*="EventDetailsVenue__Address"] span'
SEL_HIGHLIGHTS = '[class*="EventDetailsBase__Highlight"]'


class DiceConnector(BaseConnector):
    SOURCE = "dice"
    REQUIRED_FIELDS = ["source_event_id", "event_name", "date"]

    HEADLESS = True
    LOAD_MORE_PAUSE = 2.0
    MAX_LOAD_MORE = 200

    def __init__(self, days_ahead=3):
        self.days_ahead = days_ahead

    def _browse_url(self, city):
        slug = CITY_SLUGS.get(city)
        if not slug:
            raise ValueError(
                f"No Dice.fm browse-page slug known for '{city}'. "
                f"Add it to CITY_SLUGS in connector.py."
            )
        today = datetime.today().date()
        until = today + timedelta(days=self.days_ahead)
        return (
            f"{BASE_URL}/browse/{slug}" f"?from={today:%Y-%m-%d}&until={until:%Y-%m-%d}"
        )

    @staticmethod
    def _text_or_none(page, selector):
        loc = page.locator(selector)
        if loc.count() == 0:
            return None
        txt = loc.first.inner_text().strip()
        return txt or None

    @staticmethod
    def _extract_capacity(page):
        rows = page.locator(SEL_HIGHLIGHTS)
        for i in range(rows.count()):
            txt = rows.nth(i).inner_text().strip()
            if "capacity" in txt.lower():
                return txt
        return None

    def _collect_event_links(self, page, browse_url):
        page.goto(browse_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_selector(SEL_EVENT_CARD_LINK, timeout=30_000)

        clicks = 0
        while clicks < self.MAX_LOAD_MORE:
            page.mouse.wheel(0, 20_000)
            page.wait_for_timeout(800)

            btn = page.locator(SEL_LOAD_MORE_BTN)
            if btn.count() == 0 or not btn.first.is_visible():
                break

            prev_count = page.locator(SEL_EVENT_CARD_LINK).count()
            try:
                btn.first.click(timeout=10_000)
            except PWTimeout:
                break
            clicks += 1

            deadline = time.time() + 10
            while time.time() < deadline:
                if page.locator(SEL_EVENT_CARD_LINK).count() > prev_count:
                    break
                page.wait_for_timeout(500)
            page.wait_for_timeout(int(self.LOAD_MORE_PAUSE * 1000))

        hrefs = page.locator(SEL_EVENT_CARD_LINK).evaluate_all(
            "els => els.map(e => e.getAttribute('href'))"
        )
        seen, links = set(), []
        for h in hrefs:
            if h and h not in seen:
                seen.add(h)
                links.append(BASE_URL + h if h.startswith("/") else h)
        return links

    def _scrape_event(self, page, url, city):
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_selector(SEL_TITLE, timeout=15_000)
        except PWTimeout:
            pass

        return {
            "source": self.SOURCE,
            "city": city,
            "url": url,
            "title": self._text_or_none(page, SEL_TITLE),
            "type": self._text_or_none(page, SEL_TYPE),
            "date_and_time": self._text_or_none(page, SEL_DATE),
            "venue": self._text_or_none(page, SEL_VENUE),
            "capacity": self._extract_capacity(page),
        }

    def discover(self, city, country):
        # country isn't used yet — CITY_SLUGS is already city-specific, so
        # there's no cross-country ambiguity for this source today. Accepted
        # here only to satisfy BaseConnector's contract consistently.
        # One browser session is shared for the whole run — opening a fresh
        # browser per event would be far too slow. That means this method
        # does everything the original prototype's main() did (collect
        # links, then visit and scrape every event page), and fetch() below
        # is a no-op passthrough — same reasoning as the Ticketmaster
        # connector, just for a different underlying cause.
        browse_url = self._browse_url(city)
        raw_events = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.HEADLESS)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 900},
            )
            page = context.new_page()

            links = self._collect_event_links(page, browse_url)
            for url in links:
                try:
                    raw_events.append(self._scrape_event(page, url, city))
                except Exception as error:
                    print(f"[dice] failed on {url}: {error}")

            browser.close()

        return raw_events

    def fetch(self, raw_event):
        # discover() already scraped full detail for every event.
        return raw_event

    def normalize(self, raw_event):
        # SEL_TYPE ("EventDetailsTitle__Venues") is the venue name; SEL_VENUE
        # ("EventDetailsVenue__Address") is the full address — geocoding
        # needs the address, so get the mapping right here.
        venue_name = raw_event.get("type")
        venue_address = raw_event.get("venue")
        venue_city = raw_event.get("city")
        venue_country = "France"

        latitude, longitude = get_coordinates(
            address=venue_address,
            name=venue_name,
            city=venue_city,
            country=venue_country,
            source=self.SOURCE,
        )

        event_date, event_time = _split_dice_datetime(raw_event.get("date_and_time"))

        return {
            "source": raw_event.get("source"),
            "source_event_id": raw_event.get("url"),
            "event_name": raw_event.get("title"),
            "date": event_date,
            "time": event_time,
            "venue_name": venue_name,
            "venue_address": venue_address,
            "venue_city": venue_city,
            "venue_country": venue_country,
            "venue_latitude": latitude,
            "venue_longitude": longitude,
            "capacity": raw_event.get("capacity"),
            "url": raw_event.get("url"),
        }

    def validate(self, normalized_event):
        missing = [f for f in self.REQUIRED_FIELDS if not normalized_event.get(f)]
        return (len(missing) == 0, missing)
