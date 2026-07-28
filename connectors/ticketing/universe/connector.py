import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from connector_sdk.core.base_connector import BaseConnector
from core.geocoding import geocode_place

CURRENCY_RE = re.compile(r"[€$£¥]")
_MONTHS_ALT = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
DATE_RE = re.compile(
    rf"(?:[A-Za-z]{{3,9}},?\s+)?({_MONTHS_ALT})[a-z]*\s+(\d{{1,2}}),\s*(\d{{4}})",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"\d{1,2}(?::\d{2})?\s*[APap][Mm]")
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


def _iso(month_name, day, year):
    mi = MONTHS.get(month_name[:3].lower())
    return f"{int(year):04d}-{mi:02d}-{int(day):02d}" if mi else ""


def _parse_dates(raw):
    """-> (start_date, end_date, start_time, end_time). Handles single/double
    dates, with or without a time range. Ported from the reference scraper,
    validated against real card markup."""
    raw = (raw or "").strip()
    if not raw:
        return "", "", "", ""
    date_part, time_part = raw, ""
    m = re.split(r"\s+at\s+", raw, maxsplit=1, flags=re.IGNORECASE)
    if len(m) == 2:
        date_part, time_part = m
    dates = DATE_RE.findall(date_part)
    start_date = _iso(*dates[0]) if len(dates) >= 1 else ""
    end_date = _iso(*dates[1]) if len(dates) >= 2 else ""
    times = TIME_RE.findall(time_part)
    start_time = times[0].upper().replace(" ", "") if len(times) >= 1 else ""
    end_time = times[1].upper().replace(" ", "") if len(times) >= 2 else ""
    return start_date, end_date, start_time, end_time


def _parse_price(raw):
    """-> (currency, price_min, price_max)."""
    raw = (raw or "").strip()
    if not raw:
        return "", None, None
    if re.search(r"free", raw, re.IGNORECASE):
        return "", 0.0, 0.0
    cur = CURRENCY_RE.search(raw)
    currency = cur.group(0) if cur else ""
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", raw)]
    if not nums:
        return currency, None, None
    return currency, min(nums), max(nums)


def _find_card(host_anchor):
    el = host_anchor
    for _ in range(8):
        if el is None:
            break
        txt = el.get_text(" ")
        if el.select_one('a[href*="/events/"]') and (
            CURRENCY_RE.search(txt) or "free" in txt.lower()
        ):
            return el
        el = el.parent
    return host_anchor.parent


def _parse_card(card):
    host_a = card.select_one('a[href*="/users/"]')
    host = host_a.get_text(strip=True) if host_a else ""

    event_anchors = card.select('a[href*="/events/"]')
    title, event_url = "", ""
    for a in event_anchors:
        t = a.get_text(strip=True)
        if (
            t
            and not CURRENCY_RE.search(t)
            and not re.fullmatch(r"free", t, re.IGNORECASE)
        ):
            title, event_url = t, a.get("href", "")
            break
    if not event_url and event_anchors:
        event_url = event_anchors[0].get("href", "")

    price_raw = ""
    for a in event_anchors:
        t = a.get_text(strip=True)
        if CURRENCY_RE.search(t) or re.search(r"free", t, re.IGNORECASE):
            price_raw = t
            break
    if not price_raw:
        pe = card.select_one('[class*="price"]')
        if pe:
            price_raw = pe.get_text(strip=True)

    date_raw = ""
    for p in card.find_all("p"):
        if DATE_RE.search(p.get_text(" ", strip=True)):
            date_raw = p.get_text(" ", strip=True)
            break
    if not date_raw:
        for s in card.stripped_strings:
            if DATE_RE.search(s):
                date_raw = s
                break

    # The venue text is never isolated from the rest of the card by any
    # dedicated class — it's picked out heuristically as the longest
    # comma-containing leftover string. This is a full address blob (e.g.
    # "Nieuwezijds Kolk 25, 1012 PV Amsterdam, Nederland"), not a clean
    # venue name — normalize() geocodes it as-is rather than trying to
    # split it, since the segment order isn't even consistent between cards
    # (sometimes "street, postcode city, country", sometimes
    # "street, city, postcode, country").
    known = {host, title, price_raw, date_raw}
    candidates = []
    for s in card.stripped_strings:
        s = s.strip()
        if not s or s in known or CURRENCY_RE.search(s) or DATE_RE.search(s):
            continue
        if re.search(r"[A-Za-z]", s):
            candidates.append(s)
    with_comma = [c for c in candidates if "," in c]
    pool = with_comma or candidates
    location = max(pool, key=len) if pool else ""

    image_url = ""
    bg = card.select_one('[style*="background-image"]')
    if bg:
        mm = re.search(r'url\((["\']?)(.*?)\1\)', bg.get("style", ""))
        if mm:
            image_url = mm.group(2)
    if not image_url:
        img = card.select_one("img")
        if img:
            image_url = img.get("src", "")

    sd, ed, st, et = _parse_dates(date_raw)
    currency, pmin, pmax = _parse_price(price_raw)
    return {
        "title": title,
        "host": host,
        "start_date": sd,
        "end_date": ed,
        "start_time": st,
        "end_time": et,
        "date_raw": date_raw,
        "location": location,
        "price_raw": price_raw,
        "currency": currency,
        "price_min": pmin,
        "price_max": pmax,
        "event_url": event_url,
        "image_url": image_url,
    }


