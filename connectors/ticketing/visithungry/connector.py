import re
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from connector_sdk.core.base_connector import BaseConnector
from core.dedup import haversine_meters
from core.geocoding import geocode_place

BASE_URL = "https://visithungary.com"

_BG_URL_RE = re.compile(r'url\(\s*["\']?(.*?)["\']?\s*\)')

_MONTH_NAMES = (
    "January|February|March|April|May|June|July|"
    "August|September|October|November|December"
)
# Matches one date "chunk": "10-14 September, 2026" (day2 present, same
# month) or "01 May, 2026" (day2 absent, a single day within a possibly
# larger range spelled out as two separate chunks — see
# _parse_event_date_range).
_DATE_CHUNK_RE = re.compile(
    rf"(\d{{1,2}})(?:\s*[-–—]\s*(\d{{1,2}}))?\s+({_MONTH_NAMES}),?\s+(\d{{4}})",
    re.IGNORECASE,
)
_MONTH_NUM = {
    name.lower(): i
    for i, name in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        start=1,
    )
}


def _to_date(day, month_name, year):
    month = _MONTH_NUM.get((month_name or "").lower())
    if not month:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def _parse_event_date_range(raw):
    """VisitHungary's date text is always freeform and often a range, e.g.
    '10-14 September, 2026' (same-month range), '01 May, 2026 -
    28 February, 2027' (cross-month/year range, sometimes spanning most of
    a year for a long-running exhibition), or '19 September, 2026' (a
    single day). The reference scraper never parsed this into a real
    date at all — just kept the raw text. Returns (start_iso, end_iso);
    end_iso equals start_iso for a single day. Returns (None, None) if
    nothing parses."""
    if not raw:
        return None, None
    chunks = list(_DATE_CHUNK_RE.finditer(raw))
    if not chunks:
        return None, None

    first = chunks[0]
    start = _to_date(first.group(1), first.group(3), first.group(4))
    if not start:
        return None, None

    if first.group(2):  # "10-14 September, 2026" — same-month range
        end = _to_date(first.group(2), first.group(3), first.group(4))
    elif len(chunks) >= 2:  # "01 May, 2026 - 28 February, 2027"
        second = chunks[1]
        end = _to_date(second.group(1), second.group(3), second.group(4))
    else:
        end = start

    end = end or start
    return start.isoformat(), end.isoformat()


def _extract_bg_url(style):
    """Pull the URL out of a CSS `background-image: url("...")` value."""
    if not style:
        return None
    m = _BG_URL_RE.search(style)
    return m.group(1) if m else None


def _text_or_none(node):
    return node.get_text(strip=True) if node else None


def _parse_events(html):
    """Extract every event card (.poser) from the fully-rendered HTML."""
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for card in soup.select("div.poser"):
        date_span = card.select_one(".card-header b span")
        date_text = None
        if date_span:
            date_text = date_span.get_text(strip=True) or date_span.get("title")

        location = _text_or_none(card.select_one(".location-name"))
        title = _text_or_none(card.select_one("span.title"))
        tag = _text_or_none(card.select_one(".tag"))

        link = card.select_one("a[href]")
        href = link["href"] if link and link.has_attr("href") else None
        event_url = urljoin(BASE_URL, href) if href else None
        slug = href.rstrip("/").rsplit("/", 1)[-1] if href else None

        bg = card.select_one("span.bg")
        image = _extract_bg_url(bg.get("style")) if bg else None
        image = urljoin(BASE_URL, image) if image else None

        if not (title or event_url):
            continue  # empty/placeholder card

        events.append(
            {
                "title": title,
                "date": date_text,
                "location": location,
                "tag": tag,
                "url": event_url,
                "slug": slug,
                "image": image,
            }
        )

    return events


