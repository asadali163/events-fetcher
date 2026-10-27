import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from connector_sdk.core.base_connector import BaseConnector
from core.date_utils import split_iso_datetime
from core.geocoding import get_coordinates

# Meetup's own location-filter slug format, e.g. "fr--Paris" — there's no
# reliable way to derive this from a city name, so new cities get added
# here by hand: search the city on meetup.com/find/ and read the
# `location=` query param off the resulting URL.
CITY_LOCATION_SLUGS = {
    "Paris": "fr--Paris",
}

EVENT_ID_RE = re.compile(r"/events/(\d+)")


def _canonical_event_url(href):
    """Strip tracking params -> https://www.meetup.com/<group>/events/<id>/"""
    match = EVENT_ID_RE.search(href or "")
    if not match:
        return None
    path = urlparse(href).path
    end = path.find(match.group(0)) + len(match.group(0))
    clean = path[:end]
    if not clean.endswith("/"):
        clean += "/"
    return f"https://www.meetup.com{clean}"


class MeetupConnector(BaseConnector):
    SOURCE = "meetup"
    REQUIRED_FIELDS = ["source_event_id", "event_name", "date"]

    # Deliberately scoped to 50+ attendee events only — not a gap, a
    # deliberate choice. Meetup's find page needs a size filter applied via
    # its dropdown to page through results at all.
    SIZE_FILTERS = [
        {"label": "50-100 attendees", "url_hint": "fiftyToHundred"},
        {"label": "100+ attendees", "url_hint": "hundredplus"},
    ]

    HEADLESS = False
    MIN_DELAY = 2.5
    MAX_DELAY = 5.0
    MAX_SCROLLS = 80
    NAV_TIMEOUT = 30_000

    # No real date-range filter exists on Meetup's find page, so the usual
    # days_ahead window doesn't mean anything here. Instead the dispatcher
    # skips this connector if it already ran successfully within the last
    # REFETCH_INTERVAL_DAYS days, regardless of window. Placeholder value —
    # tune later.
    REFETCH_INTERVAL_DAYS = 10

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )

    def __init__(self, days_ahead=None):
        # Always None — discover() has no real date-range filter and ignores
        # this entirely, so the dispatcher's window-containment refetch
        # logic must never apply to this connector. See REFETCH_INTERVAL_DAYS
        # above for the refetch strategy that applies instead.
        self.days_ahead = None

    def _location_slug(self, city):
        slug = CITY_LOCATION_SLUGS.get(city)
        if not slug:
            raise ValueError(
                f"No Meetup location slug known for '{city}'. "
                f"Add it to CITY_LOCATION_SLUGS in connector.py."
            )
        return slug

    def _base_url(self, city):
        slug = self._location_slug(city)
        return (
            f"https://www.meetup.com/find/?location={slug}"
            "&source=EVENTS&distance=anyDistance&eventType=inPerson"
        )

    @staticmethod
    def _dismiss_overlays(page):
        for name in ["Accept", "Accept all", "Allow all", "Agree", "OK", "Got it"]:
            try:
                btn = page.get_by_role("button", name=re.compile(name, re.I))
                if btn.count() and btn.first.is_visible():
                    btn.first.click(timeout=1500)
                    page.wait_for_timeout(400)
                    break
            except Exception:
                pass
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

    def _apply_size_filter(self, page, label, url_hint):
        btn = page.locator('button[data-testid="RevampedFilterButtonAttendeeCount"]')
        btn.wait_for(state="visible", timeout=self.NAV_TIMEOUT)
        btn.click()

        option = page.get_by_role("menuitem").filter(has_text=label).first
        option.wait_for(state="visible", timeout=8000)
        option.click()

        try:
            page.wait_for_url(
                re.compile("attendeeCount=" + re.escape(url_hint)), timeout=8000
            )
        except PWTimeout:
            pass
        page.wait_for_timeout(2500)  # let the filtered list re-render

    def _scroll_and_harvest(self, page):
        seen = {}  # event_id -> canonical url

        def harvest():
            anchors = page.locator('a[data-event-label="Event Card"]')
            for j in range(anchors.count()):
                cu = _canonical_event_url(anchors.nth(j).get_attribute("href"))
                if cu:
                    seen[EVENT_ID_RE.search(cu).group(1)] = cu

        harvest()
        stagnant = 0
        for _ in range(self.MAX_SCROLLS):
            before = len(seen)

            clicked = False
            for label in ["See more", "Show more", "See all", "Load more"]:
                try:
                    btn = page.get_by_role("button", name=re.compile(label, re.I))
                    if btn.count() and btn.first.is_visible():
                        btn.first.click()
                        clicked = True
                        break
                except Exception:
                    pass
            if not clicked:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

            deadline = time.time() + 7
            while time.time() < deadline:
                page.wait_for_timeout(700)
                harvest()
                if len(seen) > before:
                    break

            if len(seen) == before:
                stagnant += 1
                if stagnant >= 3:  # no new events for 3 rounds -> reached the end
                    break
            else:
                stagnant = 0

        return seen

    @staticmethod
    def _first_text(page, selectors):
        for sel in selectors:
            loc = page.locator(sel)
            if loc.count():
                try:
                    t = loc.first.inner_text(timeout=2000).strip()
                    if t:
                        return t
                except Exception:
                    continue
        return ""

    def _scrape_event(self, page, url, city, country):
        page.goto(url, timeout=self.NAV_TIMEOUT, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

        match = EVENT_ID_RE.search(url)
        event_id = match.group(1) if match else None

        name = self._first_text(page, ["h1"])

        start_time = ""
        tloc = page.locator("time[datetime]")
        if tloc.count():
            start_time = tloc.first.get_attribute("datetime") or ""

        # ds2-k16 primary <p> that does NOT wrap a <time> — the time <p>
        # shares the same classes, hence the nested-time check.
        venue = ""
        prim = page.locator("p.ds2-k16.text-ds2-text-fill-primary-enabled")
        for k in range(prim.count()):
            p = prim.nth(k)
            if p.locator("time").count() == 0:
                txt = (p.inner_text() or "").strip()
                if txt:
                    venue = txt
                    break

        addr_loc = page.locator("p.ds2-r16.text-ds2-text-fill-tertiary-enabled")
        address = addr_loc.first.inner_text().strip() if addr_loc.count() else ""

        attendees = ""
        try:
            badge = page.locator(
                'xpath=//h2[normalize-space()="Attendees"]/..'
                "/following-sibling::div//span"
            )
            if badge.count():
                m = re.search(r"\d[\d,]*", badge.first.inner_text())
                if m:
                    attendees = m.group(0).replace(",", "")
        except Exception:
            pass
        if not attendees:
            m = re.search(
                r"([\d,]+)\s+attendees", page.locator("body").inner_text(), re.I
            )
            if m:
                attendees = m.group(1).replace(",", "")

        return {
            "event_id": event_id,
            "event_url": url,
            "name": name,
            "start_time": start_time,
            "venue": venue,
            "address": address,
            "attendees": attendees,
            "_search_city": city,
            "_search_country": country,
        }

    def discover(self, city, country):
        # One shared browser session for the whole run (both size filters,
        # every event page) — a fresh browser per event would be far too
        # slow. fetch() below is a no-op passthrough, same reasoning as
        # Dice/Bandsintown.
        base_url = self._base_url(city)
        found = {}  # event_id -> canonical url, deduped across both filters

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.HEADLESS)
            ctx = browser.new_context(user_agent=self.USER_AGENT)
            ctx.set_default_timeout(self.NAV_TIMEOUT)
            page = ctx.new_page()

            for flt in self.SIZE_FILTERS:
                page.goto(
                    base_url, timeout=self.NAV_TIMEOUT, wait_until="domcontentloaded"
                )
                page.wait_for_timeout(2000)
                self._dismiss_overlays(page)
                try:
                    self._apply_size_filter(page, flt["label"], flt["url_hint"])
                except Exception as error:
                    print(f"[meetup] could not apply filter '{flt['label']}': {error}")
                    continue

                events = self._scroll_and_harvest(page)
                print(f"[meetup] {flt['label']}: {len(events)} events found")
                found.update(events)

            print(f"[meetup] visiting {len(found)} event pages...")
            raw_events = []
            for i, (event_id, url) in enumerate(found.items(), 1):
                try:
                    raw_events.append(self._scrape_event(page, url, city, country))
                except PWTimeout:
                    print(f"[meetup] timeout on {url} — skipped")
                except Exception as error:
                    print(f"[meetup] failed on {url}: {error}")
                if i % 20 == 0:
                    print(f"[meetup]   {i}/{len(found)} event pages visited")
                time.sleep(random.uniform(self.MIN_DELAY, self.MAX_DELAY))

            browser.close()

        return raw_events

    def fetch(self, raw_event):
        # discover() already scraped full detail for every event.
        return raw_event

    def normalize(self, raw_event):
        event_date, event_time = split_iso_datetime(raw_event.get("start_time"))

        venue_name = raw_event.get("venue") or None
        venue_address = raw_event.get("address") or None
        venue_city = raw_event.get("_search_city")
        venue_country = raw_event.get("_search_country")

        latitude = longitude = None
        if venue_name or venue_address:
            latitude, longitude = get_coordinates(
                address=venue_address or venue_name,
                name=venue_name or venue_address,
                city=venue_city,
                country=venue_country,
                source=self.SOURCE,
            )

        attendees_raw = raw_event.get("attendees")
        attendees = (
            int(attendees_raw) if attendees_raw and attendees_raw.isdigit() else None
        )

        return {
            "source": self.SOURCE,
            "source_event_id": raw_event.get("event_id"),
            "event_name": raw_event.get("name") or None,
            "date": event_date,
            "time": event_time,
            "venue_name": venue_name,
            "venue_address": venue_address,
            "venue_city": venue_city,
            "venue_country": venue_country,
            "venue_latitude": latitude,
            "venue_longitude": longitude,
            # Not part of the common schema anywhere else yet — potentially
            # useful input for a future Attendance Estimator pipeline stage.
            "attendees": attendees,
            "url": raw_event.get("event_url"),
        }

    def validate(self, normalized_event):
        missing = [f for f in self.REQUIRED_FIELDS if not normalized_event.get(f)]
        return (len(missing) == 0, missing)