def _parse_events_from_html(html):
    soup = BeautifulSoup(html, "lxml")
    events, seen = [], set()
    for host_a in soup.select('a[href*="/users/"]'):
        card = _find_card(host_a)
        if card is None:
            continue
        row = _parse_card(card)
        key = row.get("event_url") or (row.get("title"), row.get("date_raw"))
        if not row.get("title") or key in seen:
            continue
        seen.add(key)
        events.append(row)
    return events


def _get_total_pages(html, max_pages):
    """Read the 'X of Y' pagination label; default to 1 if absent."""
    soup = BeautifulSoup(html, "lxml")
    label = soup.select_one(".pagination-component__label")
    if label:
        m = re.search(r"of\s+(\d+)", label.get_text())
        if m:
            return min(int(m.group(1)), max_pages)
    nums = [
        int(a.get_text())
        for a in soup.select(".pagination-component__page--link")
        if a.get_text().strip().isdigit()
    ]
    return min(max(nums), max_pages) if nums else 1


def _page_url(base, n):
    sep = "&" if "?" in base else "?"
    base = re.sub(r"([?&])page=\d+", "", base)  # drop existing page=
    base = re.sub(r"[?&]$", "", base)
    return f"{base}{sep}page={n}"


def _extract_event_id(event_url):
    """.../meijt-invites-ferry-worldpride-tickets-7Z9DF0?ref=... -> '7Z9DF0'.
    The unique code is always the last hyphen-separated segment of the
    path, right before the query string."""
    if not event_url:
        return None
    path = event_url.split("?")[0].rstrip("/")
    match = re.search(r"-([A-Za-z0-9]+)$", path)
    return match.group(1) if match else None