class VisitHungaryConnector(BaseConnector):
    SOURCE = "visit_hungary"
    REQUIRED_FIELDS = ["source_event_id", "event_name", "date"]

    # visithungary.com/collection/events is a NATIONWIDE site with no
    # city/location search parameter at all (confirmed: the URL only takes
    # date filters) — every event anywhere in Hungary is returned
    # regardless of which city triggered discover(). Gated on country, not
    # a specific city (contrast with Luxembourg City, which is the
    # single-CITY version of this same "source doesn't search by
    # location" pattern).
    SUPPORTED_COUNTRY = "HU"

    HEADLESS = True
    PAGE_TIMEOUT_MS = 30_000
    LOAD_MORE_PAUSE_MS = 1_500
    MAX_LOAD_MORE_CLICKS = 100

    # Sanity check for geocoding a rough region label (e.g. "Pécs region"),
    # same reasoning as StubHub/Eventbrite's venue-distance check — but
    # against Hungary's own rough center rather than one city, and with a
    # much more generous radius, since this is a whole-country source, not
    # a city one (Hungary is roughly 250km x 500km across).
    COUNTRY_CENTER = (47.1625, 19.5033)
    MAX_LOCATION_DISTANCE_METERS = 250_000

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self, days_ahead=16):
        self.days_ahead = days_ahead

    @staticmethod
    def _build_url(start, end):
        return (
            f"{BASE_URL}/collection/events"
            f"?eventDateGt_filter={start.isoformat()}"
            f"&eventDateLt_filter={end.isoformat()}"
        )

    @staticmethod
    def _dismiss_cookie_banner(page):
        for sel in (
            "button:has-text('Accept')",
            "button:has-text('Accept all')",
            "button:has-text('I agree')",
            "button:has-text('Elfogadom')",  # Hungarian: "I accept"
            "button:has-text('Rendben')",  # Hungarian: "OK"
            "button:has-text('OK')",
        ):
            try:
                btn = page.locator(sel).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=2_000)
                    page.wait_for_timeout(500)
                    return
            except Exception:
                continue

    def _click_all_load_more(self, page):
        """Keep clicking 'Load more' until every event is on the page."""
        for _ in range(self.MAX_LOAD_MORE_CLICKS):
            before = page.locator("div.poser").count()

            load_more = page.get_by_text("Load more", exact=False).first
            if load_more.count() == 0 or not load_more.is_visible():
                return

            try:
                load_more.scroll_into_view_if_needed(timeout=5_000)
                load_more.click(timeout=5_000)
            except Exception:
                return

            try:
                page.wait_for_function(
                    "prev => document.querySelectorAll('div.poser').length > prev",
                    arg=before,
                    timeout=self.PAGE_TIMEOUT_MS,
                )
            except PWTimeout:
                return
            page.wait_for_timeout(self.LOAD_MORE_PAUSE_MS)

    def discover(self, city, country):
        if country != self.SUPPORTED_COUNTRY:
            raise ValueError(
                f"VisitHungaryConnector only covers Hungary "
                f"('{self.SUPPORTED_COUNTRY}'), not '{country}'. It has no "
                f"city-level search — every event nationwide is returned "
                f"regardless of the requested city."
            )

        start = date.today()
        end = start + timedelta(days=self.days_ahead)
        url = self._build_url(start, end)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.HEADLESS)
            ctx = browser.new_context(user_agent=self.USER_AGENT, locale="en-US")
            page = ctx.new_page()

            page.goto(url, wait_until="domcontentloaded", timeout=self.PAGE_TIMEOUT_MS)
            self._dismiss_cookie_banner(page)

            try:
                page.wait_for_selector("div.poser", timeout=self.PAGE_TIMEOUT_MS)
            except PWTimeout:
                print(
                    f"[visit_hungary] no event cards appeared for {start}..{end}."
                )
                browser.close()
                return []

            self._click_all_load_more(page)
            html = page.content()
            browser.close()

        raw_events = _parse_events(html)
        for row in raw_events:
            row["_search_city"] = city
            row["_search_country"] = country
        return raw_events

    def fetch(self, raw_event):
        # discover() already scraped full detail for every event.
        return raw_event

    def normalize(self, raw_event):
        start_date, end_date = _parse_event_date_range(raw_event.get("date"))

        location_text = (raw_event.get("location") or "").strip() or None
        venue_city = raw_event.get("_search_city")
        venue_country = raw_event.get("_search_country")
        latitude = longitude = None

        if location_text:
            # Only a rough region label is given (e.g. "Pécs region",
            # sometimes spanning two towns like "Tokaj and Nyíregyháza") —
            # never a venue or address. Geocode it as-is, trusting
            # geocode_place()'s own resolved city/country over the
            # requested city, same reasoning as Universe/StubHub/
            # Eventbrite — doubly so here, since this source doesn't even
            # attempt to search by location in the first place.
            latitude, longitude, geocoded_city, geocoded_country = geocode_place(
                f"{location_text}, Hungary", name=location_text, source=self.SOURCE
            )
            if latitude is not None:
                distance = haversine_meters(latitude, longitude, *self.COUNTRY_CENTER)
                if distance is not None and distance > self.MAX_LOCATION_DISTANCE_METERS:
                    latitude = longitude = None
                    geocoded_city = geocoded_country = None
            venue_city = geocoded_city or venue_city
            venue_country = geocoded_country or venue_country

        return {
            "source": self.SOURCE,
            "source_event_id": raw_event.get("slug") or raw_event.get("url"),
            "event_name": raw_event.get("title") or None,
            "date": start_date,
            "time": None,
            "end_date": end_date,
            "end_time": None,
            # No venue name is given anywhere, only the rough region label
            # captured above as venue_city — left None rather than
            # guessing, per this project's convention.
            "venue_name": None,
            "venue_address": None,
            "venue_city": venue_city,
            "venue_country": venue_country,
            "venue_latitude": latitude,
            "venue_longitude": longitude,
            "type": raw_event.get("tag") or None,
            "url": raw_event.get("url"),
            "image_url": raw_event.get("image"),
            # Extra field, not part of the common schema yet — the raw
            # region label, kept since venue_city is only a geocoded best
            # guess derived from it.
            "location_raw": raw_event.get("location"),
        }

    def validate(self, normalized_event):
        missing = [f for f in self.REQUIRED_FIELDS if not normalized_event.get(f)]
        return (len(missing) == 0, missing)