def _clean_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class UniverseConnector(BaseConnector):
    SOURCE = "universe"
    REQUIRED_FIELDS = ["source_event_id", "event_name", "date"]

    # Universe's explore search needs a rough city center point for its
    # `ll=` param — same reasoning as OpenAgenda's CITY_COORDINATES. Add
    # more cities here as you test them.
    CITY_COORDINATES = {
        "Amsterdam": (52.3675734, 4.9041389),
        "Brussels": (50.84770289999999, 4.357200100000001),
        "Toronto": (43.6548253, -79.388447),
    }

    HEADLESS = True
    MAX_PAGES = 50
    MIN_DELAY, MAX_DELAY = 2.0, 4.0
    NAV_TIMEOUT = 45_000
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )

    # discover() returns everything it finds, with no date-range filtering
    # (deliberate — see connector.py history), so the dispatcher's usual
    # days_ahead window-containment refetch check doesn't apply here.
    # Instead, refetch on a timer: skip re-running for a city until this
    # many days have passed since the last successful fetch. Same pattern
    # as MeetupConnector.REFETCH_INTERVAL_DAYS.
    REFETCH_INTERVAL_DAYS = 3

    def __init__(self, days_ahead=None):
        # Always None — discover() has no date-range filter and ignores
        # this entirely, so the dispatcher's window-containment refetch
        # logic must never apply to this connector. See
        # REFETCH_INTERVAL_DAYS above for the refetch strategy that
        # applies instead.
        self.days_ahead = None

    def _city_coordinates(self, city):
        coords = self.CITY_COORDINATES.get(city)
        if not coords:
            raise ValueError(
                f"No center coordinates known for '{city}'. "
                f"Add it to UniverseConnector.CITY_COORDINATES."
            )
        return coords

    def _build_url(self, city, country):
        lat, lon = self._city_coordinates(city)
        # Universe's own search UI double-URL-encodes these two params
        # (verified against a real explore URL) — quote() applied twice
        # reproduces that exactly.
        loc = quote(quote(f"{city}, {country}"))
        ll = quote(quote(f"{lat},{lon}"))
        return f"https://www.universe.com/explore?query=&loc={loc}&ll={ll}"

    def discover(self, city, country):
        base_url = self._build_url(city, country)

        all_events, seen_urls = [], set()

        print("BASE URL: ", base_url)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.HEADLESS)
            ctx = browser.new_context(user_agent=self.USER_AGENT, locale="en-US")
            page = ctx.new_page()
            page.set_default_navigation_timeout(self.NAV_TIMEOUT)

            first_url = _page_url(base_url, 1)
            print("First URL: ", first_url)
            page.goto(first_url, wait_until="domcontentloaded")
            try:
                page.wait_for_selector('a[href*="/events/"]', timeout=15_000)
            except Exception:
                print(f"[universe] no event cards appeared for {city} — skipping.")
                browser.close()
                return []

            html = page.content()
            total = _get_total_pages(html, self.MAX_PAGES)
            print(f"[universe] {city}: {total} page(s) reported")

            for n in range(1, total + 1):
                if n > 1:
                    url = _page_url(base_url, n)
                    page.goto(url, wait_until="domcontentloaded")
                    try:
                        page.wait_for_selector('a[href*="/events/"]', timeout=15_000)
                    except Exception:
                        print(f"[universe] no events on page {n} — stopping.")
                        break
                    html = page.content()

                for row in _parse_events_from_html(html):
                    key = row.get("event_url") or (
                        row.get("title"),
                        row.get("date_raw"),
                    )
                    if key in seen_urls:
                        continue
                    seen_urls.add(key)

                    row["_search_city"] = city
                    row["_search_country"] = country
                    all_events.append(row)

                print(f"[universe]   page {n}/{total}: {len(all_events)} events so far")
                if n < total:
                    time.sleep(random.uniform(self.MIN_DELAY, self.MAX_DELAY))

            browser.close()

        return all_events

    def fetch(self, raw_event):
        # The listing card's text already gives everything needed — no
        # separate event-page visit required.
        return raw_event

    def normalize(self, raw_event):
        address = raw_event.get("location") or None
        venue_city = raw_event.get("_search_city")
        venue_country = raw_event.get("_search_country")
        latitude = longitude = None

        if address:
            latitude, longitude, geocoded_city, geocoded_country = geocode_place(
                address, name=address, source=self.SOURCE
            )
            # Prefer the geocoder's own resolved city/country over the
            # searched city — Universe's explore results aren't confined to
            # the literal searched city (a Haarlem event showed up under an
            # Amsterdam-area search in real output), the same lesson
            # learned from Bandsintown's Brussels/Tomorrowland case.
            venue_city = geocoded_city or venue_city
            venue_country = geocoded_country or venue_country

        return {
            "source": self.SOURCE,
            "source_event_id": _extract_event_id(raw_event.get("event_url")),
            "event_name": raw_event.get("title") or None,
            "date": raw_event.get("start_date") or None,
            "time": raw_event.get("start_time") or None,
            "end_date": raw_event.get("end_date") or None,
            "end_time": raw_event.get("end_time") or None,
            # No venue name is ever cleanly separable from the scraped
            # address blob (see _parse_card) — left None rather than
            # guessing, per this project's convention.
            "venue_name": None,
            "venue_address": address,
            "venue_city": venue_city,
            "venue_country": venue_country,
            "venue_latitude": latitude,
            "venue_longitude": longitude,
            "url": (
                _clean_url(raw_event["event_url"])
                if raw_event.get("event_url")
                else None
            ),
            "image_url": raw_event.get("image_url") or None,
            # Extra fields, not part of the common schema used elsewhere
            # yet (same precedent as Meetup's "attendees") — preserved
            # since Universe is the only source that currently offers them.
            "organizer": raw_event.get("host") or None,
            "currency": raw_event.get("currency") or None,
            "price_min": raw_event.get("price_min"),
            "price_max": raw_event.get("price_max"),
        }

    def validate(self, normalized_event):
        missing = [f for f in self.REQUIRED_FIELDS if not normalized_event.get(f)]
        return (len(missing) == 0, missing)
